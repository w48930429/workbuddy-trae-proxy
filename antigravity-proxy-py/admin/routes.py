import os
import json
import base64
import time
import uuid
import re
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, HTMLResponse
import httpx

from pool.account_pool import AccountPool
from pool.refresher import TokenRefresher
from pool.quota_manager import QuotaManager
from pool.local_extractor import extract_from_windows_credential
from config import save_config, DEFAULT_OAUTH_CLIENT_ID, DEFAULT_OAUTH_CLIENT_SECRET


def read_email_from_id_token(id_token: Optional[str]) -> str:
    if not id_token:
        return 'unknown@gmail.com'
    try:
        parts = id_token.split('.')
        if len(parts) >= 2:
            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += '=' * (4 - rem)
            payload_json = base64.b64decode(payload_b64).decode('utf-8')
            payload = json.loads(payload_json)
            return payload.get('email', 'unknown@gmail.com')
    except Exception:
        pass
    return 'unknown@gmail.com'


class OAuthSession:
    def __init__(self, server: HTTPServer, thread: threading.Thread, state: str, auth_url: str, expires_at: float):
        self.server = server
        self.thread = thread
        self.state = state
        self.auth_url = auth_url
        self.expires_at = expires_at

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass


oauth_session: Optional[OAuthSession] = None
oauth_session_lock = threading.Lock()


def build_admin_router(
    pool: AccountPool,
    refresher: TokenRefresher,
    config: Dict[str, Any],
    quota_manager: Optional[QuotaManager] = None
) -> APIRouter:
    router = APIRouter()

    def check_admin_auth(request: Request) -> Optional[Response]:
        auth_header = request.headers.get('x-admin-password') or request.headers.get('authorization')
        provided_pass = ''
        if auth_header:
            if auth_header.startswith('Bearer '):
                provided_pass = auth_header[7:].strip()
            else:
                provided_pass = auth_header.strip()

        admin_pass = config.get('adminPassword', 'admin123')
        if not provided_pass or provided_pass != admin_pass:
            return JSONResponse(status_code=401, content={'error': 'Unauthorized: invalid admin password'})
        return None

    # 1. 获取系统统计信息
    @router.get('/api/admin/stats')
    async def get_stats(request: Request):
        err = check_admin_auth(request)
        if err:
            return err
        return pool.get_stats()

    # 2. 获取所有账号
    @router.get('/api/admin/accounts')
    async def get_accounts(request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        accounts = pool.get_all_accounts()
        masked_list = []
        for acc in accounts:
            a_copy = dict(acc)
            rf = acc.get('refreshToken', '')
            at = acc.get('accessToken', '')
            a_copy['refreshTokenMasked'] = f"{rf[:6]}...{rf[-4:]}" if rf and len(rf) >= 10 else (rf or '')
            a_copy['accessTokenMasked'] = f"{at[:10]}..." if at and len(at) >= 10 else (at or '')
            masked_list.append(a_copy)
        return {'accounts': masked_list}

    # 3. 添加账号
    @router.post('/api/admin/accounts')
    async def add_account(request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={'error': 'Invalid JSON body'})

        if not body.get('email') or not body.get('refreshToken'):
            return JSONResponse(status_code=400, content={'error': 'email and refreshToken are required'})

        new_acc = pool.add_account(
            email=body['email'],
            refresh_token=body['refreshToken'],
            access_token=body.get('accessToken'),
            expires_in_seconds=body.get('expiresInSeconds', 3600),
            proxy_url=body.get('proxyUrl'),
            max_concurrency=body.get('maxConcurrency', 5),
            tier=body.get('tier', 'unknown')
        )

        import asyncio
        asyncio.create_task(refresher.refresh_single_account(new_acc))
        return {'message': 'Account added successfully', 'account': new_acc}

    # 4. 删除账号
    @router.delete('/api/admin/accounts/{acc_id}')
    async def delete_account(acc_id: str, request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        success = pool.remove_account(acc_id)
        if not success:
            return JSONResponse(status_code=404, content={'error': 'Account not found'})
        return {'message': 'Account removed successfully'}

    # 4.1 更新指定账号配置
    @router.put('/api/admin/accounts/{acc_id}')
    async def update_account_config(acc_id: str, request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={'error': 'Invalid JSON body'})

        acc = pool.get_account(acc_id)
        if not acc:
            return JSONResponse(status_code=404, content={'error': 'Account not found'})

        updates: Dict[str, Any] = {}
        if 'proxyUrl' in body:
            raw = str(body['proxyUrl'] or '').strip()
            if not raw or raw.lower() == 'direct' or raw == '直连':
                updates['proxyUrl'] = None
            else:
                norm = raw
                if not re.match(r'^(https?|socks5?)://', norm, re.IGNORECASE):
                    norm = 'http://' + norm
                updates['proxyUrl'] = norm

        if 'maxConcurrency' in body:
            try:
                mc = int(body['maxConcurrency'])
                if mc >= 1:
                    updates['maxConcurrency'] = min(50, mc)
            except Exception:
                pass

        if 'tier' in body:
            t = str(body['tier']).lower()
            if t in ['free', 'pro', 'ultra']:
                updates['tier'] = t

        updated = pool.update_account(acc_id, updates)
        return {'success': True, 'message': '账号配置已成功更新', 'account': updated}

    # 5. 手动刷新指定账号
    @router.post('/api/admin/accounts/{acc_id}/refresh')
    async def refresh_account(acc_id: str, request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        acc = pool.get_account(acc_id)
        if not acc:
            return JSONResponse(status_code=404, content={'error': 'Account not found'})

        ok = await refresher.refresh_single_account(acc)
        return {'success': ok, 'account': acc}

    # 6. 重置账号状态为 active
    @router.post('/api/admin/accounts/{acc_id}/reset')
    async def reset_account(acc_id: str, request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        acc = pool.get_account(acc_id)
        if not acc:
            return JSONResponse(status_code=404, content={'error': 'Account not found'})

        acc['status'] = 'active'
        acc['cooldownUntil'] = 0
        acc['activeConcurrency'] = 0
        acc['lastError'] = None
        pool.save_accounts()
        return {'message': 'Account status reset to active', 'account': acc}

    # 6.1 手动启用或停用账号
    @router.put('/api/admin/accounts/{acc_id}/status')
    async def toggle_account_status(acc_id: str, request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={'error': 'Invalid JSON body'})

        status = body.get('status')
        if status not in ['active', 'disabled']:
            return JSONResponse(status_code=400, content={'error': 'status must be active or disabled'})

        acc = pool.get_account(acc_id)
        if not acc:
            return JSONResponse(status_code=404, content={'error': 'Account not found'})

        acc['status'] = status
        acc['cooldownUntil'] = 0
        if status == 'active':
            acc['lastError'] = None
        pool.save_accounts()
        msg = '账号已停用，不会参与后续调度' if status == 'disabled' else '账号已重新启用'
        return {'success': True, 'message': msg, 'account': acc}

    # 7. 查询/刷新指定账号真实额度
    @router.post('/api/admin/accounts/{acc_id}/quota')
    async def get_account_quota(acc_id: str, request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        acc = pool.get_account(acc_id)
        if not acc:
            return JSONResponse(status_code=404, content={'error': 'Account not found'})
        if not quota_manager:
            return JSONResponse(status_code=500, content={'error': 'QuotaManager not initialized'})

        now_ms = int(time.time() * 1000)
        if not acc.get('accessToken') or now_ms >= acc.get('accessTokenExpiresAt', 0):
            await refresher.refresh_single_account(acc)

        quota = await quota_manager.fetch_quota_for_account(acc)
        return {'success': bool(quota), 'quota': quota, 'account': acc}

    # 8. 一键刷新全池所有账号的真实额度
    @router.post('/api/admin/sync-all-quota')
    async def sync_all_quota(request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        if not quota_manager:
            return JSONResponse(status_code=500, content={'error': 'QuotaManager not initialized'})

        now_ms = int(time.time() * 1000)
        for acc in pool.get_all_accounts():
            if not acc.get('accessToken') or now_ms >= acc.get('accessTokenExpiresAt', 0):
                try:
                    await refresher.refresh_single_account(acc)
                except Exception:
                    pass

        await quota_manager.fetch_all_quotas()
        return {'success': True, 'message': 'All account quotas updated successfully'}

    # 9. 查看与更新系统配置
    @router.get('/api/admin/config')
    async def get_config(request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        return {
            'port': config.get('port'),
            'apiMasterKey': config.get('apiMasterKey'),
            'upstreamUrl': config.get('upstreamUrl'),
            'defaultMaxConcurrency': config.get('defaultMaxConcurrency'),
            'cooldownSeconds': config.get('cooldownSeconds'),
            'defaultProxyUrl': config.get('defaultProxyUrl'),
            'hasOauthCredentials': bool(config.get('oauthClientId'))
        }

    @router.put('/api/admin/config')
    async def update_config(request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={'error': 'Invalid JSON body'})

        if 'apiMasterKey' in body:
            config['apiMasterKey'] = body['apiMasterKey']
        if 'adminPassword' in body:
            config['adminPassword'] = body['adminPassword']
        if 'upstreamUrl' in body:
            config['upstreamUrl'] = body['upstreamUrl']
        if 'cooldownSeconds' in body:
            config['cooldownSeconds'] = body['cooldownSeconds']
        if 'defaultProxyUrl' in body:
            config['defaultProxyUrl'] = body['defaultProxyUrl']

        save_config(config)
        return {'message': 'Configuration updated', 'config': config}

    # 10. 从本机 Antigravity 登录缓存一键同步凭据
    @router.post('/api/admin/sync-local')
    async def sync_local(request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        creds = extract_from_windows_credential()
        if not creds or not creds.get('refreshToken'):
            return JSONResponse(
                status_code=404,
                content={'error': '未在本机凭据管理器中找到已登录的 Antigravity 凭据，请先确保 Antigravity 已登录'}
            )

        email = creds.get('email') or 'antigravity-local@google.com'
        existing = next((a for a in pool.get_all_accounts() if a.get('email') == email), None)

        if existing:
            target_acc = pool.update_account(existing['id'], {
                'refreshToken': creds['refreshToken'],
                'accessToken': creds.get('accessToken'),
                'status': 'active',
                'cooldownUntil': 0
            })
        else:
            target_acc = pool.add_account(
                email=email,
                refresh_token=creds['refreshToken'],
                access_token=creds.get('accessToken'),
                max_concurrency=5
            )

        if target_acc:
            import asyncio
            asyncio.create_task(refresher.refresh_single_account(target_acc))

        return {
            'message': '成功从本机 Antigravity 提取并同步账号',
            'account': target_acc
        }

    # 11. 复用 Google OAuth 流程
    @router.post('/api/admin/oauth/start')
    async def oauth_start(request: Request):
        nonlocal pool
        err = check_admin_auth(request)
        if err:
            return err

        global oauth_session
        with oauth_session_lock:
            now_ms = time.time() * 1000
            if oauth_session and oauth_session.expires_at > now_ms:
                return {'authUrl': oauth_session.auth_url, 'expiresAt': oauth_session.expires_at}
            if oauth_session:
                oauth_session.close()
                oauth_session = None

            oauth_port = 4567
            oauth_redirect_uri = f"http://127.0.0.1:{oauth_port}/callback"
            oauth_scopes = ' '.join([
                'https://www.googleapis.com/auth/cloud-platform',
                'https://www.googleapis.com/auth/userinfo.email',
                'https://www.googleapis.com/auth/userinfo.profile',
                'openid'
            ])
            state = str(uuid.uuid4())
            client_id = config.get('oauthClientId') or DEFAULT_OAUTH_CLIENT_ID
            client_secret = config.get('oauthClientSecret') or DEFAULT_OAUTH_CLIENT_SECRET

            params = {
                'client_id': client_id,
                'redirect_uri': oauth_redirect_uri,
                'response_type': 'code',
                'scope': oauth_scopes,
                'access_type': 'offline',
                'prompt': 'consent',
                'state': state
            }
            auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

            class CallbackHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    parsed_path = urllib.parse.urlparse(self.path)
                    if parsed_path.path != '/callback':
                        self.send_response(404)
                        self.end_headers()
                        return

                    query_params = urllib.parse.parse_qs(parsed_path.query)
                    cb_state = query_params.get('state', [''])[0]
                    code = query_params.get('code', [''])[0]
                    error = query_params.get('error', [''])[0]

                    if cb_state != state:
                        self.send_response(400)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        self.wfile.write('<h2>授权验证失败，请返回控制台重新发起登录。</h2>'.encode('utf-8'))
                        return

                    if error or not code:
                        self.send_response(400)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        self.wfile.write('<h2>Google 授权未完成。</h2><p>请关闭此页面并重试。</p>'.encode('utf-8'))
                        return

                    try:
                        token_resp = httpx.post(
                            'https://oauth2.googleapis.com/token',
                            data={
                                'client_id': client_id,
                                'client_secret': client_secret,
                                'code': code,
                                'grant_type': 'authorization_code',
                                'redirect_uri': oauth_redirect_uri
                            },
                            timeout=30.0
                        )
                        token_data = token_resp.json()
                        refresh_token = token_data.get('refresh_token')
                        if not refresh_token:
                            err_msg = token_data.get('error_description') or 'Google 未返回可长期使用的 Refresh Token'
                            raise Exception(err_msg)

                        email = read_email_from_id_token(token_data.get('id_token'))
                        existing = next((a for a in pool.get_all_accounts() if a.get('email') == email), None)

                        if existing:
                            acc = pool.update_account(existing['id'], {
                                'refreshToken': refresh_token,
                                'accessToken': token_data.get('access_token', ''),
                                'accessTokenExpiresAt': int(time.time() * 1000) + int(token_data.get('expires_in', 3600)) * 1000,
                                'status': 'active',
                                'cooldownUntil': 0
                            })
                        else:
                            acc = pool.add_account(
                                email=email,
                                refresh_token=refresh_token,
                                access_token=token_data.get('access_token'),
                                expires_in_seconds=token_data.get('expires_in', 3600),
                                max_concurrency=config.get('defaultMaxConcurrency', 5)
                            )

                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        body_html = f'<main style="font-family:system-ui;text-align:center;margin:72px auto;max-width:480px"><h1>授权成功</h1><p>{email} 已加入账号池，可以关闭此页面返回控制台。</p></main>'
                        self.wfile.write(body_html.encode('utf-8'))
                    except Exception as ex:
                        self.send_response(500)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        self.wfile.write(f'<h2>Token 交换失败</h2><p>{ex}</p>'.encode('utf-8'))

            try:
                server = HTTPServer(('127.0.0.1', oauth_port), CallbackHandler)
            except Exception:
                return JSONResponse(
                    status_code=409,
                    content={'error': f'OAuth 回调端口 {oauth_port} 正在被占用，请先关闭已有的流程'}
                )

            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()

            expires_at = time.time() * 1000 + 10 * 60 * 1000
            oauth_session = OAuthSession(server, th, state, auth_url, expires_at)
            return {'authUrl': auth_url, 'expiresAt': expires_at}

    # 12. 更新调度策略
    @router.post('/api/admin/strategy')
    async def update_strategy(request: Request):
        err = check_admin_auth(request)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={'error': 'Invalid JSON body'})

        strategy = body.get('strategy')
        if strategy not in ['zero-waste', 'sequential-drain', 'least-concurrency', 'round-robin']:
            return JSONResponse(
                status_code=400,
                content={'error': 'Invalid strategy. Allowed: zero-waste, sequential-drain, least-concurrency, round-robin'}
            )

        pool.set_routing_strategy(strategy)
        config['routingStrategy'] = strategy
        save_config(config)
        return {'success': True, 'strategy': strategy, 'message': f'调度策略已切换为: {strategy}'}

    return router
