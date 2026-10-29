"""读取本机 CodeArts Agent 的登录态 —— 纯 Python，只读。

凭据链（与 Chromium / 豆包桌面端同一套 OSCrypt）：

    %APPDATA%\\codearts-agent\\Local State
      os_crypt.encrypted_key = base64("DPAPI" + DPAPI(32 字节 AES 主密钥))

    %APPDATA%\\codearts-agent\\User\\globalStorage\\state.vscdb   (SQLite)
      ItemTable['secret://{"extensionId":"huaweicloud.authentication",
                            "key":"HuaweiCloudSession"}']
        value = {"type":"Buffer","data":["v10" | nonce(12) | AES-256-GCM(ct||tag)]}
      → 明文 JSON: accessKey / secretKey / securitytoken / expires_at /
                   refresh_token / loginContext(dpopKeyPair, pkcePair)

本模块**只读**：不解密就写回，不碰 refresh_token。因为 refresh_token 是
单次轮转且 DPoP 绑定（JWT 的 ``cnf.jkt``），网关先续期而不写回会让 IDE 的
登录当场失效（上游 ``STS5.1806`` = refresh_token already used）。

非 Windows 平台没有 DPAPI，无法自动读取 —— 那时只能手工导入会话 JSON。

自测：``python codearts/codearts_session.py``
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import sqlite3
from dataclasses import dataclass

SECRET_ITEM_KEY = ('secret://{"extensionId":"huaweicloud.authentication",'
                   '"key":"HuaweiCloudSession"}')

# OSCrypt 密文前缀与 AES-GCM 参数
_V10 = b"v10"
_NONCE_LEN = 12
_TAG_LEN = 16

DEFAULT_USER_DATA = os.path.join(os.environ.get("APPDATA") or "", "codearts-agent")


class SessionMissing(Exception):
    """本机没有 CodeArts Agent 登录态（未安装、未登录，或平台不支持）。"""


class SessionUndecryptable(Exception):
    """找到了密文但解不开 —— 通常是换了 Windows 账户，或 Local State 已重写。"""


@dataclass
class CodeArtsSession:
    access_key: str
    secret_key: str
    security_token: str = ""
    expires_at: str = ""
    refresh_token: str = ""
    domain_id: str = ""
    account_label: str = ""
    source: str = "desktop"

    @property
    def has_sts(self):
        return bool(self.security_token)

    def masked(self):
        """给看板用的脱敏视图；永不返回 secret_key 全文。"""
        return {
            "account": self.account_label,
            "accessKey": (self.access_key[:6] + "…" + self.access_key[-4:]
                          if len(self.access_key) > 12 else "…"),
            "hasSecretKey": bool(self.secret_key),
            "hasSts": self.has_sts,
            "expiresAt": self.expires_at,
            "domainId": (self.domain_id[:8] + "…") if self.domain_id else "",
            "source": self.source,
        }


def _dpapi_unprotect(blob):
    if os.name != "nt":
        raise SessionMissing(
            "DPAPI is Windows-only; on this platform import a session JSON by hand")
    import ctypes
    import ctypes.wintypes as wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def as_blob(data):
        return _Blob(len(data), ctypes.cast(
            ctypes.create_string_buffer(data, len(data)),
            ctypes.POINTER(ctypes.c_char)))

    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(as_blob(blob)), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise SessionUndecryptable(
            "DPAPI could not decrypt Local State (different Windows user?)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def local_state_master_key(user_data=None):
    """从 Local State 取出 Chromium 的 32 字节 AES 主密钥。"""
    path = os.path.join(user_data or DEFAULT_USER_DATA, "Local State")
    if not os.path.isfile(path):
        raise SessionMissing("CodeArts Agent Local State not found at %s" % path)
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    encoded = ((state.get("os_crypt") or {}).get("encrypted_key") or "").strip()
    if not encoded:
        raise SessionMissing("Local State has no os_crypt.encrypted_key")
    raw = base64.b64decode(encoded)
    if not raw.startswith(b"DPAPI"):
        raise SessionUndecryptable(
            "encrypted_key is not DPAPI-wrapped (unsupported platform)")
    return _dpapi_unprotect(raw[5:])


def _aes_gcm_decrypt(key, payload):
    if len(payload) < len(_V10) + _NONCE_LEN + _TAG_LEN:
        raise SessionUndecryptable("ciphertext too short")
    if not payload.startswith(_V10):
        raise SessionUndecryptable("ciphertext is not v10-prefixed")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = payload[len(_V10):len(_V10) + _NONCE_LEN]
    body = payload[len(_V10) + _NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, body, None)
    except Exception as exc:
        raise SessionUndecryptable("AES-GCM decrypt failed: %s" % exc) from exc


def _read_secret_blob(user_data):
    path = os.path.join(user_data or DEFAULT_USER_DATA,
                        "User", "globalStorage", "state.vscdb")
    if not os.path.isfile(path):
        raise SessionMissing("state.vscdb not found at %s" % path)
    # 只读模式 + immutable：不拿写锁，绝不改动 IDE 正在使用的库。
    uri = "file:%s?mode=ro&immutable=1" % path.replace("\\", "/").replace("#", "%23")
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=3)
    except sqlite3.Error as exc:
        raise SessionMissing("cannot open state.vscdb read-only: %s" % exc) from exc
    try:
        row = connection.execute(
            "select value from ItemTable where key = ?", (SECRET_ITEM_KEY,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise SessionMissing(
            "no CodeArts login in state.vscdb (open the IDE and sign in first)")
    return row[0]


def _parse_session(plain, source="desktop"):
    if isinstance(plain, (bytes, bytearray)):
        plain = bytes(plain).decode("utf-8", "replace")
    try:
        data = json.loads(plain)
    except ValueError as exc:
        raise SessionUndecryptable("session payload is not JSON: %s" % exc) from exc
    if not isinstance(data, dict):
        raise SessionUndecryptable("session payload is not an object")
    access_key = str(data.get("accessKey") or "").strip()
    secret_key = str(data.get("secretKey") or "").strip()
    if not access_key or not secret_key:
        raise SessionUndecryptable(
            "session has no accessKey/secretKey (IDE not fully signed in?)")
    account = data.get("account") or {}
    return CodeArtsSession(
        access_key=access_key,
        secret_key=secret_key,
        # 上游在不同版本里用了两种大小写，两边都认。
        security_token=str(data.get("securitytoken")
                           or data.get("securityToken") or "").strip(),
        expires_at=str(data.get("expires_at") or data.get("expiresAt") or "").strip(),
        refresh_token=str(data.get("refresh_token")
                          or data.get("refreshToken") or "").strip(),
        domain_id=str(data.get("domainId") or "").strip(),
        account_label=str((account or {}).get("label") or "").strip(),
        source=source,
    )


DEFAULT_WEB_ACCOUNT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "accounts",
    "codearts_account.json"
)
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config.json"
)


def load_desktop_session(user_data=None):
    """读取并解密本机 CodeArts Agent 桌面端登录态（只读）。"""
    master = local_state_master_key(user_data)
    stored = _read_secret_blob(user_data)
    if isinstance(stored, bytes):
        stored = stored.decode("utf-8", "replace")
    try:
        wrapped = json.loads(stored)
        blob = bytes(wrapped["data"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SessionUndecryptable("secret wrapper is malformed: %s" % exc) from exc
    return _parse_session(_aes_gcm_decrypt(master, blob), source="desktop")


def load_web_session(account_path=DEFAULT_WEB_ACCOUNT_PATH, auto_refresh=True):
    """读取独立 Web OAuth 授权账号，并在临近过期时自动通过 DPoP 续期。"""
    if not os.path.isfile(account_path):
        return None
    try:
        session = load_session_file(account_path)
    except Exception:
        return None

    # 如果包含 STS 且已过期或濒临过期（< 60秒），尝试自动续期
    if auto_refresh and session.refresh_token:
        stamp = expires_at_datetime(session)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if stamp is not None and (stamp - now_utc).total_seconds() < 60:
            try:
                import codearts_oauth
                codearts_oauth.refresh_sts_token(account_path)
                session = load_session_file(account_path)
            except Exception:
                # 续期失败保留原 session
                pass

    session.source = "web_oauth"
    return session


def load_config_ak_sk(config_path=CONFIG_PATH):
    """读取 config.json 中的永久 AK/SK 配置。"""
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ca_cfg = cfg.get("codearts") or {}
        ak = str(ca_cfg.get("ak") or "").strip()
        sk = str(ca_cfg.get("sk") or "").strip()
        if ak and sk:
            return CodeArtsSession(
                access_key=ak,
                secret_key=sk,
                account_label=ca_cfg.get("account") or "ConfigAKSK",
                source="config_ak_sk",
            )
    except Exception:
        pass
    return None


def load_session(user_data=None):
    """按优先级顺序获取 CodeArts 鉴权会话：
    1. 独立 Web OAuth 凭据 (accounts/codearts_account.json)
    2. config.json 静态 AK/SK
    3. 桌面 IDE DPAPI 解密 (state.vscdb)
    """
    if user_data is not None:
        return load_desktop_session(user_data)

    web_session = load_web_session()
    if web_session is not None:
        return web_session

    cfg_session = load_config_ak_sk()
    if cfg_session is not None:
        return cfg_session

    return load_desktop_session()

def load_session_file(path):
    """从导出的会话 JSON 读入（非 Windows 或手工导入时使用）。"""
    with open(path, encoding="utf-8") as handle:
        return _parse_session(handle.read(), source="imported")


def expires_at_datetime(session):
    """``expires_at`` 解析成 aware datetime；缺失或无法解析返回 None。

    上游格式是 ``2026-10-29T03:34:23.406Z``。解析失败按「不知道」处理，
    而不是当成已过期 —— 误报过期会让一条本来能用的请求被拒绝。
    """
    raw = (session.expires_at or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


def is_expired(session, now=None):
    """STS 是否已过期。无法判断时返回 False。"""
    stamp = expires_at_datetime(session)
    if stamp is None:
        return False
    return stamp <= (now or datetime.datetime.now(datetime.timezone.utc))
