"""跨上游排队可见性：谁在等、等了多久、同时有几个在等。

只做统计与打标，不参与调度。排队本身仍由各上游决定（CodeArts 的单账号
3 会话槽位、Qoder 免费档的 p3 队列）。目的只有一个：让日志和 /health 能
一眼分清「本地在排队等槽位」和「上游根本没回包」—— 这两种情况在客户端
看来都是终端不动，但对使用者的含义完全不同。
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_STATE = {
    "active": 0,
    "total": 0,
    "last_wait_ms": 0,
    "last_at": 0.0,
    "upstreams": {},
}


def enter(upstream: str) -> float:
    """标记一次排队开始，返回起始时间戳（交给 leave）。"""
    now = time.time()
    with _LOCK:
        _STATE["active"] += 1
        _STATE["total"] += 1
        per = _STATE["upstreams"].setdefault(upstream, {"active": 0, "total": 0})
        per["active"] += 1
        per["total"] += 1
    return now


def leave(upstream: str, started_at: float, outcome: str = "") -> int:
    """标记排队结束，返回本次等待毫秒数。"""
    waited_ms = int(max(0.0, time.time() - started_at) * 1000)
    with _LOCK:
        _STATE["active"] = max(0, _STATE["active"] - 1)
        _STATE["last_wait_ms"] = waited_ms
        _STATE["last_at"] = time.time()
        per = _STATE["upstreams"].setdefault(upstream, {"active": 0, "total": 0})
        per["active"] = max(0, per["active"] - 1)
        if outcome:
            per["last_outcome"] = outcome
    return waited_ms


def active() -> int:
    with _LOCK:
        return _STATE["active"]


def snapshot() -> dict:
    with _LOCK:
        return {
            "active": _STATE["active"],
            "total": _STATE["total"],
            "last_wait_ms": _STATE["last_wait_ms"],
            "last_at": int(_STATE["last_at"]) or None,
            "upstreams": {name: dict(val) for name, val in _STATE["upstreams"].items()},
        }