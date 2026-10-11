"""Qoder 日志系统 —— 内存环 + stdout + 文件三 sink，对应 Go: logger/logger.go。

功能：
  - 三级日志：debug / info / error
  - 内存环缓冲（默认 2000 条），供 API 查询
  - stdout 输出（终端可见）
  - 文件 sink：按日轮转，旧日志 gzip 归档，保留 7 天
"""

from __future__ import annotations

import gzip
import io
import os
import shutil
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional

_LEVEL_PRIORITY = {"debug": 0, "info": 1, "error": 2}

_MAX_ENTRIES = 2000
_RETAIN_DAYS = 7

_lock = threading.RLock()
_entries: list[Entry] = []
_next_seq = 1
_current_level = "info"

_file_lock = threading.Lock()
_file_dir: str = ""
_file_handle = None
_file_date: str = ""


@dataclass
class Entry:
    seq: int = 0
    time: str = ""
    level: str = ""
    message: str = ""


@dataclass
class LogPage:
    entries: list = None
    last_seq: int = 0

    def __post_init__(self):
        if self.entries is None:
            self.entries = []


def set_level(level: str):
    global _current_level
    with _lock:
        _current_level = level


def _should_log(level: str) -> bool:
    with _lock:
        return _LEVEL_PRIORITY.get(level, 1) >= _LEVEL_PRIORITY.get(_current_level, 1)


def _log(level: str, message: str):
    if not _should_log(level):
        return

    with _lock:
        global _next_seq
        entry = Entry(
            seq=_next_seq,
            time=time.strftime("%Y-%m-%d %H:%M:%S"),
            level=level,
            message=message,
        )
        _next_seq += 1
        _entries.append(entry)
        if len(_entries) > _MAX_ENTRIES:
            del _entries[: len(_entries) - _MAX_ENTRIES]

    line = f"[{entry.time}][{level}] {message}"
    print(line, flush=True)

    with _file_lock:
        f = _ensure_file()
        if f is not None:
            try:
                f.write(line + "\n")
                f.flush()
            except Exception:
                pass


def debug(message: str):
    _log("debug", message)


def info(message: str):
    _log("info", message)


def error(message: str):
    _log("error", message)


def get_logs(limit: int = 0) -> list[Entry]:
    with _lock:
        if limit <= 0 or limit > len(_entries):
            limit = len(_entries)
        start = len(_entries) - limit
        return list(_entries[start:])


def get_logs_since(after_seq: int, limit: int = 0) -> LogPage:
    with _lock:
        result = []
        for i in range(len(_entries) - 1, -1, -1):
            if _entries[i].seq <= after_seq:
                break
            result.append(_entries[i])
        result.reverse()
        if limit > 0 and len(result) > limit:
            result = result[len(result) - limit:]
        last = _entries[-1].seq if _entries else 0
        return LogPage(entries=result, last_seq=last)


def clear():
    with _lock:
        _entries.clear()


def init_file(dir_path: str) -> bool:
    global _file_dir
    if not dir_path:
        return False
    try:
        os.makedirs(dir_path, exist_ok=True)
    except Exception:
        return False
    with _file_lock:
        _file_dir = dir_path
    _archive_old_plain_logs(dir_path)
    _cleanup_old_archives(dir_path, _RETAIN_DAYS)
    with _file_lock:
        f = _ensure_file()
        if f is None:
            _file_dir = ""
            return False
    return True


def close():
    global _file_handle, _file_date
    with _file_lock:
        if _file_handle is not None:
            try:
                _file_handle.close()
            except Exception:
                pass
            _file_handle = None
            _file_date = ""


def _ensure_file():
    global _file_handle, _file_date
    if not _file_dir:
        return None
    today = time.strftime("%Y%m%d")
    if _file_handle is not None and _file_date == today:
        return _file_handle
    if _file_handle is not None:
        try:
            _file_handle.close()
        except Exception:
            pass
        _file_handle = None
        old_dir = _file_dir
        threading.Thread(
            target=lambda: (_archive_old_plain_logs(old_dir), _cleanup_old_archives(old_dir, _RETAIN_DAYS)),
            daemon=True,
        ).start()
    path = os.path.join(_file_dir, f"qccg-{today}.log")
    try:
        _file_handle = open(path, "a", encoding="utf-8")
        _file_date = today
        return _file_handle
    except Exception:
        _file_handle = None
        _file_date = ""
        return None


def _archive_old_plain_logs(dir_path: str):
    today = time.strftime("%Y%m%d")
    try:
        for name in os.listdir(dir_path):
            if not name.startswith("qccg-") or not name.endswith(".log") or name.endswith(".log.gz"):
                continue
            date_part = name[len("qccg-"):-len(".log")]
            if date_part == today:
                continue
            src = os.path.join(dir_path, name)
            dst = src + ".gz"
            try:
                with open(src, "rb") as fin, gzip.open(dst, "wb") as gz:
                    shutil.copyfileobj(fin, gz)
                os.remove(src)
            except Exception:
                pass
    except Exception:
        pass


def _cleanup_old_archives(dir_path: str, retain: int):
    if retain <= 0:
        return
    cutoff = time.time() - retain * 86400
    try:
        for name in os.listdir(dir_path):
            if not name.startswith("qccg-"):
                continue
            if not (name.endswith(".log") or name.endswith(".log.gz")):
                continue
            p = os.path.join(dir_path, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    print("=== qoder_logger.py 自测 ===")

    clear()
    set_level("debug")

    debug("debug msg")
    info("info msg")
    error("error msg")

    logs = get_logs()
    assert len(logs) == 3, f"expected 3, got {len(logs)}"
    assert logs[0].level == "debug"
    assert logs[1].level == "info"
    assert logs[2].level == "error"
    assert logs[0].seq == 1
    assert logs[2].seq == 3
    print("Test 1 PASS: basic logging + seq")

    page = get_logs_since(1)
    assert page.last_seq == 3
    assert len(page.entries) == 2
    assert page.entries[0].seq == 2
    assert page.entries[1].seq == 3
    print("Test 2 PASS: get_logs_since")

    set_level("error")
    debug("should not appear")
    info("should not appear")
    error("should appear")
    logs = get_logs()
    assert len(logs) == 4
    assert logs[-1].level == "error"
    assert logs[-1].message == "should appear"
    print("Test 3 PASS: level filtering")

    set_level("info")
    clear()
    assert len(get_logs()) == 0
    print("Test 4 PASS: clear")

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ok = init_file(td)
        assert ok, "init_file failed"
        info("file sink test")
        logs = get_logs()
        assert len(logs) == 1
        files = os.listdir(td)
        assert any(f.startswith("qccg-") and f.endswith(".log") for f in files), f"no log file: {files}"
        close()
        print("Test 5 PASS: file sink")

    print("\n=== All 5 tests PASSED ===")