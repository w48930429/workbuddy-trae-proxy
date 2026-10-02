# Trae 多账号管理：签到 / 启停 / 导出导入 / 积分批量刷新。
#
# 数据模型：vault 已支持 {uid: {token, refresh_token, user_name, avatar, expires_at}}，
# 本模块补充 per-uid 的 enabled 状态与积分缓存（存 config.json 的 trae_accounts 节，
# 与 vault 分离——加密仓只放凭证，非敏感的展示/调度状态放 config）。

import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional

import trae_icube
import trae_vault

CHECKIN_CLAIM_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim"
CHECKIN_STATUS_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/status"
_ENT_USAGE_URL = "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage"

_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_state() -> dict:
    """config.json 的 trae_accounts 节（enabled/credits 缓存）。"""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return (json.load(f) or {}).get("trae_accounts", {}) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = {}
    config["trae_accounts"] = state
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _ide_headers(jwt: str, device_id: str, machine_id: str) -> dict:
    """签到/积分查询共用的完整设备指纹头（对齐 trae_checkin.rs build_headers）。"""
    import secrets as _secrets
    auth = jwt if jwt.startswith("Cloud-IDE-JWT ") else "Cloud-IDE-JWT " + jwt.strip()
    return {
        "accept": "*/*",
        "accept-language": "zh-CN",
        "authorization": auth,
        "content-type": "application/json",
        "user-agent": "VSCode 1.107.1 (TRAE SOLO CN)",
        "x-market-client-id": "VSCode 1.107.1",
        "x-market-user-id": "",
        "x-user-region": "CN",
        "x-device-id": device_id,
        "x-lgw-req-sdk-type": "3",
        "package-type": "stable_cn",
        "x-request-id": _secrets.token_hex(16),
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "app-version": "0.1.45",
        "x-tt-trace-id": "00-" + _secrets.token_hex(8) + "-01",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "none",
        "x-machine-id": machine_id,
    }


def _post_json(url: str, jwt: str, device_id: str, machine_id: str, body: dict = None) -> dict:
    payload = json.dumps(body if body is not None else {}).encode()
    headers = _ide_headers(jwt, device_id, machine_id)
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with _DIRECT_OPENER.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            raise RuntimeError(f"HTTP {e.code}: {raw[:200]}")


def checkin_account(jwt: str, device_id: str, machine_id: str) -> dict:
    """单账号每日签到：POST checkin_credits/claim，body {}。

    code==0 → 成功；message 含「已签/已领取/重复」→ already；其余 → fail。
    """
    data = _post_json(CHECKIN_CLAIM_URL, jwt, device_id, machine_id)
    code = data.get("code")
    msg = data.get("message", "") or ("HTTP-OK" if code == 0 else "unknown")
    already = isinstance(code, int) and code != 0 and any(
        t in msg for t in ("已签", "已领取", "重复", "already")
    )
    reward = None
    for k in ("reward", "reward_credits", "claim_credits", "checkin_credits", "delta", "credits"):
        v = data.get(k)
        if isinstance(v, (int, float)):
            reward = v
            break
        container = data.get("data")
        if isinstance(container, dict) and isinstance(container.get(k), (int, float)):
            reward = container[k]
            break
    if code == 0:
        status = "success"
    elif already:
        status = "already"
    else:
        status = "fail"
    return {"status": status, "message": msg, "reward": reward}


def query_credits(jwt: str, device_id: str, machine_id: str) -> dict:
    """剩余积分（对齐 TraeWorkAssistant ide_user_ent_usage + parse_credit_stats）。"""
    packs = _post_json(_ENT_USAGE_URL, jwt, device_id, machine_id,
                       {"require_usage": True, "req_source": 2})
    pack_list = packs.get("user_entitlement_pack_list")
    if pack_list is None and isinstance(packs.get("data"), dict):
        pack_list = packs["data"].get("user_entitlement_pack_list")
    if pack_list is None:
        raise RuntimeError(f"响应中缺少 user_entitlement_pack_list（键: {list(packs.keys())}）")
    now_ts = int(time.time())
    total = general = work = total_limit = 0.0
    for pack in pack_list:
        base = pack.get("entitlement_base_info") or {}
        limit = (base.get("quota") or {}).get("credits_limit")
        if not isinstance(limit, (int, float)):
            continue
        used = (pack.get("usage") or {}).get("credits_amount") or 0.0
        expire = pack.get("expire_time")
        if isinstance(expire, (int, float)) and expire < now_ts:
            continue
        remaining = max(float(limit) - float(used), 0.0)
        total += remaining
        total_limit += float(limit)
        if base.get("product_id") == 209:
            work += remaining
        else:
            general += remaining
    return {"total": round(total, 2), "general": round(general, 2),
            "work": round(work, 2), "total_limit": round(total_limit, 2)}


def list_accounts() -> list:
    """vault 全部账号 + per-uid 状态/积分缓存，组成面板列表行。"""
    state = _load_state()
    secrets = trae_vault.load_trae_secrets()
    creds = trae_icube.get_device_credentials()
    cred = creds[0] if creds else None
    out = []
    now = time.time()
    for uid, data in secrets.items():
        st = state.get(uid, {})
        entry = {
            "user_id": uid,
            "user_name": data.get("user_name", "") or ("用户%s" % uid[-10:] if uid else "Trae 账号"),
            "enabled": bool(st.get("enabled", True)),
            "expires_at": data.get("expires_at", 0),
            "expired": bool(data.get("expires_at") and data["expires_at"] < now),
            "source": "OAuth",
        }
        jwt = data.get("token", "")
        entry["token_mask"] = (jwt[:12] + "…") if jwt else ""
        # 积分：有缓存用缓存（fresh 内），无缓存且凭证可用时现查
        cached = st.get("credits")
        cached_at = st.get("credits_at", 0)
        if jwt and cred and (not cached or now - cached_at > 300):
            try:
                entry["credits"] = query_credits(jwt, cred.device_id, cred.machine_id)
                state[uid] = {**st, "credits": entry["credits"], "credits_at": now}
                _save_state(state)
            except Exception as exc:
                entry["credits_error"] = str(exc)[:120]
                if cached:
                    entry["credits"] = cached
        elif cached:
            entry["credits"] = cached
        out.append(entry)
    return out


def set_enabled(uid: str, enabled: bool) -> bool:
    state = _load_state()
    st = state.get(uid, {})
    st["enabled"] = bool(enabled)
    state[uid] = st
    _save_state(state)
    return True


def set_all_enabled(enabled: bool) -> int:
    state = _load_state()
    secrets = trae_vault.load_trae_secrets()
    n = 0
    for uid in secrets:
        st = state.get(uid, {})
        if st.get("enabled", True) != enabled:
            st["enabled"] = enabled
            state[uid] = st
            n += 1
    _save_state(state)
    return n


def checkin_one(uid: str) -> dict:
    """单账号签到（面板行内「签到」按钮用）。"""
    secrets = trae_vault.load_trae_secrets()
    data = secrets.get(uid)
    if not data:
        return {"user_id": uid, "status": "fail", "message": "账号不存在"}
    state = _load_state()
    if not state.get(uid, {}).get("enabled", True):
        return {"user_id": uid, "status": "skipped", "message": "已停用"}
    creds = trae_icube.get_device_credentials()
    if not creds:
        return {"user_id": uid, "status": "fail", "message": "无设备信息（未检测到 Trae IDE）"}
    cred = creds[0]
    jwt = data.get("token", "")
    if not jwt:
        return {"user_id": uid, "status": "fail", "message": "无凭证"}
    try:
        r = checkin_account(jwt, cred.device_id, cred.machine_id)
        return {"user_id": uid, "user_name": data.get("user_name", ""), **r}
    except Exception as exc:
        return {"user_id": uid, "status": "fail", "message": str(exc)[:120]}


def checkin_all() -> list:
    """全部账号每日签到，返回 [{user_id, user_name, status, message, reward}]。"""
    secrets = trae_vault.load_trae_secrets()
    state = _load_state()
    creds = trae_icube.get_device_credentials()
    cred = creds[0] if creds else None
    results = []
    for uid, data in secrets.items():
        st = state.get(uid, {})
        if not st.get("enabled", True):
            results.append({"user_id": uid, "user_name": data.get("user_name", ""),
                            "status": "skipped", "message": "已停用"})
            continue
        jwt = data.get("token", "")
        if not jwt or not cred:
            results.append({"user_id": uid, "user_name": data.get("user_name", ""),
                            "status": "fail", "message": "无凭证或无设备信息"})
            continue
        try:
            r = checkin_account(jwt, cred.device_id, cred.machine_id)
            results.append({"user_id": uid, "user_name": data.get("user_name", ""), **r})
        except Exception as exc:
            results.append({"user_id": uid, "user_name": data.get("user_name", ""),
                            "status": "fail", "message": str(exc)[:120]})
    return results


def import_account(token: str, refresh_token: str = "", user_name: str = "",
                   user_id: str = "", device_info: dict = None) -> dict:
    """手动导入一个 Trae 账号（token 从油猴脚本/IDE 提取）。

    user_id 缺省时从 JWT payload 解析（sub/user_id），再不行用 token 哈希派生。
    """
    import base64 as _b64
    jwt = token.strip().removeprefix("Bearer ").strip()
    uid = user_id
    if not uid:
        try:
            payload_part = jwt.split(".")[1]
            payload_part += "=" * (-len(payload_part) % 4)
            claims = json.loads(_b64.urlsafe_b64decode(payload_part).decode())
            uid = str(claims.get("user_id") or claims.get("sub") or claims.get("uid") or "")
        except Exception:
            uid = ""
    if not uid:
        uid = "trae_" + hashlib_token_id(jwt)
    expires_at = 0
    try:
        payload_part = jwt.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        claims = json.loads(_b64.urlsafe_b64decode(payload_part).decode())
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            expires_at = float(exp)
    except Exception:
        pass
    secrets = trae_vault.load_trae_secrets()
    secrets[uid] = {
        "token": jwt,
        "refresh_token": refresh_token,
        "user_name": user_name or ("Trae 账号 %s" % uid[-6:]),
        "avatar": "",
        "expires_at": expires_at,
    }
    trae_vault.save_trae_secrets(secrets)
    state = _load_state()
    state.setdefault(uid, {})["enabled"] = True
    _save_state(state)
    return {"user_id": uid, "user_name": secrets[uid]["user_name"], "expires_at": expires_at}


def hashlib_token_id(jwt: str) -> str:
    import hashlib
    return hashlib.sha256(jwt.encode()).hexdigest()[:10]


def export_accounts() -> list:
    """导出全部账号（token 明文，供迁移备份）。"""
    secrets = trae_vault.load_trae_secrets()
    state = _load_state()
    out = []
    for uid, data in secrets.items():
        out.append({
            "user_id": uid,
            "user_name": data.get("user_name", ""),
            "token": data.get("token", ""),
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": data.get("expires_at", 0),
            "enabled": state.get(uid, {}).get("enabled", True),
        })
    return out


def import_accounts_batch(accounts: list) -> int:
    """批量导入（导出格式的数组）。"""
    n = 0
    state = _load_state()
    secrets = trae_vault.load_trae_secrets()
    for a in accounts:
        uid = a.get("user_id")
        token = (a.get("token") or "").strip()
        if not uid or not token:
            continue
        secrets[uid] = {
            "token": token,
            "refresh_token": a.get("refresh_token", ""),
            "user_name": a.get("user_name", ""),
            "avatar": a.get("avatar", ""),
            "expires_at": a.get("expires_at", 0),
        }
        st = state.get(uid, {})
        st["enabled"] = bool(a.get("enabled", True))
        state[uid] = st
        n += 1
    trae_vault.save_trae_secrets(secrets)
    _save_state(state)
    return n


def delete_account(uid: str) -> bool:
    secrets = trae_vault.load_trae_secrets()
    if uid in secrets:
        del secrets[uid]
        trae_vault.save_trae_secrets(secrets)
        state = _load_state()
        state.pop(uid, None)
        _save_state(state)
        return True
    return False
