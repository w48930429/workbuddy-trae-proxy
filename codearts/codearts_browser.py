"""隔离浏览器实例启动器 —— 用于 CodeArts 网页登录新账号。

背景
----
华为云 CodeArts 授权页 (https://codearts.huaweicloud.com/portal/authorize)
依赖浏览器里已有的华为云 Cookie。如果直接在日常浏览器的标签页里打开，
浏览器会自动带着已登录的华为云会话完成授权 —— 结果就是「看起来登录了，
实际拿回的还是那个旧账号」，无法真正登录一个新账号。

本模块的做法与华为云 CodeArts Agent 桌面端扩展一致（用 openExternal 打开
授权页），但额外给浏览器实例挂一个**一次性的独立 user-data-dir**：
- 独立 profile ⇒ 不带任何既有 Cookie ⇒ 授权页会要求真实输入账号密码，
  可以登录一个全新的华为云账号；
- 授权回调仍是 http://127.0.0.1:{网关端口}/oauth/callback，由本机网关接收，
  与浏览器 profile 无关；
- 授权成功后该临时 profile 立即删除，避免在本机留下可复用的登录 Cookie。

无浏览器可执行文件时，调用方应退回到「返回授权 URL，由看板 window.open
打开默认浏览器」的老路���。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
PROFILE_ROOT = os.path.join(PROJECT_ROOT, "data", "codearts_browser_profiles")

# 常见 Chromium 内核浏览器的可执行文件位置（按优先级排列）。
BROWSER_CANDIDATES: List[str] = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
]

# 旧 profile 的最长保留时间（秒）：授权失败时保留一段时间便于重试，
# 超时则清理，避免磁盘上堆积浏览器 profile。
PROFILE_TTL_SECONDS = 24 * 3600


def find_browser() -> Optional[str]:
    """返回第一个存在的 Chromium 内核浏览器路径，找不到返回 None。"""
    for path in BROWSER_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def build_launch_args(executable: str, profile_dir: str, url: str) -> List[str]:
    """拼装独立 profile 的启动参数。

    --user-data-dir 指向一次性目录 ⇒ 全新浏览器实例，不共享日常浏览器的
    Cookie 与登录态；--no-first-run / --no-default-browser-check 抑制首次
    启动向导，否则用户会先看到一个「选择默认浏览器」弹窗。
    """
    return [
        executable,
        "--user-data-dir=%s" % profile_dir,
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]


def new_profile_dir() -> str:
    """创建一个新的临时 profile 目录（不落盘任何既有 Cookie）。"""
    os.makedirs(PROFILE_ROOT, exist_ok=True)
    path = os.path.join(PROFILE_ROOT, "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8]))
    os.makedirs(path, exist_ok=True)
    return path


def prune_old_profiles(root: str = PROFILE_ROOT, ttl_seconds: int = PROFILE_TTL_SECONDS) -> int:
    """清理超时的旧 profile 目录，返回删除数量。"""
    if not os.path.isdir(root):
        return 0
    now = time.time()
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) < ttl_seconds:
                continue
        except OSError:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                continue
        removed += 1
    return removed


def cleanup_profile(profile_dir: Optional[str], proc: Any = None) -> bool:
    """删除一次性 profile 目录（授权完成后调用）。

    先结束浏览器进程：profile 被占用时目录删不掉，会在本机留下一份带
    华为云 Cookie 的浏览器 profile。
    """
    if proc is not None:
        close_browser(proc)
    if not profile_dir or not os.path.isdir(profile_dir):
        return False
    for _ in range(10):
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except OSError:
            pass
        if not os.path.isdir(profile_dir):
            return True
        time.sleep(0.3)
    return not os.path.isdir(profile_dir)


def launch(url: str, profile_dir: Optional[str] = None) -> Dict[str, Any]:
    """用独立 profile 启动浏览器并打开授权页。

    返回 {"ok": True, "browser": exe, "profile": dir, "process": Popen} 或
    {"ok": False, ...}；调用方在 ok=False 时应退回「把 URL 交给用户自己打开」。
    """
    executable = find_browser()
    if not executable:
        return {
            "ok": False,
            "error": "未找到可用的 Chromium 内核浏览器（Edge/Chrome/Brave），请手动打开授权链接",
        }
    if not url:
        return {"ok": False, "error": "缺少授权链接"}
    try:
        prune_old_profiles()
    except OSError:
        pass
    profile = profile_dir or new_profile_dir()
    try:
        proc = subprocess.Popen(
            build_launch_args(executable, profile, url),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        cleanup_profile(profile)
        return {"ok": False, "error": "启动浏览器失败: %s" % exc}
    return {"ok": True, "browser": executable, "profile": profile, "process": proc}


def close_browser(proc: Any) -> bool:
    """关闭由 launch() 启动的隔离浏览器实例。

    profile 目录被浏览器进程占用时删不掉，必须先结束进程 —— 否则授权完成后
    会在本机留下一份带华为云 Cookie 的浏览器 profile。
    """
    if proc is None:
        return False
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            return False
    return True
