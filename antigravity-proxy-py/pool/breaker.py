import os
import json
import re
from typing import Optional, Dict, Any, Set
from datetime import datetime
import httpx

from .account_pool import AccountPool, current_time_ms
from .refresher import TokenRefresher
from .quota_manager import QuotaManager


class UpstreamRequestError(Exception):
    def __init__(self, status_code: int, upstream_body: str):
        super().__init__(f"Google Upstream returned HTTP {status_code}: {upstream_body}")
        self.status_code = status_code
        self.upstream_body = upstream_body


class ContextOverflowError(UpstreamRequestError):
    pass


def is_context_overflow(status_code: int, body: str) -> bool:
    if status_code != 400:
        return False
    patterns = [
        r"input token count exceeds the maximum number of tokens",
        r"maximum number of tokens allowed",
        r"CONTEXT_LENGTH_EXCEEDED",
        r"prompt is too long"
    ]
    for p in patterns:
        if re.search(p, body, re.IGNORECASE):
            return True
    return False


CROSS_FAMILY_FALLBACK_ENABLED = False


class RequestExecutor:
    def __init__(
        self,
        pool: AccountPool,
        refresher: TokenRefresher,
        config: Dict[str, Any],
        quota_manager: Optional[QuotaManager] = None
    ):
        self.pool = pool
        self.refresher = refresher
        self.config = config
        self.quota_manager = quota_manager

    async def count_tokens(
        self,
        payload: Dict[str, Any],
        max_retries: int = 3,
        session_key: Optional[str] = None
    ) -> Dict[str, Any]:
        tried_account_ids: Set[str] = set()
        model = (payload.get('model') or '').lower()
        family = 'claude' if any(k in model for k in ('claude', 'opus', 'sonnet', 'gpt', 'oss')) else 'gemini'

        upstream_url = self.config.get('upstreamUrl', 'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse')
        count_url = upstream_url.replace('streamGenerateContent', 'countTokens')
        count_url = re.sub(r'[?&]alt=sse\b', '', count_url, flags=re.IGNORECASE).rstrip('?&')

        for attempt in range(1, max_retries + 1):
            account = self.pool.acquire_account(tried_account_ids, family, session_key)
            if not account:
                if self.pool.is_family_exhausted(family):
                    alt_family = 'claude' if family == 'gemini' else 'gemini'
                    alt_ok = not self.pool.is_family_exhausted(alt_family)
                    hint = f"，但 {alt_family} 仍有额度（生成请求会自动切到 {alt_family} 模型）" if alt_ok else ""
                    raise Exception(f"No available {family} account for token counting. {family} 额度已全灭{hint}。")
                raise Exception(f"No available {family} account for token counting.")

            tried_account_ids.add(account['id'])

            if not account.get('accessToken') or current_time_ms() >= account.get('accessTokenExpiresAt', 0):
                ok = await self.refresher.refresh_single_account(account)
                if not ok:
                    self.pool.release_account(account['id'], {'code': 401, 'message': 'Token refresh failed during countTokens'}, family)
                    continue

            try:
                effective_proxy = (
                    account.get('proxyUrl') or
                    self.config.get('defaultProxyUrl') or
                    os.environ.get('HTTPS_PROXY') or
                    os.environ.get('HTTP_PROXY')
                )
                client_kwargs: Dict[str, Any] = {
                    'timeout': httpx.Timeout(connect=60.0, read=120.0, write=60.0, pool=60.0)
                }
                if effective_proxy:
                    client_kwargs['proxy'] = effective_proxy

                headers = {
                    'Authorization': f"Bearer {account['accessToken']}",
                    'Content-Type': 'application/json',
                    'User-Agent': 'antigravity/2.14.0',
                    'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
                }

                request_bodies = [
                    payload,
                    {'request': payload.get('request', {})}
                ]
                counted_tokens: Optional[int] = None
                last_failure = ''

                async with httpx.AsyncClient(**client_kwargs) as client:
                    for req_body in request_bodies:
                        res = await client.post(count_url, headers=headers, json=req_body)
                        body_text = res.text
                        if 200 <= res.status_code < 300:
                            try:
                                parsed = res.json()
                                total = (
                                    parsed.get('totalTokens') or
                                    parsed.get('total_tokens') or
                                    parsed.get('promptTokenCount') or
                                    parsed.get('usageMetadata', {}).get('promptTokenCount')
                                )
                                if total is not None:
                                    total_int = int(total)
                                    counted_tokens = max(counted_tokens or 0, total_int)
                                    continue
                            except Exception:
                                pass
                            last_failure = f"countTokens response did not include totalTokens: {body_text[:300]}"
                            continue

                        last_failure = f"countTokens upstream HTTP {res.status_code}: {body_text}"
                        if res.status_code in (401, 403):
                            self.pool.release_account(account['id'])
                            raise UpstreamRequestError(res.status_code, body_text)

                if counted_tokens is not None:
                    self.pool.release_account(account['id'])
                    return {'totalTokens': counted_tokens, 'accountUsed': account}

                self.pool.release_account(account['id'])
                raise Exception(last_failure or 'countTokens request failed')

            except Exception as err:
                if isinstance(err, UpstreamRequestError):
                    raise
                self.pool.release_account(account['id'], {'code': 500, 'message': f"countTokens network error: {err}"}, family)
                if attempt == max_retries:
                    raise

        raise Exception('Failed to count tokens after retries.')

    async def execute_with_retry(
        self,
        payload: Dict[str, Any],
        max_retries: int = 3,
        session_key: Optional[str] = None
    ) -> Dict[str, Any]:
        tried_account_ids: Set[str] = set()
        last_overflow_error: Optional[ContextOverflowError] = None
        last_capacity_error: Optional[UpstreamRequestError] = None

        model = (payload.get('model') or '').lower()
        family = 'claude' if any(k in model for k in ('claude', 'opus', 'sonnet', 'gpt', 'oss')) else 'gemini'
        upstream_url = self.config.get('upstreamUrl', 'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse')

        for attempt in range(1, max_retries + 1):
            account = self.pool.acquire_account(tried_account_ids, family, session_key)

            if not account:
                if last_overflow_error:
                    print('[Executor] 全部候选账号均因上下文超限失败，回报 400 让客户端压缩上下文。')
                    raise last_overflow_error

                if family == 'claude':
                    nearest_diff = float('inf')
                    for acc in self.pool.get_all_accounts():
                        reset_iso = acc.get('quota', {}).get('claudeResetTime')
                        if reset_iso:
                            try:
                                dt = datetime.fromisoformat(reset_iso.replace('Z', '+00:00'))
                                diff = int(dt.timestamp() * 1000) - current_time_ms()
                                if 0 < diff < nearest_diff:
                                    nearest_diff = diff
                            except Exception:
                                pass
                    time_hint = (
                        f" (最近回满倒计时: {int(nearest_diff // 3600000)}小时{int((nearest_diff % 3600000) // 60000)}分)"
                        if nearest_diff != float('inf') else ""
                    )
                    raise Exception(
                        f"当前账号池所有账号的 Claude/GPT 5小时额度均已耗尽{time_hint}。Gemini 额度依然充足可用，建议在客户端切换为 gemini-3.8-flash 等模型，或等待额度自动回满。"
                    )
                elif family == 'gemini':
                    nearest_diff = float('inf')
                    for acc in self.pool.get_all_accounts():
                        reset_iso = acc.get('quota', {}).get('geminiResetTime')
                        if reset_iso:
                            try:
                                dt = datetime.fromisoformat(reset_iso.replace('Z', '+00:00'))
                                diff = int(dt.timestamp() * 1000) - current_time_ms()
                                if 0 < diff < nearest_diff:
                                    nearest_diff = diff
                            except Exception:
                                pass
                    time_hint = (
                        f" (最近回满倒计时: {int(nearest_diff // 3600000)}小时{int((nearest_diff % 3600000) // 60000)}分)"
                        if nearest_diff != float('inf') else ""
                    )
                    raise Exception(
                        f"当前账号池所有账号的 Gemini 5小时额度均已耗尽{time_hint}。Claude / GPT 额度依然充足可用，建议在客户端切换为 claude-3-7-sonnet 等模型，或等待额度自动回满。"
                    )
                raise Exception('All accounts in the pool are busy, in cooldown, or exhausted.')

            tried_account_ids.add(account['id'])

            # 检查 Token 是否需要刷新
            if not account.get('accessToken') or current_time_ms() >= account.get('accessTokenExpiresAt', 0):
                ok = await self.refresher.refresh_single_account(account)
                if not ok:
                    self.pool.release_account(account['id'], {'code': 401, 'message': 'Initial token refresh failed'}, family)
                    continue

            try:
                effective_proxy = (
                    account.get('proxyUrl') or
                    self.config.get('defaultProxyUrl') or
                    os.environ.get('HTTPS_PROXY') or
                    os.environ.get('HTTP_PROXY')
                )
                client_kwargs: Dict[str, Any] = {
                    'timeout': httpx.Timeout(connect=60.0, read=600.0, write=60.0, pool=60.0)
                }
                if effective_proxy:
                    client_kwargs['proxy'] = effective_proxy

                upstream_headers = {
                    'Authorization': f"Bearer {account['accessToken']}",
                    'Content-Type': 'application/json',
                    'User-Agent': 'antigravity/2.14.0',
                    'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
                }

                client = httpx.AsyncClient(**client_kwargs)
                req = client.build_request('POST', upstream_url, headers=upstream_headers, json=payload)
                res = await client.send(req, stream=True)

                if 200 <= res.status_code < 300:
                    return {
                        'statusCode': res.status_code,
                        'headers': res.headers,
                        'response': res,
                        'client': client,
                        'accountUsed': account
                    }

                # 非 2xx 响应，读取 body 并关闭连接
                err_bytes = await res.aread()
                err_body = err_bytes.decode('utf-8', errors='replace')
                await res.aclose()
                await client.aclose()

                if res.status_code == 429:
                    print(f"[Executor] 账号 {account.get('email')} 触发 429 限频/额度枯竭 (第 {attempt}/{max_retries} 次尝试)，正在无感自动换号...: {err_body[:120]}")
                    self.pool.release_account(account['id'], {'code': 429, 'message': err_body}, family)
                    if self.quota_manager:
                        asyncio.create_task(self.quota_manager.fetch_quota_for_account(account))
                    continue

                if res.status_code == 401:
                    print(f"[Executor] Account {account.get('email')} hit 401 Unauthorized: {err_body}")
                    refresh_ok = await self.refresher.refresh_single_account(account)
                    if refresh_ok:
                        tried_account_ids.discard(account['id'])
                    else:
                        self.pool.release_account(account['id'], {'code': 401, 'message': 'Token refresh failed'})
                    continue

                if res.status_code == 403:
                    print(f"[Executor] Account {account.get('email')} hit 403 Forbidden: {err_body}")
                    self.pool.release_account(account['id'], {'code': 403, 'message': err_body})
                    continue

                if res.status_code == 503:
                    print(f"[Executor] Google Upstream 503: {err_body}")
                    last_capacity_error = UpstreamRequestError(res.status_code, err_body)
                    if 'MODEL_CAPACITY_EXHAUSTED' in err_body and 'gpt-oss' in (payload.get('model') or ''):
                        print("[Executor] GPT-OSS capacity exhausted, automatically falling back to gemini-3.8-flash-high...")
                        payload['model'] = 'gemini-3.8-flash-high'
                        tried_account_ids.discard(account['id'])
                        self.pool.release_account(account['id'])
                        continue
                    self.pool.release_account(account['id'], {'code': 503, 'message': err_body})
                    continue

                self.pool.release_account(account['id'])
                if is_context_overflow(res.status_code, err_body):
                    last_overflow_error = ContextOverflowError(res.status_code, err_body)
                    if attempt < max_retries:
                        print(f"[Executor] 账号 {account.get('email')} 返回上下文超限（{payload.get('model')}），尝试换下一个账号...")
                        continue
                    raise last_overflow_error

                raise UpstreamRequestError(res.status_code, err_body)

            except Exception as err:
                if isinstance(err, UpstreamRequestError):
                    raise
                print(f"[Executor] Network or connection error for account {account.get('email')} (attempt {attempt}/{max_retries}): {err}")
                self.pool.release_account(account['id'], {'code': 500, 'message': f"Network error: {err}"})
                if attempt == max_retries:
                    raise

        if last_overflow_error:
            raise last_overflow_error
        if last_capacity_error:
            raise last_capacity_error
        raise Exception(f"Failed to fulfill request after {max_retries} attempts with rotating accounts.")
