"""华为云 CodeArts Agent OAuth2 PKCE + DPoP 授权与令牌生命周期管理 —— 纯 Python 原生实现。

协议规范参考：
  1. HITZY2002/codearts2api (Go) - OAuth2 PKCE + DPoP 流程与 Token 刷新机制
  2. 华为云 CodeArts Agent 官方扩展 (huaweicloud.authentication v5.4.2)
     - portalHost: https://codearts.huaweicloud.com/portal
     - iamStsHost: https://sts.cn-north-4.myhuaweicloud.com
     - AUTH_REDIRECT_URL: /oauth/callback
     - CLIENT_ID: codearts-agent
     - DPoP: ES256 (NIST P-256) 签名，typ=dpop+jwt，包含 publicKeyJwk
     - Token API: /v1/oauth2/tokens

特性约束：
  - 端口复用：回调 URL 使用当前网关项目自身的运行端口，不开启外部临时端口。
  - 会话隔离：保存于 accounts/codearts_account.json，拥有独立的 refresh_token 链，
    与桌面客户端 IDE 完全独立，互不影响、互不抢占。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DEFAULT_ACCOUNT_PATH = os.path.join(PROJECT_ROOT, "accounts", "codearts_account.json")

PORTAL_HOST = "https://codearts.huaweicloud.com/portal"
IAM_STS_HOST = "https://sts.cn-north-4.myhuaweicloud.com"
IAM_TOKEN_PATH = "/v1/oauth2/tokens"
AUTH_REDIRECT_PATH = "/oauth/callback"
CLIENT_ID = "codearts-agent"
CODE_CHALLENGE_METHOD = "SHA-256"


def b64url_encode(data: bytes) -> str:
    """Base64URL 编码（去掉末尾 '=' 补位）。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(s: str) -> bytes:
    """Base64URL 解码。"""
    s = s.strip()
    rem = len(s) % 4
    if rem:
        s += "=" * (4 - rem)
    return base64.urlsafe_b64decode(s)


# ==============================================================================
# 1. PKCE (Proof Key for Code Exchange)
# ==============================================================================

def generate_pkce() -> Tuple[str, str]:
    """生成符合 CodeArts Agent 规范的 PKCE 密钥对。

    verifier: 128 位十六进制字符（64 字节随机数）
    challenge: SHA-256(verifier) 的 Base64URL
    """
    verifier = secrets.token_bytes(64).hex()  # 128 hex chars
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = b64url_encode(digest)
    return verifier, challenge


def build_authorize_url(port: int, challenge: str) -> str:
    """拼接华为云 CodeArts Agent 官方授权页面 URL。

    回调直接引导浏览器访问本项目的运行端口：
    http://127.0.0.1:{port}/oauth/callback?code=...
    """
    params = {
        "theme": "2",
        "locale": "zh-cn",
        "uri_scheme": CLIENT_ID,
        "client_id": CLIENT_ID,
        "port": str(port),
        "code_challenge": challenge,
        "code_challenge_method": CODE_CHALLENGE_METHOD,
    }
    return f"{PORTAL_HOST}/authorize?{urllib.parse.urlencode(params)}"


# ==============================================================================
# 2. DPoP (Demonstrating Proof-of-Possession) 密钥对与 JWT 证明
# ==============================================================================

def generate_dpop_key_pair() -> Dict[str, Any]:
    """生成 NIST P-256 (ES256) 椭圆曲线密钥对，并导出为 JWK 格式。"""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    pn = public_key.public_numbers()
    x_bytes = pn.x.to_bytes(32, byteorder="big")
    y_bytes = pn.y.to_bytes(32, byteorder="big")

    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url_encode(x_bytes),
        "y": b64url_encode(y_bytes),
    }

    d_bytes = private_key.private_numbers().private_value.to_bytes(32, byteorder="big")
    private_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": public_jwk["x"],
        "y": public_jwk["y"],
        "d": b64url_encode(d_bytes),
    }

    return {
        "publicKeyJwk": public_jwk,
        "privateKeyJwk": private_jwk,
    }


def export_jwk_from_private_key(private_key: ec.EllipticCurvePrivateKey) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """从 private_key 导出 privateKeyJwk 与 publicKeyJwk 字典。"""
    pn = private_key.public_key().public_numbers()
    x_bytes = pn.x.to_bytes(32, byteorder="big")
    y_bytes = pn.y.to_bytes(32, byteorder="big")
    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url_encode(x_bytes),
        "y": b64url_encode(y_bytes),
    }
    d_bytes = private_key.private_numbers().private_value.to_bytes(32, byteorder="big")
    private_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": public_jwk["x"],
        "y": public_jwk["y"],
        "d": b64url_encode(d_bytes),
    }
    return private_jwk, public_jwk


def restore_private_key_from_jwk(private_jwk: Dict[str, Any]) -> ec.EllipticCurvePrivateKey:
    """从 JWK 字典恢复 private_key 对象。"""
    d_int = int.from_bytes(b64url_decode(private_jwk["d"]), byteorder="big")
    return ec.derive_private_key(d_int, ec.SECP256R1())


import_private_key_from_jwk = restore_private_key_from_jwk

def create_dpop_proof(dpop_key_pair: Dict[str, Any], htm: str, htu: str) -> str:
    """生成符合 RFC 9449 与华为云 IAM STS 要求的 DPoP 证明 JWT。

    Header:
      {"alg": "ES256", "typ": "dpop+jwt", "jwk": publicKeyJwk}
    Payload:
      {"htm": htm, "htu": htu, "iat": now, "jti": random_hex(32)}
    Signature:
      IEEE P1363 格式原始 64 字节签名 (r || s)，Base64URL 编码。
    """
    header = {
        "alg": "ES256",
        "typ": "dpop+jwt",
        "jwk": dpop_key_pair["publicKeyJwk"],
    }
    payload = {
        "htm": htm,
        "htu": htu,
        "iat": int(time.time()),
        "jti": secrets.token_hex(32),
    }

    header_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    private_key = restore_private_key_from_jwk(dpop_key_pair["privateKeyJwk"])
    sig_der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(sig_der)
    raw_sig = r.to_bytes(32, byteorder="big") + s.to_bytes(32, byteorder="big")

    return f"{signing_input.decode('ascii')}.{b64url_encode(raw_sig)}"


# ==============================================================================
# 3. 内存中待处理的 OAuth 握手上下文管理
# ==============================================================================

class _PendingOAuthManager:
    """管理并发起或等待回调的 OAuth 状态。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._latest_state: Optional[str] = None
        self._last_result: Optional[Dict[str, Any]] = None

    def start_login(self, port: int) -> Dict[str, Any]:
        with self._lock:
            # 清理超过 10 分钟的历史状态
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if now - v["created_at"] < 600}

            verifier, challenge = generate_pkce()
            dpop_pair = generate_dpop_key_pair()
            state = secrets.token_hex(16)
            login_url = build_authorize_url(port, challenge)

            entry = {
                "state": state,
                "verifier": verifier,
                "challenge": challenge,
                "dpop_pair": dpop_pair,
                "port": port,
                "created_at": now,
            }
            self._pending[state] = entry
            self._latest_state = state
            self._last_result = {"status": "pending", "state": state}

            return {
                "login_url": login_url,
                "state": state,
                "port": port,
            }

    def pop_pending(self, state: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            if state and state in self._pending:
                return self._pending.pop(state)
            if self._latest_state and self._latest_state in self._pending:
                st = self._latest_state
                self._latest_state = None
                return self._pending.pop(st)
            # 兜底返回最新的一个
            if self._pending:
                latest_k = max(self._pending.keys(), key=lambda k: self._pending[k]["created_at"])
                return self._pending.pop(latest_k)
            return None

    def record_result(self, result: Dict[str, Any]):
        with self._lock:
            self._last_result = result

    def get_last_result(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._last_result


_OAUTH_MGR = _PendingOAuthManager()


def start_oauth_login(port: int) -> Dict[str, Any]:
    """启动 OAuth 登录流程，返回供前端调用的登录 URL 及相关参数。"""
    return _OAUTH_MGR.start_login(port)


def get_oauth_poll_result() -> Optional[Dict[str, Any]]:
    """前端轮询登录结果。"""
    return _OAUTH_MGR.get_last_result()


# ==============================================================================
# 4. Token 交换与会话持久化
# ==============================================================================

def _parse_jwt_payload_unverified(jwt_token: str) -> Dict[str, Any]:
    """解析 JWT 的 Payload（不验证签名，仅用于读取华为云返回的用户基本信息）。"""
    parts = (jwt_token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = b64url_decode(parts[1])
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}

def _urlopen(req: urllib.request.Request, timeout: int = 30, proxy: str = ""):
    """带可选代理支持的 urlopen。用于确保 STS 换取与刷新与账号出口代理 IP 强一致。"""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def exchange_code_for_token(code: str, port: int, verifier: str,
                           dpop_pair: Dict[str, Any],
                           timeout: int = 30,
                           proxy: str = "") -> Dict[str, Any]:
    """使用授权码换取 STS 临时凭据及独立 refresh_token。"""
    token_url = f"{IAM_STS_HOST}{IAM_TOKEN_PATH}"
    redirect_uri = f"http://127.0.0.1:{port}{AUTH_REDIRECT_PATH}"

    dpop_jwt = create_dpop_proof(dpop_pair, "POST", token_url)

    params = {
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    body_data = urllib.parse.urlencode(params).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=body_data,
        headers={
            "DPoP": dpop_jwt,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "codearts-agent",
        },
        method="POST"
    )

    try:
        with _urlopen(req, timeout=timeout, proxy=proxy) as resp:
            resp_body = resp.read().decode("utf-8")
            data = json.loads(resp_body)
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HuaweiCloud STS Token exchange failed (HTTP {exc.code}): {err_msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"HuaweiCloud STS network request failed: {exc}") from exc

    return _process_token_response(data, verifier, dpop_pair)


def refresh_sts_token(account_path: str = DEFAULT_ACCOUNT_PATH, timeout: int = 30, proxy: str = "") -> Dict[str, Any]:
    """使用保存的独立 refresh_token 刷新 STS 临时凭据。"""
    if not os.path.isfile(account_path):
        raise FileNotFoundError(f"Account file not found: {account_path}")

    with open(account_path, "r", encoding="utf-8") as f:
        stored = json.load(f)

    refresh_token = stored.get("refresh_token") or stored.get("refreshToken")
    login_context = stored.get("loginContext") or {}
    pkce_pair = login_context.get("pkcePair") or {}
    code_verifier = pkce_pair.get("codeVerifier")
    dpop_pair = login_context.get("dpopKeyPair")

    if not refresh_token or not dpop_pair or not code_verifier:
        raise ValueError("Stored CodeArts session lacks refresh_token, dpopKeyPair, or codeVerifier")

    token_url = f"{IAM_STS_HOST}{IAM_TOKEN_PATH}"
    dpop_jwt = create_dpop_proof(dpop_pair, "POST", token_url)

    params = {
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    body_data = urllib.parse.urlencode(params).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=body_data,
        headers={
            "DPoP": dpop_jwt,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "codearts-agent",
        },
        method="POST"
    )

    try:
        with _urlopen(req, timeout=timeout, proxy=proxy) as resp:
            resp_body = resp.read().decode("utf-8")
            data = json.loads(resp_body)
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HuaweiCloud STS Token refresh failed (HTTP {exc.code}): {err_msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"HuaweiCloud STS network request failed: {exc}") from exc

    return _process_token_response(data, code_verifier, dpop_pair, save_path=account_path)


def refresh_sts_token_for_account(account: Any, timeout: int = 30, proxy: str = "") -> Dict[str, Any]:
    """针对指定 CodeArtsAccount 实例执行 STS 刷新，并使用绑定的 proxy 发起网络请求。"""
    refresh_token = getattr(account, "refresh_token", "")
    login_context = getattr(account, "login_context", {}) or {}
    pkce_pair = login_context.get("pkcePair") or {}
    code_verifier = pkce_pair.get("codeVerifier") or login_context.get("codeVerifier")
    dpop_pair = login_context.get("dpopKeyPair")
    if not refresh_token or not dpop_pair or not code_verifier:
        raise ValueError(
            f"Account {getattr(account, 'label', 'unknown')} lacks refresh_token, dpopKeyPair, or codeVerifier"
        )

    token_url = f"{IAM_STS_HOST}{IAM_TOKEN_PATH}"
    dpop_jwt = create_dpop_proof(dpop_pair, "POST", token_url)

    params = {
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    body_data = urllib.parse.urlencode(params).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=body_data,
        headers={
            "DPoP": dpop_jwt,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "codearts-agent",
        },
        method="POST"
    )

    effective_proxy = proxy or getattr(account, "proxy", "")
    try:
        with _urlopen(req, timeout=timeout, proxy=effective_proxy) as resp:
            resp_body = resp.read().decode("utf-8")
            data = json.loads(resp_body)
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HuaweiCloud STS Token refresh failed (HTTP {exc.code}): {err_msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"HuaweiCloud STS network request failed: {exc}") from exc

    creds = data.get("credentials") or data.get("token") or {}
    access_key = creds.get("access_key_id") or creds.get("accessKey") or creds.get("access_key") or ""
    secret_key = creds.get("secret_access_key") or creds.get("secretKey") or creds.get("secret_key") or ""
    security_token = creds.get("security_token") or creds.get("securitytoken") or creds.get("securityToken") or ""
    expires_at = creds.get("expiration") or creds.get("expires_at") or creds.get("expiresAt") or ""
    new_refresh_token = data.get("refresh_token") or refresh_token

    if not access_key or not secret_key:
        raise ValueError(f"STS response does not contain valid credentials: {data}")

    account.access_key = access_key
    account.secret_key = secret_key
    account.security_token = security_token
    account.expires_at = expires_at
    account.refresh_token = new_refresh_token
    if "safelyRenewTokenInterval" in data:
        account.safely_renew_token_interval = int(data["safelyRenewTokenInterval"])

    return data
def _process_token_response(data: Dict[str, Any], verifier: str,
                           dpop_pair: Dict[str, Any],
                           save_path: str = DEFAULT_ACCOUNT_PATH) -> Dict[str, Any]:
    """处理华为云 Token 响应并保存为 accounts/codearts_account.json。"""
    creds = data.get("credentials") or {}
    access_key = creds.get("access_key_id") or creds.get("accessKey") or ""
    secret_key = creds.get("secret_access_key") or creds.get("secretKey") or ""
    security_token = creds.get("security_token") or creds.get("securitytoken") or ""
    expires_at = creds.get("expiration") or creds.get("expires_at") or ""
    refresh_token = data.get("refresh_token") or ""

    if not access_key or not secret_key:
        raise ValueError(f"STS response does not contain valid credentials: {data}")

    # 从 refresh_token 的 JWT payload 中提取用户信息
    rt_payload = _parse_jwt_payload_unverified(refresh_token)
    user_prof_raw = rt_payload.get("user_profile")
    user_profile = {}
    if user_prof_raw:
        try:
            user_profile = json.loads(b64url_decode(user_prof_raw).decode("utf-8"))
        except Exception:
            pass

    domain_id = user_profile.get("account_id") or data.get("domainId") or ""
    user_id = user_profile.get("principal_id") or ""
    user_name = user_profile.get("account_name") or ""

    account_label = user_name or "HuaweiCloudUser"

    session_data = {
        "account": {
            "id": user_id,
            "label": account_label,
        },
        "scopes": [],
        "accessToken": "",
        "accessKey": access_key,
        "secretKey": secret_key,
        "securitytoken": security_token,
        "expires_at": expires_at,
        "domainId": domain_id,
        "safelyRenewTokenInterval": data.get("safelyRenewTokenInterval", 3600),
        "loginContext": {
            "pkcePair": {
                "codeVerifier": verifier,
                "codeChallenge": b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest()),
                "codeChallengeMethod": CODE_CHALLENGE_METHOD,
            },
            "dpopKeyPair": dpop_pair,
        },
        "refresh_token": refresh_token,
        "source": "web_oauth",
        "updated_at": int(time.time()),
    }

    # 原子写入 accounts/codearts_account.json
    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
    tmp_path = save_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, save_path)

    # 同步写入/更新多账号池 accounts/codearts_accounts.json
    try:
        from codearts import codearts_accounts
        accounts_dir = os.path.dirname(os.path.abspath(save_path))
        pool = codearts_accounts.CodeArtsAccountPool(accounts_dir)
        acct = codearts_accounts.CodeArtsAccount(session_data)
        pool.add(acct)
    except Exception:
        pass

    return session_data


# ==============================================================================
# 5. 回调入口处理函数（供主网关直接调用）
# ==============================================================================

def handle_oauth_callback(query_params: Dict[str, list], project_port: int) -> Tuple[bool, str, Dict[str, Any]]:
    """主网关 GET /oauth/callback 处理入口。

    返回: (成功布尔值, 响应HTML或错误消息, 登录结果数据)
    """
    error_list = query_params.get("error") or []
    if error_list:
        desc_list = query_params.get("error_description") or []
        err = f"{error_list[0]}: {desc_list[0]}" if desc_list else error_list[0]
        _OAUTH_MGR.record_result({"status": "failed", "error": err, "ok": False})
        return False, _build_html_page(False, err), {"status": "failed", "error": err, "ok": False}

    code_list = query_params.get("code") or []
    code = code_list[0] if code_list else ""
    if not code:
        err = "Missing authorization code in callback parameters."
        _OAUTH_MGR.record_result({"status": "failed", "error": err, "ok": False})
        return False, _build_html_page(False, err), {"status": "failed", "error": err, "ok": False}
    pending = _OAUTH_MGR.pop_pending()
    if not pending:
        err = "No pending OAuth login task found (or login request timed out)."
        _OAUTH_MGR.record_result({"ok": False, "error": err})
        return False, _build_html_page(False, err), {"error": err}

    try:
        session_data = exchange_code_for_token(
            code=code,
            port=pending.get("port") or project_port,
            verifier=pending["verifier"],
            dpop_pair=pending["dpop_pair"]
        )
        acc_label = session_data.get("account", {}).get("label") or "HuaweiCloudUser"
        result = {
            "status": "success",
            "ok": True,
            "account": acc_label,
            "domainId": session_data.get("domainId"),
            "expiresAt": session_data.get("expires_at"),
            "source": "web_oauth",
        }
        _OAUTH_MGR.record_result(result)
        return True, _build_html_page(True, f"欢迎，{acc_label}！华为云 CodeArts Agent 凭据已成功导入网关。"), result
    except Exception as exc:
        err = str(exc)
        res = {"status": "failed", "ok": False, "error": err}
        _OAUTH_MGR.record_result(res)
        return False, _build_html_page(False, f"换取凭据失败: {err}"), res


def _build_html_page(success: bool, message: str) -> str:
    """生成给浏览器回调渲染的美观提示页。"""
    title = "登录成功" if success else "登录失败"
    icon = "✓" if success else "✗"
    color = "#10b981" if success else "#ef4444"
    bg_gradient = "linear-gradient(135deg, #064e3b 0%, #0f172a 100%)" if success else "linear-gradient(135deg, #450a0a 0%, #0f172a 100%)"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CodeArts 授权结果 - {title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: {bg_gradient};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    color: #f8fafc;
    padding: 20px;
  }}
  .card {{
    background: rgba(30, 41, 59, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.1);
    backdrop-filter: blur(12px);
    border-radius: 16px;
    padding: 40px 32px;
    max-width: 480px;
    width: 100%;
    text-align: center;
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  }}
  .icon {{
    width: 64px;
    height: 64px;
    border-radius: 50%;
    background: {color};
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 32px;
    margin: 0 auto 20px;
    box-shadow: 0 0 20px {color}88;
  }}
  h1 {{ font-size: 22px; margin-bottom: 12px; font-weight: 600; }}
  p {{ font-size: 14px; line-height: 1.6; color: #94a3b8; margin-bottom: 24px; }}
  .close-tip {{ font-size: 12px; color: #64748b; }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <div class="close-tip">网关已捕获授权凭据，您可以安全关闭此标签页。</div>
  </div>
  <script>
    setTimeout(function() {{
      try {{ window.close(); }} catch(e) {{}}
    }}, 4000);
  </script>
</body>
</html>
"""
