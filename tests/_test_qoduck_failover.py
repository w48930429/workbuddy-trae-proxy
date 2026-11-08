import io
import json
import unittest
from unittest.mock import MagicMock, patch

import wb_proxy


class FakeHandler:
    def __init__(self, path="/qoder/v1/chat/completions", realm=""):
        self.path = path
        self.wfile = io.BytesIO()
        self.headers = {"Content-Type": "application/json"}
        self._realm = realm
        self.response_status = None
        self.response_headers = {}
        self._fallback_to_workbuddy_cn = MagicMock(return_value=(200, "wb_fallback_success"))
        self._chat_nonstream_response = MagicMock(return_value=(200, "ok"))
        self._chat_stream_response = MagicMock(return_value=(200, "ok"))

    def send_response(self, status):
        self.response_status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def _request_realm(self, override=""):
        return override or self._realm

    def _error(self, code, message, error_type="server_error"):
        self.response_status = code
        self.error_body = {"error": {"message": message, "type": error_type, "code": code}}
        return code, self.error_body

    _route_to_qoder = wb_proxy.Handler._route_to_qoder

class TestQoDuckFailover(unittest.TestCase):
    def setUp(self):
        self.call_history = []

    def fake_call_qoder_builder(self, results):
        """results is a list of (status, body) tuples returned in sequence."""
        def fake_call_qoder(payload, on_delta=None, proxy="", realm=""):
            self.call_history.append((payload.get("model"), realm, bool(on_delta)))
            idx = len(self.call_history) - 1
            if idx < len(results):
                status, body = results[idx]
            else:
                status, body = (500, "exhausted mocks")
            if on_delta and status == 200:
                on_delta({"id": "chunk1", "choices": [{"delta": {"content": "hello"}}]})
            return status, body
        return fake_call_qoder

    @patch("wb_proxy.qoder_route.call_qoder")
    def test_qoduck_global_fails_to_cn_nonstream(self, mock_call):
        # Starts on global (e.g. local-qoder / sk-in-*), global fails with 403, cn succeeds
        mock_call.side_effect = self.fake_call_qoder_builder([
            (403, json.dumps({"error": {"message": "10605 queue limit"}})),
            (200, json.dumps({"id": "chatcmpl-test", "choices": [{"message": {"content": "from cn"}}]})),
        ])
        handler = FakeHandler(realm="global")
        payload = {"model": "QoDuck", "messages": [{"role": "user", "content": "hi"}]}

        handler._route_to_qoder(payload)

        self.assertEqual(len(self.call_history), 2)
        self.assertEqual(self.call_history[0], ("QoDuck", "global", False))
        self.assertEqual(self.call_history[1], ("QoDuck", "cn", False))
        self.assertEqual(handler.response_status, 200)
        resp_json = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(resp_json["choices"][0]["message"]["content"], "from cn")

    @patch("wb_proxy.qoder_route.call_qoder")
    def test_qoduck_cn_fails_to_global_nonstream(self, mock_call):
        # Starts on cn (e.g. local-qoder-cn / sk-cn-*), cn fails with 429, global succeeds
        mock_call.side_effect = self.fake_call_qoder_builder([
            (429, json.dumps({"error": {"message": "rate limit"}})),
            (200, json.dumps({"id": "chatcmpl-test", "choices": [{"message": {"content": "from global"}}]})),
        ])
        handler = FakeHandler(realm="cn")
        payload = {"model": "QoDuck", "messages": [{"role": "user", "content": "hi"}]}

        handler._route_to_qoder(payload)

        self.assertEqual(len(self.call_history), 2)
        self.assertEqual(self.call_history[0], ("QoDuck", "cn", False))
        self.assertEqual(self.call_history[1], ("QoDuck", "global", False))
        self.assertEqual(handler.response_status, 200)
        resp_json = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(resp_json["choices"][0]["message"]["content"], "from global")

    @patch("wb_proxy.qoder_route.call_qoder")
    def test_qoduck_both_fail_to_workbuddy_cn_nonstream(self, mock_call):
        # Global fails, then CN fails -> falls back to WorkBuddy CN deepseek-v4.1-flash
        mock_call.side_effect = self.fake_call_qoder_builder([
            (403, json.dumps({"error": {"message": "10605 queue limit"}})),
            (503, json.dumps({"error": {"message": "no_available_account"}})),
        ])
        handler = FakeHandler(realm="global")
        payload = {"model": "QoDuck", "messages": [{"role": "user", "content": "hi"}]}

        handler._route_to_qoder(payload)

        self.assertEqual(len(self.call_history), 2)
        self.assertEqual(self.call_history[0], ("QoDuck", "global", False))
        self.assertEqual(self.call_history[1], ("QoDuck", "cn", False))
        handler._fallback_to_workbuddy_cn.assert_called_once()
        args, kwargs = handler._fallback_to_workbuddy_cn.call_args
        self.assertEqual(kwargs.get("wb_model"), "deepseek-v4.1-flash")
        self.assertFalse(kwargs.get("want_stream"))

    @patch("wb_proxy.qoder_route.call_qoder")
    def test_qoduck_global_fails_to_cn_stream(self, mock_call):
        # Streaming: global fails before headers sent -> cn succeeds with SSE chunks
        mock_call.side_effect = self.fake_call_qoder_builder([
            (502, "bad gateway"),
            (200, "done"),
        ])
        handler = FakeHandler(realm="global")
        payload = {"model": "QoDuck", "stream": True, "messages": [{"role": "user", "content": "hi"}]}

        handler._route_to_qoder(payload)

        self.assertEqual(len(self.call_history), 2)
        self.assertEqual(self.call_history[0], ("QoDuck", "global", True))
        self.assertEqual(self.call_history[1], ("QoDuck", "cn", True))
        self.assertEqual(handler.response_status, 200)
        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("data: [DONE]", output)
        self.assertIn("hello", output)

    @patch("wb_proxy.qoder_route.call_qoder")
    def test_stream_no_failover_if_headers_already_sent(self, mock_call):
        # If chunks were already emitted to client, DO NOT switch realms (breaks SSE protocol)
        def fake_call_emitting_then_failing(payload, on_delta=None, proxy="", realm=""):
            self.call_history.append((payload.get("model"), realm, bool(on_delta)))
            # Emit first chunk
            on_delta({"id": "chunk1", "choices": [{"delta": {"content": "partial"}}]})
            # Then fail
            return 500, "mid-stream abort"

        mock_call.side_effect = fake_call_emitting_then_failing
        handler = FakeHandler(realm="global")
        payload = {"model": "QoDuck", "stream": True, "messages": [{"role": "user", "content": "hi"}]}

        handler._route_to_qoder(payload)

        # Must NOT attempt realm 2
        self.assertEqual(len(self.call_history), 1)
        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("chatcmpl-err", output)
        self.assertIn("data: [DONE]", output)
        handler._fallback_to_workbuddy_cn.assert_not_called()

    @patch("wb_proxy.qoder_route.call_qoder")
    def test_content_violation_never_failovers(self, mock_call):
        # 400 content violation should immediately fail, no fallback
        mock_call.side_effect = self.fake_call_qoder_builder([
            (400, "Content Rejected: Sensitive content detected"),
        ])
        handler = FakeHandler(realm="global")
        payload = {"model": "QoDuck", "messages": [{"role": "user", "content": "bad"}]}

        handler._route_to_qoder(payload)

        self.assertEqual(len(self.call_history), 1)
        handler._fallback_to_workbuddy_cn.assert_not_called()
        self.assertEqual(handler.response_status, 400)

    def test_qoduck_dispatch_on_v1_chat(self):
        # Calling /v1/chat/completions with QoDuck directly triggers _route_to_qoder
        handler = FakeHandler(path="/v1/chat/completions", realm="global")
        handler._route_to_qoder = MagicMock(return_value="routed_to_qoder")
        payload = {"model": "QoDuck", "messages": [{"role": "user", "content": "hi"}]}

        # Test _dispatch_chat_post logic:
        model_name = payload.get("model") or ""
        qoder_forced = False
        self.assertTrue(wb_proxy._qoder_enabled())
        self.assertTrue(wb_proxy._is_qoder_qoduck(model_name))
        if wb_proxy._qoder_enabled() and (qoder_forced or wb_proxy._is_qoder_qoduck(model_name)):
            res = handler._route_to_qoder(payload)
        self.assertEqual(res, "routed_to_qoder")

    @patch("wb_proxy.open_upstream")
    @patch("wb_proxy.Handler._chat_nonstream_response")
    def test_fallback_to_workbuddy_cn_pops_realm(self, mock_nonstream, mock_open_upstream):
        mock_open_upstream.return_value = (MagicMock(), MagicMock(uid="fake_account"))
        mock_nonstream.return_value = (200, "ok")
        handler = FakeHandler()
        # Bind the real _fallback_to_workbuddy_cn
        handler._fallback_to_workbuddy_cn = wb_proxy.Handler._fallback_to_workbuddy_cn.__get__(handler)

        payload = {"model": "QoDuck", "realm": "global", "messages": [{"role": "user", "content": "test"}]}
        handler._fallback_to_workbuddy_cn(payload, want_stream=False, t_start=1000.0, wb_model="deepseek-v4.1-flash")

        mock_open_upstream.assert_called_once()
        called_payload, = mock_open_upstream.call_args[0]
        called_kwargs = mock_open_upstream.call_args[1]
        self.assertEqual(called_kwargs.get("target_realm"), "cn")
        self.assertNotIn("realm", called_payload)
        self.assertEqual(called_payload.get("model"), "deepseek-v4.1-flash")


if __name__ == "__main__":
    unittest.main()
