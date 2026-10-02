# Trae 积分额度查询 —— 对齐 TraeWorkAssistant accounts.rs（ide_user_ent_usage + calc_remaining_credits）。
#
# 端点：POST https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage
# body: {"require_usage": true, "req_source": 2}
# 认证：authorization: Cloud-IDE-JWT <jwt>（设备指纹头必需，2026-09 实测仅 authorization 会 401）
# 响应：user_entitlement_pack_list[]，每个包：
#   entitlement_base_info.quota.credits_limit   本周期总额度
#   usage.credits_amount                        已用
#   entitlement_base_info.product_id            209 = Work 积分，其余 = 通用积分
#   entitlement_base_info.end_time / expire_time 到期
# 剩余 = (credits_limit - used).max(0)，对有 credits_limit 的包求和。

import json
import time
import urllib.request
import urllib.error
from typing import Optional

_ENT_USAGE_URL = "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage"
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ide_query_headers(jwt: str, device_id: str, machine_id: str, session_id: str = "") -> dict:
    """IDE 查询类 POST 统一头（对齐 ide_query_post：设备指纹头必需，否则 401）。"""
    auth = jwt if jwt.startswith("Cloud-IDE-JWT ") else "Cloud-IDE-JWT " + jwt.strip()
    request_id = os_urandom_hex(32)
    trace_id = "00-" + os_urandom_hex(16) + "-01"
    headers = {
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
        "x-request-id": request_id,
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "app-version": "0.1.45",
        "x-tt-trace-id": trace_id,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "none",
    }
    if session_id:
        headers["vscode-sessionid"] = session_id
    return headers


def os_urandom_hex(n: int) -> str:
    import secrets
    return secrets.token_hex(n // 2)


def fetch_credit_packs(jwt: str, device_id: str, machine_id: str) -> list:
    """拉取积分包列表（user_entitlement_pack_list）。"""
    body = json.dumps({"require_usage": True, "req_source": 2}).encode()
    headers = _ide_query_headers(jwt, device_id, machine_id)
    req = urllib.request.Request(_ENT_USAGE_URL, data=body, headers=headers, method="POST")
    try:
        with _DIRECT_OPENER.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ide_user_ent_usage HTTP {e.code}: {raw[:200]}")
    packs = data.get("user_entitlement_pack_list")
    if packs is None:
        # dig 下钻一层（data 包裹容错）
        inner = data.get("data") or {}
        packs = inner.get("user_entitlement_pack_list")
    if packs is None:
        raise RuntimeError(f"响应中缺少 user_entitlement_pack_list（键: {list(data.keys())}）")
    return packs


def calc_credit_stats(packs: list, now_ts: Optional[int] = None) -> dict:
    """解析积分包列表 → 统计（对齐 parse_credit_stats 口径）。"""
    now_ts = now_ts if now_ts is not None else int(time.time())
    stats = {"total": 0.0, "general": 0.0, "work": 0.0, "total_limit": 0.0,
             "membership_expire": None, "packs": []}
    for pack in packs:
        group_name = pack.get("group_name", "") or ""
        display_desc = pack.get("display_desc", "") or ""
        base = pack.get("entitlement_base_info") or {}
        end = base.get("end_time") or pack.get("expire_time")
        if ("会员" in group_name or "会员" in display_desc) and isinstance(end, int):
            if stats["membership_expire"] is None or end > stats["membership_expire"]:
                stats["membership_expire"] = end

        limit = (base.get("quota") or {}).get("credits_limit")
        if not isinstance(limit, (int, float)):
            continue
        used = (pack.get("usage") or {}).get("credits_amount") or 0.0
        remaining = max(float(limit) - float(used), 0.0)
        product_id = base.get("product_id") or 0
        expire = pack.get("expire_time")

        # 过期包不计入剩余（对齐参考实现：仅统计仍有剩余且未过期的包）
        if isinstance(expire, int) and expire < now_ts:
            continue

        stats["total"] += remaining
        stats["total_limit"] += float(limit)
        if product_id == 209:
            stats["work"] += remaining
        else:
            stats["general"] += remaining
        stats["packs"].append({
            "product_id": product_id, "limit": limit, "used": used,
            "remaining": remaining, "expire_time": expire,
            "group_name": group_name, "display_desc": display_desc,
        })
    stats["total"] = round(stats["total"], 2)
    stats["general"] = round(stats["general"], 2)
    stats["work"] = round(stats["work"], 2)
    return stats


def query_credits(jwt: str, device_id: str, machine_id: str) -> dict:
    """查询账号剩余积分（total/general/work/明细/会员到期）。"""
    packs = fetch_credit_packs(jwt, device_id, machine_id)
    return calc_credit_stats(packs)
