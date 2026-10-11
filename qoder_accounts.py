"""Qoder 账号体系 —— 纯 Python 移植 qoder2api/account。

覆盖：账号模型与文件存储、密钥存储、设置、本机指纹盐、OAuth 设备码登录、
额度/套餐查询。

数据目录默认 <本文件所在目录>/qoder_data，可用 QODER_DATA_ROOT 覆盖。
自测：python qoder_accounts.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import qoder_client as client
import qoder_cosy as cosy
HERE = os.path.dirname(os.path.abspath(__file__))
OAUTH_CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"
OAUTH_SESSION_TTL = 600  # 秒；默认 10 分钟
_KEYRING_PREFIX = "go-keyring-base64:"


# ---------------------------------------------------------------------------
# 路径（account/paths.go）
# ---------------------------------------------------------------------------

def data_root() -> str:
    return os.environ.get("QODER_DATA_ROOT") or os.path.join(HERE, "qoder_data")


def accounts_dir() -> str:
    return os.path.join(data_root(), "accounts")


def secrets_dir() -> str:
    return os.path.join(data_root(), "secrets")


def settings_path() -> str:
    return os.path.join(data_root(), "settings.json")


def _ensure_dir(d: str) -> str:
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 账号模型（account/model.go）
# ---------------------------------------------------------------------------

@dataclass
class Account:
    id: str = ""
    name: str = ""
    email: str = ""
    user_type: str = ""
    plan: str = ""
    region: str = "global"
    auth_mode: str = "oauth"      # "pat" | "oauth"
    api_mode: str = "openai"
    tags: list = field(default_factory=list)
    created_at: str = ""
    last_used: Optional[str] = None
    active: bool = False
    sort_order: int = 0


@dataclass
class OAuthSession:
    login_id: str
    login_url: str


@dataclass
class QuotaBucket:
    used: float = 0.0
    total: float = 0.0
    remaining: float = 0.0
    reset_time: str = ""


@dataclass
class QuotaInfo:
    plan: str = ""
    user_quota: Optional[QuotaBucket] = None
    addon_quota: Optional[QuotaBucket] = None
    is_quota_exceeded: bool = False
    expires_at: int = 0


@dataclass
class Settings:
    port: int = 8963
    auto_start: bool = False
    log_level: str = "info"
    quota_refresh_interval: int = 0
    bridge_token: str = "qccg"
    console_password: str = ""
    model_mappings: dict = field(default_factory=dict)
    model_mapping: dict = field(default_factory=dict)
    auto_checkin: bool = False
    auto_checkin_times: list = field(default_factory=list)
    machine_salt: str = ""


def sanitize_id(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", raw or "")
    s = s[:64]
    if not s:
        s = f"acct{time.time_ns()}"
    return s


# ---------------------------------------------------------------------------
# 账号存储（account/store.go）
# ---------------------------------------------------------------------------

def _account_path(account_id: str) -> str:
    return os.path.join(accounts_dir(), sanitize_id(account_id) + ".json")


def list_accounts() -> list:
    d = accounts_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                out.append(Account(**json.load(f)))
        except (OSError, ValueError, TypeError):
            continue
    out.sort(key=lambda a: a.sort_order)
    return out


def save_account(acct: Account) -> None:
    _ensure_dir(accounts_dir())
    if not acct.id:
        acct.id = sanitize_id(acct.email + acct.name + str(time.time_ns()))
    if not acct.created_at:
        acct.created_at = _now_iso()
    with open(_account_path(acct.id), "w", encoding="utf-8") as f:
        json.dump(asdict(acct), f, ensure_ascii=False, indent=2)


def reorder(ids: list) -> None:
    """等价 Go: Reorder —— 按给定 id 顺序设置 sort_order 并持久化。"""
    if not ids:
        return
    order_map = {id_: i for i, id_ in enumerate(ids)}
    for acct in list_accounts():
        if acct.id in order_map:
            acct.sort_order = order_map[acct.id]
            save_account(acct)


def cleanup_all_data() -> None:
    """等价 Go: CleanupAllData —— 删除所有账号和密钥。"""
    for acct in list_accounts():
        try:
            delete_secret(acct.id)
        except Exception:
            pass
        try:
            delete_account(acct.id)
        except Exception:
            pass


def delete_account(account_id: str) -> None:
    try:
        os.remove(_account_path(account_id))
    except OSError:
        pass
    delete_secret(account_id)


def get_account(account_id: str) -> Optional[Account]:
    for a in list_accounts():
        if a.id == account_id:
            return a
    return None


def set_active(account_id: str) -> None:
    for a in list_accounts():
        a.active = (a.id == account_id)
        save_account(a)


def get_active() -> Optional[Account]:
    for a in list_accounts():
        if a.active:
            return a
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 密钥存储（account/secret.go）
# ---------------------------------------------------------------------------

def _normalize_secret(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith(_KEYRING_PREFIX):
        enc = s[len(_KEYRING_PREFIX):]
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                s = decoder(enc).decode(errors="replace").strip()
                break
            except Exception:
                continue
    return s


def _secret_path(account_id: str) -> str:
    return os.path.join(secrets_dir(), sanitize_id(account_id) + ".token")


def save_secret(account_id: str, secret: str) -> None:
    _ensure_dir(secrets_dir())
    with open(_secret_path(account_id), "w", encoding="utf-8") as f:
        f.write(_normalize_secret(secret))


def get_secret(account_id: str) -> Optional[str]:
    p = _secret_path(account_id)
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        s = _normalize_secret(f.read())
    return s or None


def delete_secret(account_id: str) -> None:
    try:
        os.remove(_secret_path(account_id))
    except OSError:
        pass


def has_secret(account_id: str) -> bool:
    return get_secret(account_id) is not None


# ---------------------------------------------------------------------------
# 设置（account/store.go Settings / EnsureMachineSalt）
# ---------------------------------------------------------------------------

def load_settings() -> Settings:
    p = settings_path()
    if not os.path.isfile(p):
        return Settings()
    try:
        with open(p, "r", encoding="utf-8") as f:
            return Settings(**json.load(f))
    except (OSError, ValueError, TypeError):
        return Settings()


def save_settings(s: Settings) -> None:
    _ensure_dir(os.path.dirname(settings_path()))
    with open(settings_path(), "w", encoding="utf-8") as f:
        json.dump(asdict(s), f, ensure_ascii=False, indent=2)


def ensure_machine_salt() -> str:
    """读取或首次生成本机指纹盐，写入 settings.json。变更即指纹漂移。"""
    s = load_settings()
    if s.machine_salt:
        return s.machine_salt
    s.machine_salt = os.urandom(16).hex()
    save_settings(s)
    return s.machine_salt


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

def _opener(proxy: str = ""):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url: str, token: str = "", proxy: str = "", timeout: int = 15):
    headers = {"accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode()) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


# ---------------------------------------------------------------------------
# OAuth 设备码登录（account/oauth.go）
# ---------------------------------------------------------------------------

_pending: dict = {}  # login_id -> dict(nonce, verifier, region, deadline)


def _pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start_login(region: str = "global") -> OAuthSession:
    """启动 OAuth：生成 PKCE，返回登录 URL（用户在浏览器打开并授权）。"""
    verifier, challenge = _pkce()
    nonce = os.urandom(16).hex()
    login_id = os.urandom(16).hex()
    ep = client.get_endpoints(region)
    params = urllib.parse.urlencode({
        "nonce": nonce,
        "challenge": challenge,
        "challenge_method": "S256",
        "client_id": OAUTH_CLIENT_ID,
    })
    login_url = ep.device_login_base + "?" + params
    _pending[login_id] = {
        "nonce": nonce,
        "verifier": verifier,
        "region": client.normalize_region(region),
        "deadline": time.time() + OAUTH_SESSION_TTL,
    }
    return OAuthSession(login_id=login_id, login_url=login_url)


def poll_login(login_id: str, proxy: str = ""):
    """轮询一次：返回 (status, account)。

    status ∈ {"pending", "done", "expired", "timeout", "error"}。
    done 时 account 为已保存的 Account。
    """
    st = _pending.get(login_id)
    if st is None:
        return "error", None
    if time.time() > st["deadline"]:
        _pending.pop(login_id, None)
        return "timeout", None

    ep = client.get_endpoints(st["region"])
    q = urllib.parse.urlencode({
        "nonce": st["nonce"],
        "verifier": st["verifier"],
        "challenge_method": "S256",
    })
    url = ep.poll_endpoint + "?" + q
    try:
        with _opener(proxy).open(url, timeout=15) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "pending", None       # 尚未授权
        return "error", None
    except Exception:
        return "error", None

    if code != 200:
        return "pending", None
    try:
        result = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return "error", None
    device_token = result.get("token") or ""
    refresh_token = result.get("refresh_token") or ""
    if not device_token:
        return "pending", None
    acct = build_account_from_token(device_token, refresh_token, st["region"], ep, proxy)
    _pending.pop(login_id, None)
    return "done", acct


def build_account_from_token(device_token: str, refresh_token: str, region: str,
                             ep, proxy: str = "") -> Account:
    """等价 Go: buildAccountFromToken —— 拉用户信息 + 套餐，落盘账号与密钥。"""
    _, info = _get_json(ep.userinfo_base, device_token, proxy)
    info = info or {}
    _, plan_resp = _get_json(ep.plan_endpoint, device_token, proxy)
    plan = (plan_resp or {}).get("plan_tier_name", "")

    uid = str(info.get("userId") or "") + str(info.get("email") or "")
    account_id = sanitize_id(uid)
    acct = Account(
        id=account_id,
        name=str(info.get("name") or ""),
        email=str(info.get("email") or "").lower(),
        user_type=str(info.get("userType") or ""),
        plan=plan,
        region=client.normalize_region(region),
        auth_mode="oauth",
        api_mode="openai",
        created_at=_now_iso(),
    )
    save_secret(account_id, json.dumps(
        {"device_token": device_token, "refresh_token": refresh_token},
        ensure_ascii=False))
    save_account(acct)
    return acct


def add_pat_account(pat: str, region: str = "global", name: str = "") -> Account:
    """用 PAT 添加账号（辅助入口）。"""
    ep = client.get_endpoints(region)
    device_token, refresh_token = client.parse_oauth_secret(pat)
    if device_token.startswith("dt-"):
        _, info = _get_json(ep.userinfo_base, device_token)
        info = info or {}
        uid = str(info.get("userId") or "") + str(info.get("email") or "")
        acct = Account(
            id=sanitize_id(uid), name=name or str(info.get("name") or ""),
            email=str(info.get("email") or "").lower(),
            user_type=str(info.get("userType") or ""), region=client.normalize_region(region),
            auth_mode="oauth", created_at=_now_iso())
    else:
        import qoder_cosy as cosy
        seed = cosy.fingerprint_seed("", pat)
        jt = cosy.exchange_job_token(pat, cosy.derive_machine_id(seed),
                                     cosy.derive_machine_token(seed),
                                     cosy.derive_machine_type(seed), ep.job_token_url)
        acct = Account(
            id=sanitize_id(str(jt.get("id") or pat[:16])),
            name=name or str(jt.get("name") or ""),
            region=client.normalize_region(region),
            auth_mode="pat", created_at=_now_iso())
    save_secret(acct.id, pat)
    save_account(acct)
    return acct


# ---------------------------------------------------------------------------
# 额度 / 套餐（account/oauth.go FetchQuota / fetchPlan）
# ---------------------------------------------------------------------------

def _normalize_token(token: str) -> str:
    """接受裸 token（dt-/drt-/PAT）或 secrets/*.json 的原文密文。

    密文形态是 {"device_token": "dt-...", "refresh_token": "drt-..."}。此前这里
    直接把它当 PAT 送去 jobToken 交换，上游回 401 personal token is invalid，
    额度于是恒为 0、套餐名为空。裸 token 仍按原逻辑走 PAT→jobToken 交换。
    """
    s = (token or "").strip()
    if s.startswith("{"):
        try:
            device_token, refresh_token = client.parse_oauth_secret(s)
        except Exception:
            return s
        return device_token or refresh_token or s
    return s


def fetch_plan(token: str, region: str, proxy: str = "") -> str:
    ep = client.get_endpoints(region)
    _, resp = _get_json(ep.plan_endpoint, _normalize_token(token), proxy)
    return (resp or {}).get("plan_tier_name", "")


def fetch_quota(token: str, region: str, proxy: str = "") -> QuotaInfo:
    ep = client.get_endpoints(region)
    token = _normalize_token(token)

    # Go 端 GetAccountQuota 的 token 转换逻辑：PAT → jobToken → securityOauthToken
    if not token.startswith(("dt-", "drt-")):
        seed = cosy.fingerprint_seed("", token)
        machine_id = cosy.derive_machine_id(seed)
        machine_token = cosy.derive_machine_token(seed)
        machine_type = cosy.derive_machine_type(seed)
        try:
            jt = cosy.exchange_job_token(token, machine_id, machine_token, machine_type,
                                          ep.job_token_url)
            oauth_token = jt.get("securityOauthToken") or ""
            if oauth_token:
                token = oauth_token
        except Exception:
            pass

    _, result = _get_json(ep.quota_endpoint, token, proxy)
    result = result or {}
    info = QuotaInfo(
        plan=fetch_plan(token, region, proxy),
        is_quota_exceeded=(result.get("isQuotaExceeded") is True),
        expires_at=int(result.get("expiresAt") or 0),
    )
    info.user_quota = _extract_bucket(result, "userQuota")
    info.addon_quota = _extract_bucket(result, "addOnQuota")
    return info


def _extract_bucket(data: dict, key: str) -> Optional[QuotaBucket]:
    obj = data.get(key)
    if not isinstance(obj, dict):
        return None
    used = float(obj.get("used") or 0)
    total = float(obj.get("total") or 0)
    remaining = float(obj.get("remaining") or 0)
    if total == 0 and used == 0 and remaining == 0:
        return None
    reset = obj.get("resetTime") or obj.get("reset_time") or ""
    return QuotaBucket(used=used, total=total, remaining=remaining, reset_time=reset)


# ---------------------------------------------------------------------------
# 自测（离线，不涉及网络）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    tmp = tempfile.mkdtemp(prefix="qoder_acct_")
    os.environ["QODER_DATA_ROOT"] = tmp

    # 1) 账号存储 CRUD
    a = Account(id="", name="张三", email="a@b.com", region="cn",
                auth_mode="oauth", created_at=_now_iso())
    save_account(a)
    assert a.id, "id 自动生成失败"
    assert get_account(a.id).name == "张三"
    assert len(list_accounts()) == 1
    set_active(a.id)
    assert get_active().id == a.id
    print("[OK] 账号存储 CRUD")

    # 2) 密钥存储 + keyring 解码
    save_secret(a.id, '{"device_token":"dt-x","refresh_token":"drt-y"}')
    assert has_secret(a.id)
    assert "dt-x" in get_secret(a.id)
    enc = base64.b64encode(b"pat-plain").decode()
    save_secret("k1", _KEYRING_PREFIX + enc)
    assert get_secret("k1") == "pat-plain"
    print("[OK] 密钥存储/keyring 解码")

    # 3) 设置 + 本机盐
    s = load_settings()
    s.port = 3588
    save_settings(s)
    assert load_settings().port == 3588
    salt1 = ensure_machine_salt()
    salt2 = ensure_machine_salt()
    assert salt1 == salt2 and len(salt1) == 32
    print("[OK] 设置/本机盐")

    # 4) OAuth StartLogin（不联网）
    sess = start_login("cn")
    assert sess.login_id and "challenge_method=S256" in sess.login_url
    assert "qoder.com.cn/device/selectAccounts" in sess.login_url
    assert poll_login("nonexistent-login")[0] == "error"
    print("[OK] OAuth StartLogin/URL 构造")

    # 5) sanitize_id
    assert sanitize_id("a/b\\c@d.com") == "abcdcom"
    assert sanitize_id("") .startswith("acct")
    print("[OK] sanitize_id")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    del os.environ["QODER_DATA_ROOT"]
    print("\n全部通过 ✅")