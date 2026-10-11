"""Qoder 每日签到 —— 纯 Python 移植 qoder2api/checkin.go。

流程（桌面端抓包还原）：
  GET  /sash/api/v1/me/campaigns               查询活动
  POST /sash/api/v1/me/campaigns/{id}/claim     领取（空 body）
  GET  /sash/api/v1/me/daily-check-in/status    只读统计（legacy，常 DISABLED）

本地记录签到历史（<data_root>/checkin_history.json）以计算真实连续签到天数。

自测：python qoder_checkin.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import qoder_accounts as accounts
import qoder_client as client

STATUS_CLAIMED = "claimed"
STATUS_ALREADY = "already_claimed"
STATUS_NO_CAMPAIGN = "no_campaign"
STATUS_NO_TOKEN = "no_token"
STATUS_ERROR = "error"

CHECKIN_HEADERS_UA = "Qoder"
DEFAULT_REWARD = 100


@dataclass
class CheckinResult:
    account: str = ""
    account_id: str = ""
    status: str = STATUS_ERROR
    amount: int = 0
    expires_at: str = ""
    message: str = ""
    streak_days: int = 0
    total_claim_days: int = 0
    total_reward_credits: int = 0
    reward_credits: int = 0
    window_date: str = ""


# ---------------------------------------------------------------------------
# HTTP（checkin.go doCheckinRequest）
# ---------------------------------------------------------------------------

def _checkin_headers(device_token: str) -> dict:
    return {
        "authorization": "Bearer " + device_token,
        "accept": "application/json",
        "accept-language": "zh-CN",
        "user-agent": CHECKIN_HEADERS_UA,
        "cosy-clienttype": "10",
    }


def _checkin_host(region: str) -> str:
    return client.get_endpoints(region).openapi_host


def _do_checkin_request(method: str, path: str, device_token: str,
                        req_body=None, host: str = "", proxy: str = ""):
    """返回 (http_status, parsed_json_or_None, raw_text)。"""
    host = host or _checkin_host("cn")
    url = "https://" + host + path
    data = None
    if req_body is not None:
        data = json.dumps(req_body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=_checkin_headers(device_token),
                                 method=method)
    if method == "POST":
        req.add_header("origin", "https://" + host)
        if req_body is None:
            req.add_header("content-length", "0")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy
        else urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            raw = resp.read().decode(errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") if e.fp else str(e)
        return e.code, None, raw
    except Exception as e:  # noqa: BLE001
        return 0, None, f"do request: {e}"
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    return status, parsed, raw


def _device_token_of(secret: str) -> str:
    s = (secret or "").strip()
    if s.startswith("{"):
        try:
            return (json.loads(s) or {}).get("device_token", "") or ""
        except ValueError:
            return ""
    return s


# ---------------------------------------------------------------------------
# 本地签到历史（checkin.go 本地历史）
# ---------------------------------------------------------------------------

def _history_path() -> str:
    return os.path.join(accounts.data_root(), "checkin_history.json")


def _load_history() -> dict:
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            h = json.load(f)
        if not isinstance(h.get("accounts"), dict):
            h["accounts"] = {}
        return h
    except (OSError, ValueError):
        return {"accounts": {}}


def _save_history(h: dict) -> None:
    os.makedirs(os.path.dirname(_history_path()), exist_ok=True)
    with open(_history_path(), "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)


def calc_local_stats(recs: list):
    """(streak, total_days, total_credits)。今天未领不断链。"""
    if not recs:
        return 0, 0, 0
    days = {r["date"] for r in recs}
    total_credits = sum(int(r.get("amount") or 0) for r in recs)
    total_days = len(recs)
    cursor = datetime.now()
    if cursor.strftime("%Y-%m-%d") not in days:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor.strftime("%Y-%m-%d") in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak, total_days, total_credits


def record_checkin_on(account_id: str, date: str, amount: int):
    h = _load_history()
    recs = h["accounts"].get(account_id, [])
    found = any(r["date"] == date for r in recs)
    if not found:
        recs.append({"date": date, "amount": amount})
        h["accounts"][account_id] = recs
        _save_history(h)
    elif amount > 0:
        for r in recs:
            if r["date"] == date and int(r.get("amount") or 0) == 0:
                r["amount"] = amount
                h["accounts"][account_id] = recs
                _save_history(h)
                break
    return calc_local_stats(recs)


def _apply_local_streak(account_id: str, res: CheckinResult, record: bool, date: str, amount: int):
    if record:
        date = date or datetime.now().strftime("%Y-%m-%d")
        streak, total_days, total_credits = record_checkin_on(account_id, date, amount)
    else:
        streak, total_days, total_credits = calc_local_stats(
            _load_history()["accounts"].get(account_id, []))
    if total_days > 0:
        res.streak_days = streak
        res.total_claim_days = total_days
        res.total_reward_credits = total_credits


# ---------------------------------------------------------------------------
# 签到主流程（checkin.go campaignsCheckin / checkinAccount）
# ---------------------------------------------------------------------------

def _window_hint(res: CheckinResult, fallback: str) -> str:
    if not res.window_date:
        return fallback
    today = datetime.now().strftime("%Y-%m-%d")
    if res.window_date == today:
        return fallback
    try:
        wd = datetime.strptime(res.window_date, "%Y-%m-%d")
        return f"已领取 {res.window_date} 窗口额度（新窗口 {wd + timedelta(days=1):%m-%d} 10:00 开放）"
    except ValueError:
        return fallback


def campaigns_checkin(device_token: str, res: CheckinResult, host: str, proxy: str = "") -> CheckinResult:
    status, body, raw = _do_checkin_request("GET", "/sash/api/v1/me/campaigns",
                                            device_token, None, host, proxy)
    if status != 200:
        res.message = f"查询活动失败 HTTP {status}: {raw[:300]}"
        return res

    campaigns = (body or {}).get("campaigns") or []
    target = None
    benefit_campaign = None
    already_claimed = False
    for c in campaigns:
        if not isinstance(c, dict) or c.get("actionType") != "CLAIM_BENEFIT":
            continue
        benefit_campaign = c
        if c.get("claimStatus") == "CLAIMABLE":
            target = c
        elif c.get("claimStatus") == "CLAIMED":
            already_claimed = True

    window_src = target or benefit_campaign
    if window_src and int(window_src.get("startAt") or 0) > 0:
        res.window_date = datetime.fromtimestamp(
            int(window_src["startAt"])).strftime("%Y-%m-%d")

    if target is None:
        if already_claimed:
            res.status = STATUS_ALREADY
            res.message = _window_hint(res, "今日已领取")
        else:
            res.status = STATUS_NO_CAMPAIGN
            res.message = "无可用签到活动"
        return res

    claim_path = f"/sash/api/v1/me/campaigns/{target.get('campaignId')}/claim"
    status, body, raw = _do_checkin_request("POST", claim_path, device_token, None, host, proxy)
    if status != 200:
        res.message = f"领取失败 HTTP {status}: {raw[:300]}"
        return res

    body = body or {}
    claim_status = body.get("status")
    if claim_status == "CLAIMED":
        if body.get("replayed"):
            res.status = STATUS_ALREADY
            res.message = _window_hint(res, "今日已领取")
        else:
            res.status = STATUS_CLAIMED
            benefit = body.get("benefit") or {}
            res.amount = int(benefit.get("amount") or 0)
            res.expires_at = body.get("expiresAt", "")
            res.message = f"领取成功 +{res.amount} {target.get('campaignKey', '')}"
        return res

    res.message = f"未知状态: {claim_status}"
    return res


def _read_daily_checkin_stats(device_token: str, res: CheckinResult, host: str, proxy: str = ""):
    status, body, raw = _do_checkin_request("GET", "/sash/api/v1/me/daily-check-in/status",
                                            device_token, None, host, proxy)
    if status != 200 or not isinstance(body, dict):
        return
    if not body.get("status"):
        return
    if int(body.get("currentStreakDays") or 0) > res.streak_days:
        res.streak_days = int(body["currentStreakDays"])
    if int(body.get("totalClaimDays") or 0) > res.total_claim_days:
        res.total_claim_days = int(body["totalClaimDays"])
    if int(body.get("totalRewardCredits") or 0) > res.total_reward_credits:
        res.total_reward_credits = int(body["totalRewardCredits"])
    if res.reward_credits == 0 and int(body.get("rewardCredits") or 0) > 0:
        res.reward_credits = int(body["rewardCredits"])


def _streak_suffix(res: CheckinResult) -> str:
    if res.streak_days <= 0 and res.total_claim_days <= 0:
        return ""
    return (f"（连续 {res.streak_days} 天 · 累计 {res.total_claim_days} 天 · "
            f"共 {res.total_reward_credits} 积分）")


def checkin_account(acct: accounts.Account, proxy: str = "") -> CheckinResult:
    res = CheckinResult(account=acct.name, account_id=acct.id)
    secret = accounts.get_secret(acct.id)
    if not secret:
        res.status = STATUS_NO_TOKEN
        res.message = "无 token"
        return res
    device_token = _device_token_of(secret)
    if not device_token:
        res.status = STATUS_NO_TOKEN
        res.message = "无 device token"
        return res

    host = _checkin_host(acct.region)
    res = campaigns_checkin(device_token, res, host, proxy)
    _read_daily_checkin_stats(device_token, res, host, proxy)

    record = res.status in (STATUS_CLAIMED, STATUS_ALREADY)
    amount = res.amount or res.reward_credits or DEFAULT_REWARD
    _apply_local_streak(acct.id, res, record, res.window_date, amount)
    if record or res.streak_days > 0 or res.total_claim_days > 0:
        res.message += _streak_suffix(res)
    return res


def checkin_all(proxy: str = "") -> list:
    results = []
    for acct in accounts.list_accounts():
        if not accounts.has_secret(acct.id):
            results.append(CheckinResult(account=acct.name, account_id=acct.id,
                                         status=STATUS_NO_TOKEN, message="无 secret，跳过"))
            continue
        results.append(checkin_account(acct, proxy))
    return results


def checkin_one(account_id: str, proxy: str = "") -> CheckinResult:
    acct = accounts.get_account(account_id)
    if acct is None:
        return CheckinResult(account_id=account_id, status=STATUS_ERROR,
                             message=f"账号不存在: {account_id}")
    if not accounts.has_secret(acct.id):
        return CheckinResult(account=acct.name, account_id=acct.id,
                             status=STATUS_NO_TOKEN, message="无 secret")
    return checkin_account(acct, proxy)


# ---------------------------------------------------------------------------
# 自动签到调度（checkin.go StartCheckinScheduler）
# ---------------------------------------------------------------------------

_DEFAULT_TIMES = ["10:00"]
_done_slots: set = set()


def checkin_times(settings: Optional[accounts.Settings] = None) -> list:
    if settings is not None and settings.auto_checkin_times:
        return settings.auto_checkin_times
    return _DEFAULT_TIMES


def _parse_hhmm(s: str):
    parts = (s or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def run_scheduled_checkin(now: Optional[datetime] = None, proxy: str = "") -> bool:
    """执行一次调度检查：开关开启、已过时间点、当日该点未执行 → 自动签到。"""
    settings = accounts.load_settings()
    if not settings.auto_checkin:
        return False
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    ran = False
    for t in checkin_times(settings):
        hm = _parse_hhmm(t)
        if hm is None:
            continue
        slot_min = hm[0] * 60 + hm[1]
        if now_min < slot_min:
            continue
        key = f"{date} {t}"
        if key in _done_slots:
            continue
        results = checkin_all(proxy)
        retryable = sum(1 for r in results if r.status in
                        (STATUS_ERROR, STATUS_NO_CAMPAIGN, STATUS_NO_TOKEN))
        if retryable == 0 or now_min >= slot_min + 120:
            _done_slots.add(key)
        ran = True
    return ran


_scheduler_thread = None
_scheduler_running = False


def start_checkin_scheduler():
    """启动后台调度器（非阻塞）。每分钟检查一次：对每个配置的时间点，
    若开关开启、当前已过该点、且当日该点尚未执行，则自动签到。
    """
    global _scheduler_thread, _scheduler_running
    if _scheduler_running:
        return
    _scheduler_running = True

    def _loop():
        import time as _t
        while _scheduler_running:
            _t.sleep(60)
            try:
                run_scheduled_checkin()
            except Exception:
                pass

    _scheduler_thread = threading.Thread(target=_loop, daemon=True, name="qoder-checkin-scheduler")
    _scheduler_thread.start()
    times = checkin_times()
    logger.info("auto checkin scheduler started (times=%s, default off)", ",".join(times))


def stop_checkin_scheduler():
    """停止后台调度器。"""
    global _scheduler_running
    _scheduler_running = False


# ---------------------------------------------------------------------------
# 自测（离线）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="qoder_ck_")
    os.environ["QODER_DATA_ROOT"] = tmp

    # 1) 签到头
    h = _checkin_headers("dt-abc")
    assert h["authorization"] == "Bearer dt-abc"
    assert h["cosy-clienttype"] == "10"
    print("[OK] 签到请求头")

    # 2) device token 解析
    assert _device_token_of('{"device_token":"dt-x","refresh_token":"drt-y"}') == "dt-x"
    assert _device_token_of("plain-token") == "plain-token"
    print("[OK] device token 解析")

    # 3) 本地连续签到统计
    today = datetime.now()
    recs = [{"date": (today - timedelta(days=i)).strftime("%Y-%m-%d"), "amount": 100}
            for i in range(3)]
    streak, total_days, total_credits = calc_local_stats(recs)
    assert streak == 3 and total_days == 3 and total_credits == 300, (streak, total_days, total_credits)
    # 今天未领不断链：只到昨天
    recs2 = [{"date": (today - timedelta(days=i)).strftime("%Y-%m-%d"), "amount": 100}
             for i in range(1, 3)]
    assert calc_local_stats(recs2)[0] == 2
    assert calc_local_stats([]) == (0, 0, 0)
    print("[OK] 本地连续签到统计")

    # 4) 历史记录幂等
    d = today.strftime("%Y-%m-%d")
    record_checkin_on("acct1", d, 100)
    s1 = record_checkin_on("acct1", d, 100)   # 重复不叠加
    assert s1[0] == 1 and s1[2] == 100, s1
    print("[OK] 签到历史幂等")

    # 5) 时间解析
    assert _parse_hhmm("09:15") == (9, 15)
    assert _parse_hhmm("24:00") is None
    assert _parse_hhmm("bad") is None
    print("[OK] HH:MM 解析")

    # 6) 窗口提示
    r = CheckinResult(window_date="2020-01-01")
    assert "新窗口" in _window_hint(r, "今日已领取")
    assert _window_hint(CheckinResult(), "今日已领取") == "今日已领取"
    print("[OK] 窗口提示")

    shutil.rmtree(tmp, ignore_errors=True)
    del os.environ["QODER_DATA_ROOT"]
    print("\n全部通过 ✅")