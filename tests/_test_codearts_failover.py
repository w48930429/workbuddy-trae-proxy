import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import wb_proxy
from codearts.codearts_accounts import CodeArtsAccount, CodeArtsAccountPool
from codearts.codearts_client import CodeArtsError


class FakeCodeArtsHandler:
    def __init__(self, path="/v1/chat/completions"):
        self.path = path
        self.wfile = io.BytesIO()
        self.headers = {"Content-Type": "application/json"}
        self.response_status = None
        self.response_headers = {}

    def send_response(self, code):
        self.response_status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def _error(self, status, message, err_type="upstream_error", headers=None, **kwargs):
        self.response_status = status
        if headers:
            self.response_headers.update(headers)
        body = json.dumps({"error": {"message": message, "type": err_type}})
        self.wfile.write(body.encode("utf-8"))
        return status, body

    def _json(self, status, payload):
        self.response_status = status
        body = json.dumps(payload)
        self.wfile.write(body.encode("utf-8"))
        return status, body


class TestCodeArtsFailover(unittest.TestCase):
    def setUp(self):
        self.handler = FakeCodeArtsHandler()
        self.pool = CodeArtsAccountPool.__new__(CodeArtsAccountPool)
        self.pool.accounts = [
            CodeArtsAccount({
                "id": "acct_1",
                "label": "Account 1",
                "accessKey": "AK1",
                "secretKey": "SK1",
                "enabled": True,
            }),
            CodeArtsAccount({
                "id": "acct_2",
                "label": "Account 2",
                "accessKey": "AK2",
                "secretKey": "SK2",
                "enabled": True,
            }),
        ]
        import threading
        self.pool._lock = threading.RLock()
        self.pool._cursor = 0
        self.pool.accounts_dir = "."
        self.pool.file_path = "dummy.json"

    def test_non_stream_failover_on_concurrency_limit(self):
        handler_fn = wb_proxy.Handler._route_to_codearts.__get__(self.handler, wb_proxy.Handler)

        call_records = []

        def fake_complete(payload, session=None, proxy=""):
            ak = session.access_key
            call_records.append(ak)
            if ak == "AK1":
                raise CodeArtsError(400, "并发会话数已达上限(3个)，请关闭部分会话后重试。", code="TM.00001041")
            return {"id": "chatcmpl-ok", "choices": [{"message": {"content": "Hello from AK2"}}]}

        with patch("wb_proxy.get_codearts_pool", return_value=self.pool), \
             patch("wb_proxy.codearts_route.complete", side_effect=fake_complete):
            handler_fn({"model": "codearts-kimi-k3", "messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(call_records, ["AK1", "AK2"])
        self.assertEqual(self.handler.response_status, 200)
        resp_data = json.loads(self.handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(resp_data["choices"][0]["message"]["content"], "Hello from AK2")
        # 并发满（TM.00001041）是临时状态，不是账号故障 —— 拉黑只会把请求
        # 推回无计数的单会话路径，继续硬打上游。
        self.assertFalse(self.pool.accounts[0].is_cooling())
        self.assertEqual(self.pool.accounts[0].in_flight, 0)

    def test_stream_failover_on_concurrency_limit(self):
        handler_fn = wb_proxy.Handler._route_to_codearts.__get__(self.handler, wb_proxy.Handler)

        call_records = []

        def fake_stream(payload, session=None, proxy=""):
            ak = session.access_key
            call_records.append(ak)
            if ak == "AK1":
                raise CodeArtsError(400, "并发会话数已达上限(3个)，请关闭部分会话后重试。", code="TM.00001041")
            return iter([b"data: {\"choices\": [{\"delta\": {\"content\": \"chunk2\"}}]}\n\n", b"data: [DONE]\n\n"])

        with patch("wb_proxy.get_codearts_pool", return_value=self.pool), \
             patch("wb_proxy.codearts_route.stream", side_effect=fake_stream), \
             patch("wb_proxy.cors_origin_allowed", return_value=False):
            handler_fn({"model": "codearts-kimi-k3", "stream": True, "messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(call_records, ["AK1", "AK2"])
        self.assertEqual(self.handler.response_status, 200)
        body = self.handler.wfile.getvalue().decode("utf-8")
        self.assertIn("chunk2", body)
        self.assertFalse(self.pool.accounts[0].is_cooling())
        self.assertEqual(self.pool.accounts[1].in_flight, 0)



    def test_all_accounts_exhausted_answers_429_not_400(self):
        """全池并发满 → 429（可重试）。400 会被客户端当成「模型不可用」换模型。"""
        handler_fn = wb_proxy.Handler._route_to_codearts.__get__(self.handler, wb_proxy.Handler)

        call_records = []

        def fake_complete(payload, session=None, proxy=""):
            call_records.append(session.access_key)
            raise CodeArtsError(400, "并发会话数已达上限(3个)，请关闭部分会话后重试。", code="TM.00001041")

        with patch("wb_proxy.get_codearts_pool", return_value=self.pool), \
             patch("wb_proxy.codearts_route.complete", side_effect=fake_complete), \
             patch("wb_proxy.time.sleep", return_value=None), \
             patch("wb_proxy.CODEARTS_QUEUE_WAIT_SECONDS", 0):
            handler_fn({"model": "codearts-kimi-k3", "messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(self.handler.response_status, 429)
        for acct in self.pool.accounts:
            self.assertEqual(acct.in_flight, 0)

    def test_busy_pool_never_falls_back_to_single_session(self):
        """池里有启用账号但全满时，绝不能拿同一套凭据走 load_session 硬打。"""
        handler_fn = wb_proxy.Handler._route_to_codearts.__get__(self.handler, wb_proxy.Handler)
        for acct in self.pool.accounts:
            acct.in_flight = 3   # 两账号都到并发上限

        calls = []

        def fake_complete(payload, session=None, proxy=""):
            calls.append(session.access_key)
            return {"id": "should-not-happen", "choices": []}

        with patch("wb_proxy.get_codearts_pool", return_value=self.pool), \
             patch("wb_proxy.codearts_route.complete", side_effect=fake_complete), \
             patch("wb_proxy.codearts_route.load_session") as mock_session, \
             patch("wb_proxy.time.sleep", return_value=None), \
             patch("wb_proxy.CODEARTS_QUEUE_WAIT_SECONDS", 0):
            handler_fn({"model": "codearts-kimi-k3", "messages": [{"role": "user", "content": "hi"}]})

        mock_session.assert_not_called()
        self.assertEqual(calls, [])
        self.assertEqual(self.handler.response_status, 429)

    def test_slot_count_never_exceeds_cap(self):
        """选号 + 占位必须是原子的：两次抢占不可能都拿到第 4 个槽位。"""
        import threading
        acct = self.pool.accounts[0]
        results = []
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            results.append(acct.acquire_slot(timeout=0))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r), 3)
        self.assertLessEqual(acct.in_flight, 3)

    def _make_sts_expired(self, acct):
        acct.security_token = "STS"
        acct.refresh_token = "RT"
        acct.login_context = {"pkcePair": {"codeVerifier": "v"}, "dpopKeyPair": {"kty": "EC"}}
        acct.expires_at = "2020-01-01T00:00:00Z"

    def test_sts_refresh_failure_fails_over_to_next_account(self):
        """第一位账号 refresh token 失效时，必须换下一个账号，不能拖死整个池。"""
        acct1, acct2 = self.pool.accounts
        self._make_sts_expired(acct1)
        self._make_sts_expired(acct2)

        def fake_refresh(account, timeout=30, proxy=""):
            if account.id == "acct_1":
                raise RuntimeError("HuaweiCloud STS Token refresh failed (HTTP 400): "
                                   "STS5.1806 invalid refresh token: the refresh token has been used")
            return {}

        with patch("codearts.codearts_oauth.refresh_sts_token_for_account",
                   side_effect=fake_refresh), \
             patch.object(self.pool, "save"):
            account, session = self.pool.acquire_account()

        self.assertIsNotNone(account)
        self.assertEqual(account.id, "acct_2")
        self.assertEqual(acct1.in_flight, 0)
        self.assertTrue(acct1.is_credential_error())
        self.assertFalse(acct2.is_credential_error())
        self.assertEqual(session.access_key, "AK2")

    def test_all_sts_refresh_failures_surface_credentials_error_not_429(self):
        """全池续期都失败时回 502 credentials_error，而不是误导性的 429 并发满。"""
        handler_fn = wb_proxy.Handler._route_to_codearts.__get__(self.handler, wb_proxy.Handler)
        for acct in self.pool.accounts:
            self._make_sts_expired(acct)

        def fake_refresh(account, timeout=30, proxy=""):
            raise RuntimeError("STS5.1806 invalid refresh token: the refresh token has been used")

        with patch("wb_proxy.get_codearts_pool", return_value=self.pool), \
             patch("codearts.codearts_oauth.refresh_sts_token_for_account",
                   side_effect=fake_refresh), \
             patch("wb_proxy.CODEARTS_QUEUE_WAIT_SECONDS", 0):
            handler_fn({"model": "codearts-kimi-k3", "messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(self.handler.response_status, 502)
        body = json.loads(self.handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(body["error"]["type"], "credentials_error")
        self.assertIn("凭据已失效", body["error"]["message"])
        for acct in self.pool.accounts:
            self.assertEqual(acct.in_flight, 0)

    def test_cursor_round_robin_does_not_stick_to_first(self):
        """两个健康账号应轮流被选中，旧排序键会让 accounts[0] 永远压住第二位。"""
        acct1, acct2 = self.pool.accounts
        first, _ = self.pool.acquire_account()
        if first is not None:
            first.release_slot()
        second, _ = self.pool.acquire_account()
        if second is not None:
            second.release_slot()
        self.assertEqual([first.id, second.id], ["acct_1", "acct_2"])

    def test_sync_legacy_account_replaces_stale_credentials(self):
        """重新授权写入 legacy 单账号文件后，必须覆盖池里同账号的失效凭据。"""
        import tempfile
        acct = CodeArtsAccount({
            "id": "acct_1", "label": "hw-x", "domainId": "dom_1",
            "accessKey": "OLD", "secretKey": "OLDSK", "enabled": True,
        })
        acct.mark_credential_error(300, "STS5.1806 invalid refresh token")
        pool = CodeArtsAccountPool.__new__(CodeArtsAccountPool)
        pool.accounts = [acct]
        import threading
        pool._lock = threading.RLock()
        pool._cursor = 0

        with tempfile.TemporaryDirectory() as tmp:
            pool.accounts_dir = tmp
            pool.file_path = tmp + "/codearts_accounts.json"
            pool.legacy_single_file = tmp + "/codearts_account.json"
            with open(pool.legacy_single_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "label": "hw-x", "domainId": "dom_1",
                    "accessKey": "NEW", "secretKey": "NEWSK", "securitytoken": "NEWSTS",
                    "expires_at": "2030-01-01T00:00:00Z", "refresh_token": "NEWRT",
                    "loginContext": {"pkcePair": {"codeVerifier": "v"}},
                    "source": "web_oauth", "region": "cn",
                }, fh)
            with patch.object(pool, "save"):
                target = pool.sync_legacy_account()

        self.assertIs(target, acct)
        self.assertEqual(acct.access_key, "NEW")
        self.assertEqual(acct.secret_key, "NEWSK")
        self.assertEqual(acct.refresh_token, "NEWRT")
        self.assertFalse(acct.is_credential_error())
        self.assertTrue(acct.enabled)

    def test_sync_legacy_prefers_enabled_web_over_same_label_desktop(self):
        """同名 desktop 与 web_oauth 共存时，网页授权只能更新启用中的 web 条目。"""
        import tempfile
        import threading
        desktop = CodeArtsAccount({
            "id": "d1", "label": "hw-x", "domainId": "dom_1",
            "accessKey": "DESKTOP", "secretKey": "DS",
            "source": "desktop", "enabled": False,
        })
        web = CodeArtsAccount({
            "id": "w1", "label": "hw-x", "domainId": "dom_1",
            "accessKey": "OLD", "secretKey": "WS",
            "source": "web_oauth", "enabled": True,
        })
        web.mark_credential_error(300, "STS5.1806 invalid refresh token")
        pool = CodeArtsAccountPool.__new__(CodeArtsAccountPool)
        pool.accounts = [desktop, web]
        pool._lock = threading.RLock()
        pool._cursor = 0

        with tempfile.TemporaryDirectory() as tmp:
            pool.accounts_dir = tmp
            pool.file_path = tmp + "/codearts_accounts.json"
            pool.legacy_single_file = tmp + "/codearts_account.json"
            with open(pool.legacy_single_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "label": "hw-x", "domainId": "dom_1",
                    "accessKey": "NEW", "secretKey": "NEWSK", "securitytoken": "NEWSTS",
                    "expires_at": "2030-01-01T00:00:00Z", "refresh_token": "NEWRT",
                    "loginContext": {"pkcePair": {"codeVerifier": "v"}},
                    "source": "web_oauth", "region": "cn",
                }, fh)
            with patch.object(pool, "save"):
                target = pool.sync_legacy_account()

        self.assertIs(target, web)
        self.assertEqual(web.access_key, "NEW")
        self.assertFalse(web.is_credential_error())
        self.assertEqual(desktop.access_key, "DESKTOP")
        self.assertEqual(desktop.source, "desktop")
        self.assertFalse(desktop.is_credential_error())

if __name__ == "__main__":
    unittest.main()
