# Trae OAuth 登录流程 —— 对齐 TraeWorkAssistant oauth.rs（2026-09-16 逆向实锤协议）
#
# 真实流程（login_channel=native_ide）：
# 1. 生成登录 URL（PKCE + 设备参数）→ 浏览器打开 https://www.trae.cn/authorization?...
# 2. 授权页登录成功 → 302 回 auth_callback_url，回调参数为
#    authCodeInfo=<URL编码JSON>{"AuthCode","ExpireAt","ExpireDuration"}
#    userInfo=<URL编码JSON>{"UserID","ScreenName","AvatarUrl",...}
#    loginTraceID（=发起时的 login_trace_id）、host（API 域，决定交换端点）
#    —— 旧形态 refreshToken=... 查询参数已不被回传
# 3. AuthCode 交换：POST ${host}/trae/api/v3/oauth/ExchangeToken
#    主变体 payload = {ClientID, AuthCode, CodeVerifier, DeviceInfo{...}, IDEVersion}
#    DeviceInfo 来自本机 Trae IDE 的 icube 设备凭证（tc 信封解密，见 trae_icube.py）
#    请求头：Content-Type + x-cloudide-token:"" + x-device-id + x-app-id + x-platform-code
#    响应：火山信封 ResponseMetadata.Error.{Code,...} / Result.{Token,RefreshToken}
# 4. 旧形态变体（RefreshToken + ClientSecret @ api.trae.com.cn）保留为兜底探测

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Tuple, Any

import trae_icube

# OAuth 常量（2026-09-16 抓包固化，与 TraeWorkAssistant oauth.rs 对齐）
OAUTH_CLIENT_ID = "ono9krqynydwx5"
OAUTH_CLIENT_SECRET = "-"
OAUTH_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
OAUTH_PAGE_PLUGIN_VERSION = "2.3.83560"
OAUTH_PAGE_APP_VERSION = "3.3.100"
OAUTH_PLATFORM_CODE = "IDE_PC"
OAUTH_LOOPBACK_PORT = 17388
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_LOOPBACK_PORT}/authorize"
OAUTH_EXCHANGE_URL = "https://api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken"
OAUTH_DEFAULT_HOST = "https://api.trae.com.cn"

# OAuth 状态存储
_pending_login: Dict[str, dict] = {}
_callback_result: Optional[dict] = None
_callback_server: Optional[HTTPServer] = None

# trae 系域名区域封锁（海外出口 IP 返回 403「区域不支持」）：显式直连走本机中国出口
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_post_json(url: str, payload: dict, headers: Dict[str, str], timeout: int = 30) -> dict:
    """POST JSON。4xx/5xx 时读响应体（火山信封错误结构在 body 里，需要解析）。"""
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            raise RuntimeError(f"HTTP {e.code}: {raw[:200]}")


def pkce_pair() -> Tuple[str, str]:
    """RFC 7636：hex-64 verifier + S256 challenge（BASE64URL-NOPAD）。"""
    verifier = secrets.token_hex(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def generate_oauth_login_url(device_info: dict) -> Tuple[str, str]:
    """生成 OAuth 登录 URL（PKCE + 设备参数，对齐真实 Trae IDE 抓包形态）。"""
    trace_id = secrets.token_hex(16)
    pkce_verifier, code_challenge = pkce_pair()

    hostname = device_info.get("hostname", "Windows-PC")
    # 设备标识优先用本机 Trae IDE 的 icube 凭证（与交换协议 DeviceInfo 同源，
    # 否则服务端 20403/20405 设备不匹配）
    creds = trae_icube.get_device_credentials()
    if creds:
        device_id = creds[0].device_id
        machine_id = creds[0].machine_id or device_info.get("machine_id", "") or secrets.token_hex(16)
    else:
        device_id = device_info.get("device_id", "") or secrets.token_hex(16)
        machine_id = device_info.get("machine_id", "") or secrets.token_hex(16)
    os_version = device_info.get("os_version", "Windows 11")

    params = {
        "login_version": "1",
        "auth_from": "trae",
        "login_channel": "native_ide",
        "plugin_version": OAUTH_PAGE_PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": OAUTH_CLIENT_ID,
        "redirect": "0",
        "login_trace_id": trace_id,
        "auth_callback_url": OAUTH_REDIRECT_URI,
        "machine_id": machine_id,
        "device_id": device_id,
        "x_device_id": device_id,
        "x_machine_id": machine_id,
        "x_device_brand": hostname,
        "x_device_type": "windows",
        "x_os_version": os_version,
        "x_env": "",
        "x_app_version": OAUTH_PAGE_APP_VERSION,
        "x_app_type": "stable",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "channel_name": "common",
    }

    login_url = f"https://www.trae.cn/authorization?{urllib.parse.urlencode(params)}"

    _pending_login[trace_id] = {
        "state": trace_id,
        "pkce_verifier": pkce_verifier,
        "device_id": device_id,
        "device_info": device_info,
        "created_at": time.time(),
    }

    return login_url, trace_id


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """OAuth 回调处理器：解析 authCodeInfo（新协议）+ refreshToken（旧形态兜底）"""

    def do_GET(self):
        global _callback_result

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/authorize":
            self.send_response(404)
            self.end_headers()
            return

        query = urllib.parse.parse_qs(parsed.query)

        def q(name: str) -> str:
            return query.get(name, [""])[0]

        result: Dict[str, Any] = {}

        # 新协议主路径：authCodeInfo=<URL编码JSON>{"AuthCode","ExpireAt","ExpireDuration"}
        raw_code_info = q("authCodeInfo")
        if raw_code_info:
            try:
                info = json.loads(raw_code_info)
                result["auth_code"] = info.get("AuthCode", "")
                result["auth_expire_at"] = info.get("ExpireAt", "")
            except Exception:
                result["auth_code_raw"] = raw_code_info
        # 旧形态兜底：refreshToken/code 直传
        if not result.get("auth_code"):
            if q("refreshToken"):
                result["refresh_token"] = q("refreshToken")
            elif q("code"):
                result["auth_code"] = q("code")

        # userInfo=<JSON>{"UserID","ScreenName","AvatarUrl",...}
        raw_user = q("userInfo")
        if raw_user:
            try:
                ui = json.loads(raw_user)
                result["user_id"] = ui.get("UserID", "") or ui.get("userId", "") or ui.get("uid", "") or q("UserID") or q("userId")
                result["user_name"] = ui.get("ScreenName", "") or ui.get("NickName", "") or ui.get("nickname", "") or q("userName")
                result["avatar"] = ui.get("AvatarUrl", "") or q("avatar")
            except Exception:
                pass
        result.setdefault("user_id", q("UserID") or q("userId"))
        result.setdefault("user_name", q("userName"))
        result.setdefault("avatar", q("avatar"))
        # uid 兜底：从 access_token JWT 解析 user_id/sub（回调未带时）
        if not result.get("user_id"):
            for cand in (result.get("access_token"), result.get("refresh_token")):
                if not cand or cand.count(".") < 2:
                    continue
                try:
                    part = cand.split(".")[1]
                    part += "=" * (-len(part) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(part).decode())
                    uid = str(claims.get("user_id") or claims.get("sub") or claims.get("uid") or "")
                    if uid:
                        result["user_id"] = uid
                        break
                except Exception:
                    continue

        # 交换 API 域（授权页回传，决定 ExchangeToken 端点）
        result["host"] = q("host") or OAUTH_DEFAULT_HOST
        # CSRF：授权页把 login_trace_id 原样回传为 loginTraceID
        result["state"] = q("loginTraceID") or q("login_trace_id")
        result["pkce_verifier"] = _lookup_verifier(result["state"])

        _callback_result = result

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = """
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"><title>授权成功</title></head>
        <body style="text-align:center;padding:50px;font-family:sans-serif">
            <h2>✅ 授权成功</h2>
            <p>已获取 Trae 登录凭证，请关闭此页面。</p>
            <script>window.close();</script>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))

        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, format, *args):
        pass


def _lookup_verifier(login_trace_id: str) -> str:
    """按 loginTraceID 找回发起时签发的 PKCE verifier；找不到时退回最新一条。"""
    if login_trace_id and login_trace_id in _pending_login:
        return _pending_login[login_trace_id]["pkce_verifier"]
    if _pending_login:
        latest = max(_pending_login.values(), key=lambda p: p.get("created_at", 0))
        return latest["pkce_verifier"]
    return ""


def start_callback_server() -> Tuple[HTTPServer, int]:
    """启动回调监听服务器"""
    server = HTTPServer(("127.0.0.1", OAUTH_LOOPBACK_PORT), OAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, OAUTH_LOOPBACK_PORT


def wait_for_callback(timeout: int = 120) -> Optional[dict]:
    """等待 OAuth 回调"""
    global _callback_result
    start = time.time()
    while time.time() - start < timeout:
        if _callback_result is not None:
            result = _callback_result
            _callback_result = None
            return result
        time.sleep(0.5)
    return None


# token 提取键（对齐 oauth.rs：容器精确键 → 顶层）
_ACCESS_KEYS = ("AccessToken", "access_token", "token", "Jwt", "JWT")
_REFRESH_KEYS = ("RefreshToken", "refresh_token")


def _find_token(container: dict, keys) -> str:
    for k in keys:
        v = container.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _deep_find_tokens(node: Any, path: str, found: Dict[str, str]) -> None:
    """全树键名大小写不敏感找 token 对。首个命中即停（嵌套容器优先浅层）。"""
    if not isinstance(node, dict):
        return
    access: Optional[str] = None
    refresh: Optional[str] = None
    for k, v in node.items():
        lk = k.lower()
        if access is None and isinstance(v, str) and v and any(lk == t.lower() for t in _ACCESS_KEYS):
            access = v
        if refresh is None and isinstance(v, str) and v and any(lk == t.lower() for t in _REFRESH_KEYS):
            refresh = v
    if access and refresh:
        found["access"] = access
        found["refresh"] = refresh
        return
    for k, v in node.items():
        if isinstance(v, dict):
            _deep_find_tokens(v, f"{path}/{k}", found)
            if "access" in found:
                return


def _collect_key_paths(node: Any, path: str, out: list, depth: int = 0) -> None:
    """收集全部键路径（脱敏：只记路径不记值），供诊断。"""
    if depth > 6 or not isinstance(node, dict):
        return
    for k, v in node.items():
        p = f"{path}/{k}"
        out.append(p)
        if isinstance(v, dict):
            _collect_key_paths(v, p, out, depth + 1)


def _volcano_error(body: dict) -> Optional[str]:
    meta = body.get("ResponseMetadata") or {}
    err = meta.get("Error") or {}
    code = err.get("Code")
    if code not in (None, "", 0, "0"):
        msg = err.get("Message", "未知错误")
        std = err.get("StandardCode", "")
        return f"code={code}/{std}: {msg}"
    legacy = body.get("code")
    if legacy not in (None, 0, "0"):
        return f"code={legacy}: {body.get('message', '未知错误')}"
    return None


def exchange_auth_code(auth_code: str, code_verifier: str, device_id: str, host: str) -> dict:
    """AuthCode → JWT（新协议主变体链，对齐 oauth.rs exchange_code）。

    返回 {"token", "refresh_token", "user_id", "expires_at"}，失败抛 RuntimeError。
    """
    creds = trae_icube.get_device_credentials()
    variants = []

    # 主变体：DeviceInfo（含 DevicePublicKey）+ IDEVersion，x-cloudide-token:""
    if creds:
        cred = creds[0]
        try:
            pub_pem = trae_icube.device_public_key_pem(cred)
        except Exception:
            pub_pem = ""
        variants.append((
            "ExchangeToken/DeviceInfo",
            f"{host.rstrip('/')}/trae/api/v3/oauth/ExchangeToken",
            {
                "ClientID": OAUTH_CLIENT_ID,
                "AuthCode": auth_code,
                "CodeVerifier": code_verifier,
                "DeviceInfo": {
                    "DeviceID": cred.device_id,
                    "MachineID": cred.machine_id,
                    "PlatformCode": OAUTH_PLATFORM_CODE,
                    "DeviceType": "PC",
                    "DeviceName": "",
                    "DeviceModel": "",
                    "ClientVersion": cred.app_version,
                    "DevicePublicKey": pub_pem,
                    "DeviceBrand": "",
                    "DeviceCPU": "",
                    "OSInfo": "",
                    "OSVersion": "",
                },
                "IDEVersion": cred.app_version,
            },
            True,
        ))

    # 兜底：旧形态探测变体（无 DeviceInfo/公钥）
    variants.append((
        "ExchangeToken/AuthCode",
        OAUTH_EXCHANGE_URL,
        {
            "ClientID": OAUTH_CLIENT_ID,
            "AuthCode": auth_code,
            "CodeVerifier": code_verifier,
            "DeviceID": device_id,
            "PlatformCode": OAUTH_PLATFORM_CODE,
        },
        False,
    ))

    errs = []
    for tag, url, payload, with_empty_token_header in variants:
        try:
            headers = {
                "accept": "*/*",
                "x-device-id": device_id,
                "x-app-id": OAUTH_APP_ID,
                "x-platform-code": OAUTH_PLATFORM_CODE,
            }
            if with_empty_token_header:
                headers["x-cloudide-token"] = ""
            body = _http_post_json(url, payload, headers)
        except Exception as e:
            errs.append(f"{tag}: 请求失败 {e}")
            continue

        err = _volcano_error(body)
        if err:
            errs.append(f"{tag}: {err}")
            continue

        container = body.get("Result") or body.get("data") or body.get("Data") or body
        token = _find_token(container, _ACCESS_KEYS)
        refresh = _find_token(container, _REFRESH_KEYS)
        if token and refresh:
            return {
                "token": token,
                "refresh_token": refresh,
                "user_id": "",
                "expires_at": time.time() + 3600 * 24 * 30,
            }
        # 三级深挖（对齐 oauth.rs）：全树键名大小写不敏感找 token 字段
        found: Dict[str, str] = {}
        _deep_find_tokens(body, "", found)
        if found.get("access") and found.get("refresh"):
            return {
                "token": found["access"],
                "refresh_token": found["refresh"],
                "user_id": "",
                "expires_at": time.time() + 3600 * 24 * 30,
            }
        paths = []
        _collect_key_paths(body, "", paths)
        errs.append(f"{tag}: 未找到 Token 字段；键路径: {paths[:20]}")

    raise RuntimeError("全部交换变体失败 → " + " | ".join(errs))


def exchange_token(refresh_token: str, device_info: dict) -> Optional[dict]:
    """旧协议兜底：RefreshToken + ClientSecret @ api.trae.com.cn（保留）。"""
    body = {
        "ClientID": OAUTH_CLIENT_ID,
        "RefreshToken": refresh_token,
        "ClientSecret": OAUTH_CLIENT_SECRET,
        "UserID": "",
    }
    headers = {
        "x-app-id": OAUTH_APP_ID,
        "x-device-id": device_info.get("device_id", ""),
        "x-device-brand": device_info.get("hostname", "Windows-PC"),
        "x-device-cpu": "Intel",
        "x-device-type": "windows",
        "x-machine-id": device_info.get("machine_id", ""),
        "x-os-version": device_info.get("os_version", "Windows 11"),
    }
    try:
        data = _http_post_json(OAUTH_EXCHANGE_URL, body, headers, timeout=30)
        result = data.get("Result", {})
        if result.get("Token"):
            return {
                "token": result["Token"],
                "user_id": result.get("UserID", ""),
                "expires_at": time.time() + 3600 * 24 * 30,
                "refresh_token": refresh_token,
            }
    except Exception as e:
        print(f"Token exchange failed: {e}")
    return None
