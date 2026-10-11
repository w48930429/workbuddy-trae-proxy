"""Qoder 账号池模块 —— 多账号轮询选号 + 冷却 + 失败自动换号。

对应 Go: internal/bridge/pool.go + bridge_pool.go + errors.go(部分)

核心概念：
  - Slot: 一个可用账号的运行时状态（session + 冷却）
  - Pool: 账号池，轮询选号，跳过冷却中与已试过的账号
  - ShouldFailover: 判断某次上游失败是否值得换号
  - CooldownFor: 给出错误对应的冷却时长

冷却策略：
  - CooldownQuota (30min): 额度/权限类失败 (401/403)
  - CooldownRate (60s): 上游频控 (429)
  - CooldownTransient (15s): 瞬时上游故障 (418/5xx/传输抖动)
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import qoder_accounts as accounts
import qoder_client as client
import qoder_cosy as cosy


# ── 冷却时长常量 ──────────────────────────────────────────────────────────

COOLDOWN_QUOTA = 30 * 60        # 30 分钟：额度/权限类失败
COOLDOWN_RATE = 60              # 60 秒：频控
COOLDOWN_TRANSIENT = 15         # 15 秒：瞬时故障
TRANSIENT_MAX_RETRIES = 2       # 同账号瞬时故障额外重试次数


# ── 错误分类 ──────────────────────────────────────────────────────────────

CONTENT_POLICY_MARKERS = [
    "DataInspectionFailed",
    "inappropriate content",
    "input text data may contain",
    "ContentFilter",
    "SensitiveContent",
]

CLIENT_FAULT_MARKERS = [
    "invalid_parameter_error",
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "Range of ",
    "DataInspectionFailed",
    "inappropriate content",
    "input text data may contain",
    "ContentFilter",
    "SensitiveContent",
]

NON_TRANSIENT_TRANSPORT_MARKERS = [
    "unsupported protocol scheme",
    "invalid url",
    "unknown scheme",
    "x509:",
]

TRANSIENT_HTTP_CODES = {418, 500, 502, 503, 504}

ACCOUNT_EXHAUSTED_MARKERS = [
    "quota", "credit", "exceed", "10605", "rate limit", "too many request",
]


class UpstreamError(Exception):
    """上游错误，携带 HTTP 状态 + 详情 + 分类。"""

    def __init__(self, status: int, detail: str, err_type: str = "", message: str = ""):
        self.status = status
        self.detail = detail
        self.err_type = err_type or "upstream_error"
        self.message = message or detail
        super().__init__(self.message)


class NoAccountError(Exception):
    """池中没有任何可用账号。"""
    pass


def _contains_any(text: str, markers: list[str]) -> bool:
    return any(m in text for m in markers)


def is_content_policy(detail: str) -> bool:
    return _contains_any(detail, CONTENT_POLICY_MARKERS)


def is_transient_upstream(code: int, detail: str) -> bool:
    if code in (401, 403, 429):
        return False
    if _contains_any(detail, CLIENT_FAULT_MARKERS):
        return False
    if code in TRANSIENT_HTTP_CODES or code >= 500:
        return True
    if 400 <= code < 500 and "provider_error" in detail:
        return True
    return False


def is_transient_transport(err: Exception) -> bool:
    if err is None:
        return False
    msg = str(err).lower()
    if _contains_any(msg, NON_TRANSIENT_TRANSPORT_MARKERS):
        return False
    transient_markers = ["ssl", "eof", "reset", "timed out", "timeout",
                         "deadline exceeded", "broken pipe", "connection"]
    return _contains_any(msg, transient_markers)


def is_account_exhausted_detail(detail: str) -> bool:
    d = detail.lower()
    return _contains_any(d, ACCOUNT_EXHAUSTED_MARKERS)


def should_failover(err: Exception) -> bool:
    """判断某次上游失败是否值得换号。"""
    if err is None:
        return False
    if isinstance(err, UpstreamError):
        if err.err_type == "content_policy_rejected":
            return False
        if err.status in (401, 403, 429):
            return True
        if err.status == 418 or err.status >= 500:
            return True
        if 400 <= err.status < 500:
            return False
        return is_account_exhausted_detail(err.detail)
    return is_transient_transport(err)


def cooldown_for(err: Exception) -> int:
    """给出错误对应的冷却时长（秒）。"""
    if isinstance(err, UpstreamError):
        d = _retry_after_from_detail(err.detail)
        if d is not None:
            return d
        if err.status in (401, 403):
            return COOLDOWN_QUOTA
        if err.status == 429:
            return COOLDOWN_RATE
        return COOLDOWN_TRANSIENT
    return COOLDOWN_TRANSIENT


def _retry_after_from_detail(detail: str) -> Optional[int]:
    """从上游详情解析 retryAfterSeconds（上限 5 分钟）。"""
    idx = detail.find("retryAfterSeconds")
    if idx < 0:
        return None
    rest = detail[idx + len("retryAfterSeconds"):]
    digits = ""
    found_digit = False
    for ch in rest:
        if ch.isdigit():
            digits += ch
            found_digit = True
        elif found_digit:
            break
    if not digits:
        return None
    n = int(digits)
    if n <= 0:
        return None
    return min(n, 300)


def friendly_upstream_error(code: int, detail: str) -> tuple[str, str]:
    """返回 (message, err_type)。"""
    detail = detail.strip()
    if is_content_policy(detail):
        msg = ("上游内容安全审核未通过 (DataInspectionFailed)：输入可能含不当内容，"
               "属确定性拒绝、重试无效。请检查/缩短输入后重试。上游详情：" +
               _truncate(detail, 300))
        return msg, "content_policy_rejected"
    if is_transient_upstream(code, detail) or \
       ("provider_error" in detail and "invalid_" not in detail):
        d = _truncate(detail, 300) or "(无详情)"
        msg = f"上游瞬时故障 (HTTP {code})：网关已对同账号自动重试仍失败，请稍后重试。上游详情：{d}"
        return msg, "upstream_transient_error"
    return f"upstream {code}: {detail}", "upstream_error"


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def error_status(err: Exception) -> int:
    """取错误对应的 HTTP 状态码。"""
    if isinstance(err, NoAccountError):
        return 503
    if isinstance(err, UpstreamError):
        if err.err_type == "content_policy_rejected":
            return 400
        if err.err_type == "upstream_transient_error":
            return 502
        if 400 <= err.status <= 599:
            return err.status
    return 500


# ── Slot: 单个账号的运行时状态 ────────────────────────────────────────────

@dataclass
class Slot:
    id: str
    name: str
    region: str
    sess: cosy.SessionContext = None
    bearer: client.BearerClient = None
    _cooldown_until: float = 0.0
    _last_err: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _catalog: set = field(default_factory=set, repr=False)
    _catalog_at: float = 0.0
    _credential: str = ""
    _refresh_tried: bool = False

    def available(self, now: float = 0) -> bool:
        now = now or time.time()
        with self._lock:
            return now >= self._cooldown_until

    def cool(self, duration_sec: int, reason: str):
        with self._lock:
            until = time.time() + duration_sec
            if until > self._cooldown_until:
                self._cooldown_until = until
            self._last_err = reason

    def note_success(self):
        with self._lock:
            self._last_err = ""

    @property
    def cooling_remaining_ms(self) -> int:
        with self._lock:
            now = time.time()
            if now < self._cooldown_until:
                return int((self._cooldown_until - now) * 1000)
            return 0

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_err

    @property
    def catalog(self) -> set:
        with self._lock:
            return self._catalog

    @property
    def catalog_at(self) -> float:
        with self._lock:
            return self._catalog_at

    def set_catalog(self, keys: set):
        with self._lock:
            self._catalog = keys
            self._catalog_at = time.time()

    def supports_model(self, model_key: str) -> bool:
        with self._lock:
            if not self._catalog:
                return True
            return model_key in self._catalog

    def status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "region": self.region,
            "cooling_remaining_ms": self.cooling_remaining_ms,
            "last_error": self.last_error,
        }


def build_slot_from_secret(acct: accounts.Account, proxy: str = "") -> Slot:
    """用账号凭证构建一个可入池的 Slot。"""
    secret = accounts.get_secret(acct.id) or ""
    bs = client.build_session_from_secret(secret, acct.region, proxy)
    bearer = bs.client(proxy)
    current, _ = client.parse_oauth_secret(secret)
    return Slot(
        id=acct.id,
        name=acct.name or acct.email or acct.id,
        region=acct.region,
        sess=bs.session,
        bearer=bearer,
        _credential=current,
    )


def is_token_expired(err: Exception) -> bool:
    """判断错误是否为 token 失效（401 Unauthorized）——可尝试凭证轮换。"""
    return isinstance(err, UpstreamError) and err.status == 401


def rotate_slot_credential(slot: Slot, proxy: str = "") -> bool:
    """token 失效时在 dt-/drt- 两个凭证间轮换，重建 session 并持久化。

    Qoder 账号存有两个独立凭证：device_token (dt-) 与 refresh_token (drt-)。
    二者实测都能直接作为 Bearer 建立可用会话。当当前凭证 401 时，切换到
    另一个凭证重建 Slot，并把轮换结果写回磁盘 secret。

    返回 True 表示轮换成功、Slot 已更新可重试。
    """
    secret = accounts.get_secret(slot.id)
    if not secret:
        return False
    device_token, refresh_token = client.parse_oauth_secret(secret)
    current = slot._credential or device_token
    alt = refresh_token if current == device_token else device_token
    if not alt or alt == current:
        return False
    try:
        new_secret = json.dumps({"device_token": alt, "refresh_token": current},
                                ensure_ascii=False)
        bs = client.build_session_from_secret(new_secret, slot.region, proxy)
    except Exception:
        return False
    with slot._lock:
        slot.sess = bs.session
        slot.bearer = bs.client(proxy)
        slot._credential = alt
        slot._refresh_tried = True
        slot._last_err = ""
    try:
        accounts.save_secret(slot.id, new_secret)
    except Exception:
        pass
    return True


# ── Pool: 账号池 ──────────────────────────────────────────────────────────

class Pool:
    """账号池：轮询选号，跳过冷却中与已试过的账号。"""

    def __init__(self, slots: list[Slot] = None, state_path: str = ""):
        self._slots: list[Slot] = list(slots) if slots else []
        self._cursor = 0
        self._state_path = state_path
        self._lock = threading.Lock()
        if state_path:
            self._load()

    def add(self, slot: Slot):
        with self._lock:
            self._slots.append(slot)

    def size(self) -> int:
        with self._lock:
            return len(self._slots)

    def snapshot(self) -> list[Slot]:
        with self._lock:
            return list(self._slots)

    def pick(self, exclude: set[str] = None,
             filter_fn=None) -> tuple[Optional[Slot], bool]:
        """从轮询游标开始扫描，返回第一个可用的账号。"""
        exclude = exclude or set()
        slots = self.snapshot()
        n = len(slots)
        if n == 0:
            return None, False
        with self._lock:
            start = self._cursor
        now = time.time()
        for i in range(n):
            idx = (start + i) % n
            s = slots[idx]
            if s.id in exclude:
                continue
            if not s.available(now):
                continue
            if filter_fn is not None and not filter_fn(s):
                continue
            with self._lock:
                self._cursor = (idx + 1) % n
            return s, True
        return None, False

    def cool(self, slot_id: str, duration_sec: int, reason: str):
        for s in self.snapshot():
            if s.id == slot_id:
                s.cool(duration_sec, reason)
                self._save()
                return

    def note_success(self, slot_id: str):
        for s in self.snapshot():
            if s.id == slot_id:
                s.note_success()
                self._save()
                return

    def status(self) -> list[dict]:
        return [s.status() for s in self.snapshot()]

    # ── 持久化 ─────────────────────────────────────────────────────────

    def _load(self):
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        now = time.time()
        restored = 0
        for s in self.snapshot():
            e = st.get("slots", {}).get(s.id)
            if not e:
                continue
            with s._lock:
                until = e.get("cooldown_until", 0)
                if until > now:
                    s._cooldown_until = until
                    restored += 1
                s._last_err = e.get("last_error", "")
        if restored > 0:
            print(f"[pool] restored {restored} cooling account(s) from {self._state_path}")

    def _save(self):
        if not self._state_path:
            return
        st = {"slots": {}}
        for s in self.snapshot():
            with s._lock:
                until = s._cooldown_until
                last_err = s._last_err
            if until > 0 or last_err:
                st["slots"][s.id] = {
                    "cooldown_until": until,
                    "last_error": last_err,
                }
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except OSError:
            pass


def build_pool_from_accounts(proxy: str = "",
                             state_path: str = "") -> Pool:
    """从已保存的账号列表构建账号池。"""
    acct_list = accounts.list_accounts()
    slots = []
    for acct in acct_list:
        try:
            slot = build_slot_from_secret(acct, proxy)
            slots.append(slot)
        except Exception as e:
            print(f"[pool] skip account {acct.id} ({acct.name}): {e}")
    state = state_path or os.path.join(accounts.data_root(), "pool_state.json")
    return Pool(slots, state)


# ── 自测 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== qoder_pool.py 自测 ===")

    # Test 1: Slot basic operations
    s1 = Slot(id="a", name="acct-a", region="cn")
    assert s1.available(), "new slot should be available"
    s1.cool(60, "test cooldown")
    assert not s1.available(), "cooled slot should not be available"
    assert s1.cooling_remaining_ms > 0, "should have remaining cooldown"
    s1.note_success()
    assert s1.last_error == "", "success clears last_err"
    print("Test 1 PASS: Slot basic operations")

    # Test 2: Pool pick / cool / exclude
    s1 = Slot(id="a", name="acct-a", region="cn")
    s2 = Slot(id="b", name="acct-b", region="global")
    pool = Pool([s1, s2])
    assert pool.size() == 2

    picked, ok = pool.pick()
    assert ok and picked.id in ("a", "b"), "should pick a slot"

    pool.cool("a", 60, "quota exceeded")
    picked2, ok2 = pool.pick(exclude={"b"})
    assert ok2 and picked2.id == "a" or not ok2, "cool + exclude logic"
    # Actually 'a' is cooled so should not be picked
    picked3, ok3 = pool.pick()
    assert ok3 and picked3.id == "b", "should skip cooled 'a' and pick 'b'"
    print("Test 2 PASS: Pool pick / cool / exclude")

    # Test 3: should_failover
    assert not should_failover(None), "nil error"
    assert should_failover(UpstreamError(401, "unauthorized")), "401 should failover"
    assert should_failover(UpstreamError(403, "forbidden")), "403 should failover"
    assert should_failover(UpstreamError(429, "rate limit")), "429 should failover"
    assert should_failover(UpstreamError(418, "provider error")), "418 should failover"
    assert should_failover(UpstreamError(500, "server error")), "500 should failover"
    assert not should_failover(UpstreamError(400, "bad request")), "400 should not"
    assert not should_failover(UpstreamError(0, "DataInspectionFailed")), "content policy not"
    assert should_failover(UpstreamError(0, "quota exceeded")), "quota detail should"
    assert not should_failover(ValueError("some bug")), "non-transient transport"
    assert should_failover(ValueError("ssl eof")), "ssl eof is transient transport"
    print("Test 3 PASS: should_failover")

    # Test 4: cooldown_for
    assert cooldown_for(UpstreamError(401, "unauthorized")) == COOLDOWN_QUOTA
    assert cooldown_for(UpstreamError(403, "forbidden")) == COOLDOWN_QUOTA
    assert cooldown_for(UpstreamError(429, "rate limit")) == COOLDOWN_RATE
    assert cooldown_for(UpstreamError(500, "server error")) == COOLDOWN_TRANSIENT
    assert cooldown_for(ValueError("timeout")) == COOLDOWN_TRANSIENT
    print("Test 4 PASS: cooldown_for")

    # Test 5: retry_after_from_detail
    assert _retry_after_from_detail("retryAfterSeconds: 30") == 30
    assert _retry_after_from_detail("retryAfterSeconds=120") == 120
    assert _retry_after_from_detail("no retry info") is None
    assert _retry_after_from_detail("retryAfterSeconds: 99999") == 300, "capped at 5min"
    print("Test 5 PASS: retry_after_from_detail")

    # Test 6: friendly_upstream_error
    msg, etype = friendly_upstream_error(400, "DataInspectionFailed: input text")
    assert etype == "content_policy_rejected", f"got {etype}"
    msg2, etype2 = friendly_upstream_error(500, "server error")
    assert etype2 == "upstream_transient_error", f"got {etype2}"
    print("Test 6 PASS: friendly_upstream_error")

    # Test 7: error_status
    assert error_status(NoAccountError()) == 503
    assert error_status(UpstreamError(400, "x", "content_policy_rejected")) == 400
    assert error_status(UpstreamError(500, "x", "upstream_transient_error")) == 502
    assert error_status(UpstreamError(404, "x")) == 404
    assert error_status(ValueError("x")) == 500
    print("Test 7 PASS: error_status")

    # Test 8: Pool persistence
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        tmp_path = tf.name
    os.unlink(tmp_path)  # remove so pool creates fresh

    s1 = Slot(id="x", name="acct-x", region="cn")
    pool = Pool([s1], state_path=tmp_path)
    pool.cool("x", 60, "test")
    assert os.path.exists(tmp_path), "state file should be created"

    # Recreate pool and verify state restored
    s2 = Slot(id="x", name="acct-x", region="cn")
    pool2 = Pool([s2], state_path=tmp_path)
    assert not s2.available(), "cooldown should be restored from state file"
    os.unlink(tmp_path)
    print("Test 8 PASS: Pool persistence")

    print("\n=== All 8 tests PASSED ===")