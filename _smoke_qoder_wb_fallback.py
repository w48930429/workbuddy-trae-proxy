"""Smoke test suite for two-way Qoder fallback and WorkBuddy CN failover."""
import json
import io
import unittest
from unittest.mock import patch, MagicMock

import wb_proxy
from wb_proxy import (
    Handler,
    _is_qoder_deepseek,
    _is_qoder_qwen_flash,
    _is_qoder_exhausted,
)


class DummyHandler(Handler):
    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers = {}
        self.path = "/qoder/v1/chat/completions"
        self._headers_buffer = []
        self.response_code = None
        self.response_headers = {}

    def send_response(self, code, message=None):
        self.response_code = code

    def send_header(self, keyword, value):
        self.response_headers[keyword.lower()] = value

    def end_headers(self):
        pass

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _error(self, code, message, error_type="server_error"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": {"message": message, "type": error_type}}).encode("utf-8"))


class TestQoderWbFallback(unittest.TestCase):
    def test_classification_helpers(self):
        self.assertTrue(_is_qoder_deepseek("DeepSeek-Flash"))
        self.assertTrue(_is_qoder_deepseek("deepseek-flash"))
        self.assertTrue(_is_qoder_deepseek("DeepSeek-V4-Pro"))
        self.assertTrue(_is_qoder_deepseek("dfmodel"))
        self.assertFalse(_is_qoder_deepseek("Qwen3.8-Flash"))

        self.assertTrue(_is_qoder_qwen_flash("Qwen3.8-Flash"))
        self.assertTrue(_is_qoder_qwen_flash("qwen3.8-flash"))
        self.assertTrue(_is_qoder_qwen_flash("qwen3.8flash"))
        self.assertTrue(_is_qoder_qwen_flash("qfmodel"))
        self.assertFalse(_is_qoder_qwen_flash("Qwen3.8-Max"))
        self.assertFalse(_is_qoder_qwen_flash("DeepSeek-Flash"))

    def test_exhaustion_detection(self):
        self.assertTrue(_is_qoder_exhausted(503, '{"error": {"type": "no_available_account"}}'))
        self.assertTrue(_is_qoder_exhausted(429, '{"error": {"message": "rate limit exceeded"}}'))
        self.assertTrue(_is_qoder_exhausted(403, '{"error": {"message": "account temporarily restricted"}}'))
        self.assertTrue(_is_qoder_exhausted(400, '{"error": {"message": "14018 额度不足"}}'))
        self.assertFalse(_is_qoder_exhausted(200, '{"choices": []}'))
        self.assertFalse(_is_qoder_exhausted(400, '{"error": {"message": "invalid parameter: max_tokens too high"}}'))

    def test_nonstream_ds_exhausted_fallback_to_qwen(self):
        handler = DummyHandler()
        payload = {
            "model": "DeepSeek-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            if p.get("model") == "DeepSeek-Flash":
                return 503, json.dumps({"error": {"type": "no_available_account", "message": "all out"}})
            elif p.get("model") == "Qwen3.8-Flash":
                return 200, json.dumps({"choices": [{"message": {"content": "Hello from Qwen"}}]})
            return 500, "error"

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder):
            handler._route_to_qoder(payload)

        self.assertEqual(calls, ["DeepSeek-Flash", "Qwen3.8-Flash"])
        self.assertEqual(handler.response_code, 200)
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello from Qwen")

    def test_nonstream_both_exhausted_fallback_to_workbuddy_cn(self):
        handler = DummyHandler()
        payload = {
            "model": "DeepSeek-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            return 503, json.dumps({"error": {"type": "no_available_account"}})

        mock_upstream = io.BytesIO(b"data: {\"choices\": [{\"delta\": {\"content\": \"Hello from WB CN\"}}]}\n\ndata: [DONE]\n\n")
        mock_account = MagicMock(uid="wb-test-account")

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder), \
             patch("wb_proxy.open_upstream", return_value=(mock_upstream, mock_account)) as mock_open_up:
            handler._route_to_qoder(payload)

        self.assertEqual(calls, ["DeepSeek-Flash", "Qwen3.8-Flash"])
        self.assertEqual(mock_open_up.call_count, 1)
        up_args, up_kwargs = mock_open_up.call_args
        self.assertEqual(up_kwargs.get("target_realm"), "cn")
        self.assertEqual(up_args[0].get("model"), "deepseek-v4.1-flash")
        self.assertEqual(handler.response_code, 200)

    def test_nonstream_qwen_exhausted_fallback_to_ds(self):
        handler = DummyHandler()
        payload = {
            "model": "Qwen3.8-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            if p.get("model") == "Qwen3.8-Flash":
                return 429, json.dumps({"error": {"message": "cooling down"}})
            elif p.get("model") == "DeepSeek-Flash":
                return 200, json.dumps({"choices": [{"message": {"content": "Hello from DS"}}]})
            return 500, "error"

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder):
            handler._route_to_qoder(payload)

        self.assertEqual(calls, ["Qwen3.8-Flash", "DeepSeek-Flash"])
        self.assertEqual(handler.response_code, 200)
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello from DS")

    def test_stream_ds_exhausted_fallback_to_qwen(self):
        handler = DummyHandler()
        payload = {
            "model": "DeepSeek-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            if p.get("model") == "DeepSeek-Flash":
                return 503, json.dumps({"error": {"type": "no_available_account"}})
            elif p.get("model") == "Qwen3.8-Flash":
                if on_delta:
                    on_delta({"choices": [{"delta": {"content": "Streaming from Qwen"}}]})
                return 200, ""
            return 500, "error"

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder):
            handler._route_to_qoder(payload)

        self.assertEqual(calls, ["DeepSeek-Flash", "Qwen3.8-Flash"])
        self.assertEqual(handler.response_code, 200)
        self.assertIn("text/event-stream", handler.response_headers.get("content-type", ""))
        raw_output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("Streaming from Qwen", raw_output)
        self.assertIn("[DONE]", raw_output)

    def test_stream_both_exhausted_fallback_to_workbuddy_cn(self):
        handler = DummyHandler()
        payload = {
            "model": "DeepSeek-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            return 503, json.dumps({"error": {"type": "no_available_account"}})

        wb_lines = [
            b"data: {\"choices\": [{\"delta\": {\"content\": \"Stream WB CN\"}}]}\n\n",
            b"data: [DONE]\n\n"
        ]
        mock_upstream = iter(wb_lines)
        mock_account = MagicMock(uid="wb-test-account")

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder), \
             patch("wb_proxy.open_upstream", return_value=(mock_upstream, mock_account)) as mock_open_up:
            handler._route_to_qoder(payload)

        self.assertEqual(calls, ["DeepSeek-Flash", "Qwen3.8-Flash"])
        self.assertEqual(mock_open_up.call_count, 1)
        up_args, up_kwargs = mock_open_up.call_args
        self.assertEqual(up_kwargs.get("target_realm"), "cn")
        self.assertEqual(up_args[0].get("model"), "deepseek-v4.1-flash")
        self.assertEqual(handler.response_code, 200)
        self.assertIn("text/event-stream", handler.response_headers.get("content-type", ""))
        raw_output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("Stream WB CN", raw_output)
    def test_stream_qwen_exhausted_fallback_to_ds(self):
        handler = DummyHandler()
        payload = {
            "model": "Qwen3.8-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            if p.get("model") == "Qwen3.8-Flash":
                return 503, json.dumps({"error": {"type": "no_available_account"}})
            elif p.get("model") == "DeepSeek-Flash":
                if on_delta:
                    on_delta({"choices": [{"delta": {"content": "Streaming from DS"}}]})
                return 200, ""
            return 500, "error"

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder):
            handler._route_to_qoder(payload)

        self.assertEqual(calls, ["Qwen3.8-Flash", "DeepSeek-Flash"])
        self.assertEqual(handler.response_code, 200)
        self.assertIn("text/event-stream", handler.response_headers.get("content-type", ""))
        raw_output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("Streaming from DS", raw_output)
        self.assertIn("[DONE]", raw_output)

    def test_stream_qwen_both_exhausted_fallback_to_workbuddy_cn(self):
        handler = DummyHandler()
        payload = {
            "model": "Qwen3.8-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            return 503, json.dumps({"error": {"type": "no_available_account"}})

        wb_lines = [
            b"data: {\"choices\": [{\"delta\": {\"content\": \"Stream WB CN from Qwen fallback\"}}]}\n\n",
            b"data: [DONE]\n\n"
        ]
        mock_upstream = iter(wb_lines)
        mock_account = MagicMock(uid="wb-test-account")

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder), \
             patch("wb_proxy.open_upstream", return_value=(mock_upstream, mock_account)) as mock_open_up:
            handler._route_to_qoder(payload)

        self.assertEqual(calls, ["Qwen3.8-Flash", "DeepSeek-Flash"])
        self.assertEqual(mock_open_up.call_count, 1)
        up_args, up_kwargs = mock_open_up.call_args
        self.assertEqual(up_kwargs.get("target_realm"), "cn")
        self.assertEqual(up_args[0].get("model"), "deepseek-v4.1-flash")
        self.assertEqual(handler.response_code, 200)
        self.assertIn("text/event-stream", handler.response_headers.get("content-type", ""))
        raw_output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("Stream WB CN from Qwen fallback", raw_output)
        self.assertIn("[DONE]", raw_output)

        self.assertIn("[DONE]", raw_output)

    def test_non_exhaustion_error_does_not_fallback(self):
        handler = DummyHandler()
        payload = {
            "model": "DeepSeek-Flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }

        calls = []
        def mock_call_qoder(p, on_delta=None):
            calls.append(p.get("model"))
            return 400, json.dumps({"error": {"message": "context length exceeded"}})

        with patch("wb_proxy.qoder_route.call_qoder", side_effect=mock_call_qoder):
            handler._route_to_qoder(payload)

        # Should only call DeepSeek-Flash, NOT fall back
        self.assertEqual(calls, ["DeepSeek-Flash"])
        self.assertEqual(handler.response_code, 400)


if __name__ == "__main__":
    unittest.main()
