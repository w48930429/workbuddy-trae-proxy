"""CodeArts 路由层的离线测试。

重点是那些「不加前缀就会路由错」的同名模型冲突，以及上游模型名与本地
展示名之间的双向映射 —— 这两件事一旦错，请求会静默打到错误的上游。
"""
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "codearts"))

import codearts_client as client
import codearts_route as route


class PrefixTests(unittest.TestCase):
    def test_detects_codearts_models(self):
        self.assertTrue(route.is_codearts_model("codearts-kimi-k3"))
        self.assertFalse(route.is_codearts_model("kimi-k3"))
        self.assertFalse(route.is_codearts_model(""))
        self.assertFalse(route.is_codearts_model(None))

    def test_strips_prefix_once(self):
        self.assertEqual(route.upstream_model("codearts-kimi-k3"), "kimi-k3")
        self.assertEqual(route.upstream_model("kimi-k3"), "kimi-k3")

    def test_adds_prefix_idempotently(self):
        self.assertEqual(route.local_model("kimi-k3"), "codearts-kimi-k3")
        self.assertEqual(route.local_model("codearts-kimi-k3"), "codearts-kimi-k3")

    def test_no_collision_with_the_fallback_route_model(self):
        # 关键不变量：deepseek-v4.1-flash 是额度不足时的降级目标。
        # 它在 CodeArts 也有同名模型，因此必须只在带前缀时才算 CodeArts 的。
        self.assertFalse(route.is_codearts_model("deepseek-v4.1-flash"))
        self.assertTrue(route.is_codearts_model("codearts-deepseek-v4.1-flash"))


class BuildRequestTests(unittest.TestCase):
    def test_strips_prefix_before_sending(self):
        # 前缀是本仓库内部的；发往上游会导致 404 / 模型不存在。
        body = route.build_request({"model": "codearts-kimi-k3",
                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(body["model"], "kimi-k3")

    def test_removes_internal_fields(self):
        body = route.build_request({"model": "codearts-kimi-k3",
                                    "messages": [],
                                    "stream_options": {"include_usage": True},
                                    "_namespace_map": {"a": "b"}})
        self.assertNotIn("stream_options", body)
        self.assertNotIn("_namespace_map", body)

    def test_keeps_sampling_parameters(self):
        body = route.build_request({"model": "codearts-kimi-k3",
                                    "messages": [], "temperature": 0.3,
                                    "max_tokens": 512})
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(body["max_tokens"], 512)

    def test_stream_only_model_is_forced_to_stream(self):
        # glm-5.3 非流式会被上游拒（400 空 body），必须强制流式再聚合。
        body = route.build_request({"model": "codearts-glm-5.3",
                                    "messages": [], "stream": False})
        self.assertTrue(body["stream"])

    def test_original_payload_is_not_mutated(self):
        original = {"model": "codearts-glm-5.3", "messages": [], "stream": False}
        route.build_request(original)
        self.assertEqual(original["stream"], False, "caller's dict must not change")
        self.assertEqual(original["model"], "codearts-glm-5.3")

    def test_non_stream_model_keeps_stream_flag(self):
        body = route.build_request({"model": "codearts-kimi-k3",
                                    "messages": [], "stream": False})
        self.assertFalse(body.get("stream"))


class CatalogTests(unittest.TestCase):
    def test_builtin_catalog_is_prefixed(self):
        entries = route.catalog_models()
        self.assertTrue(entries)
        for entry in entries:
            self.assertTrue(entry["id"].startswith("codearts-"))
            self.assertEqual(entry["object"], "model")
            self.assertEqual(entry["owned_by"], "codearts")

    def test_catalog_includes_models_absent_from_workbuddy(self):
        ids = {e["id"] for e in route.catalog_models()}
        for expected in ("codearts-openpangu-2.0-pro", "codearts-Qwen3-VL-235B",
                         "codearts-kimi-k2.6-vl", "codearts-GLM-5.2"):
            self.assertIn(expected, ids)


class ErrorPayloadTests(unittest.TestCase):
    def test_credential_error_is_labelled(self):
        exc = client.CodeArtsCredentialError(502, "reopen the IDE", "STS5.1806")
        status, body = route.error_payload(exc)
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["type"], "credentials_error")
        self.assertEqual(body["error"]["code"], "STS5.1806")

    def test_parameter_error_keeps_status(self):
        exc = client.CodeArtsError(400, "temperature out of range",
                                   "InferHub.001001005.400")
        status, body = route.error_payload(exc)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "upstream_error")


class AggregateStreamTests(unittest.TestCase):
    def _feed(self, frames):
        class Response(object):
            def __init__(self):
                self.closed = False

            def __iter__(self_inner):
                for frame in frames:
                    yield ("data: " + json.dumps(frame) + "\n\n").encode()

            def close(self_inner):
                self_inner.closed = True

        original_stream, original_session = route.client.stream_chat, route.load_session
        route.client.stream_chat = lambda *a, **k: Response()
        route.load_session = lambda *a, **k: object()
        try:
            return route.complete({"model": "codearts-glm-5.3", "messages": []})
        finally:
            route.client.stream_chat, route.load_session = original_stream, original_session

    def test_aggregates_content_and_usage(self):
        result = self._feed([
            {"choices": [{"delta": {"content": "he"}}]},
            {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        ])
        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"]["total_tokens"], 5)

    def test_keeps_reasoning_content_separate(self):
        result = self._feed([
            {"choices": [{"delta": {"reasoning_content": "think"}}]},
            {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
        ])
        message = result["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "think")
        self.assertEqual(message["content"], "answer")

    def test_error_frame_raises(self):
        with self.assertRaises(client.CodeArtsError):
            self._feed([{"error_code": "InferHub.001001005.400",
                         "error_msg": "bad param"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
