"""CodeArts 隔离浏览器登录 —— 单元测试。

覆盖两条真正会出错的行为：
1. 授权页必须在**独立 profile** 里打开（否则默认浏览器的华为云 Cookie 会把
   授权页变成自动登录，登录不到新账号）；
2. 授权完成后一次性 profile 必须被删除（否则本机留下可复用的登录态）。
"""

import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CODEARTS_DIR = os.path.join(_ROOT, "codearts")
if _CODEARTS_DIR not in sys.path:
    sys.path.insert(0, _CODEARTS_DIR)

import codearts_browser


class _FakePopen:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class LaunchArgsTests(unittest.TestCase):
    def test_isolated_profile_and_no_first_run(self):
        args = codearts_browser.build_launch_args(
            r"C:\browser\msedge.exe", r"C:\profiles\p1", "https://example/authorize")
        self.assertEqual(args[0], r"C:\browser\msedge.exe")
        self.assertIn("--user-data-dir=C:\\profiles\\p1", args)
        self.assertIn("--no-first-run", args)
        self.assertIn("--no-default-browser-check", args)
        self.assertIn("--new-window", args)
        self.assertEqual(args[-1], "https://example/authorize")

    def test_find_browser_returns_none_when_missing(self):
        original = codearts_browser.BROWSER_CANDIDATES
        codearts_browser.BROWSER_CANDIDATES = [r"C:\definitely\missing\chrome.exe"]
        try:
            self.assertIsNone(codearts_browser.find_browser())
        finally:
            codearts_browser.BROWSER_CANDIDATES = original


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self._orig_popen = codearts_browser.subprocess.Popen
        self._orig_root = codearts_browser.PROFILE_ROOT
        self.tmp = tempfile.mkdtemp(prefix="ca_profile_root_")
        codearts_browser.PROFILE_ROOT = self.tmp
        codearts_browser.subprocess.Popen = _FakePopen

    def tearDown(self):
        codearts_browser.subprocess.Popen = self._orig_popen
        codearts_browser.PROFILE_ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_launch_creates_fresh_profile_and_reports_browser(self):
        res = codearts_browser.launch("https://example/authorize")
        self.assertTrue(res["ok"])
        self.assertTrue(res["browser"])
        self.assertTrue(os.path.isdir(res["profile"]))
        # 全新 profile：目录里不得有任何既有 Cookie / 偏好文件
        self.assertEqual(os.listdir(res["profile"]), [])

    def test_launch_without_browser_fails_gracefully(self):
        original = codearts_browser.BROWSER_CANDIDATES
        codearts_browser.BROWSER_CANDIDATES = [r"C:\definitely\missing\chrome.exe"]
        try:
            res = codearts_browser.launch("https://example/authorize")
        finally:
            codearts_browser.BROWSER_CANDIDATES = original
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])

    def test_cleanup_removes_profile(self):
        res = codearts_browser.launch("https://example/authorize")
        profile = res["profile"]
        self.assertTrue(codearts_browser.cleanup_profile(profile))
        self.assertFalse(os.path.isdir(profile))

    def test_cleanup_closes_browser_before_removing_profile(self):
        """profile 被浏览器占用时删不掉，必须先结束进程。"""
        res = codearts_browser.launch("https://example/authorize")
        proc = res["process"]
        self.assertTrue(codearts_browser.cleanup_profile(res["profile"], proc))
        self.assertTrue(proc.terminated)
        self.assertFalse(os.path.isdir(res["profile"]))

    def test_cleanup_ignores_missing_profile(self):
        self.assertFalse(codearts_browser.cleanup_profile(None))
        self.assertFalse(codearts_browser.cleanup_profile(os.path.join(self.tmp, "nope")))

    def test_prune_removes_expired_profiles(self):
        stale = os.path.join(self.tmp, "stale")
        fresh = os.path.join(self.tmp, "fresh")
        os.makedirs(stale)
        os.makedirs(fresh)
        os.utime(stale, (0, 0))
        removed = codearts_browser.prune_old_profiles(self.tmp, ttl_seconds=60)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))


if __name__ == "__main__":
    unittest.main()
