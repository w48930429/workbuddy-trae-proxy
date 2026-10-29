"""上游怪癖的行为测试（离线，不联网）。

上游协议有三条最容易踩的行为，这里逐条钉死，避免「看起来更干净」的改写
把兼容性改掉：

  1. 业务错误常用 HTTP 200 + ``error_code`` 表达 —— 真实状态码从 ``error_code``
     取，取不到兜底 400，**绝不**因为 HTTP 200 就当成成功。
  2. ``glm-5.3`` 只支持流式（``stream=false`` 回 400 空 body）。
  3. ``x-ot-session-id`` 必须按凭据派生并复用 —— 上游按它限并发，随机会
     撞 ``TM.00001041``。
"""
import io
import json
import os
import sys
import unittest
import urllib.error

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "codearts"))

import codearts_client as client
import codearts_session as sessions


class FakeResponse(object):
    def __init__(self, payload, status=200):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status

    def read(self, *_args):
        return self._body


def session():
    return sessions.CodeArtsSession(
        access_key="HSTAADB6JF0366BJ1QAQ",
        secret_key="secret",
        security_token="sts",
        domain_id="08d862d7bd0010690f3cc018f928b1c0",
        account_label="hw52150650")


class ErrorClassificationTests(unittest.TestCase):
    def test_http_200_with_error_code_is_a_failure(self):
        # 上游把业务失败包在 HTTP 200 里；当成成功会把错误当正文返回给客户端。
        with self.assertRaises(client.CodeArtsError) as caught:
            client.parse_response(FakeResponse(
                {"error_code": "InferHub.002002009.404", "error_msg": "not found"}))
        self.assertEqual(caught.exception.status, 404)

    def test_business_code_outside_http_range_falls_back_to_400(self):
        with self.assertRaises(client.CodeArtsError) as caught:
            client.parse_response(FakeResponse(
                {"error_code": "4005", "error_msg": "unsupported model"}))
        self.assertEqual(caught.exception.status, 400)

    def test_plain_status_code_is_used_as_is(self):
        with self.assertRaises(client.CodeArtsError) as caught:
            client.parse_response(FakeResponse({"error_code": "422"}, status=422))
        self.assertEqual(caught.exception.status, 422)

    def test_credential_failure_is_distinguishable(self):
        # 凭据失效是唯一需要人工介入的；它即使以 400 出现也必须单独归类，
        # 否则会被当成参数错误透传，客户端永远不知道该重新登录。
        # 状态码统一成 401：客户端据此重新认证，而不是重试同一份请求体。
        with self.assertRaises(client.CodeArtsCredentialError) as caught:
            client.parse_response(FakeResponse(
                {"error_code": "STS5.1806", "error_msg": "refresh token used"},
                status=400))
        self.assertEqual(caught.exception.status, 401)

    def test_parameter_error_is_not_a_credential_failure(self):
        with self.assertRaises(client.CodeArtsError) as caught:
            client.parse_response(FakeResponse(
                {"error_code": "InferHub.001001005.400",
                 "details": [{"error_code": "RANGE_VALIDATOR",
                              "error_msg": "temperature should be between 0 and 2"}]},
                status=400))
        self.assertNotIsInstance(caught.exception, client.CodeArtsCredentialError)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("temperature", caught.exception.message)

    def test_success_returns_parsed_body(self):
        body = client.parse_response(FakeResponse(
            {"choices": [{"message": {"content": "hi"}}]}))
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")

    def test_empty_body_is_not_an_error(self):
        # glm-5.3 非流式回 400 空 body；成功路径也可能给空 204 之类。
        self.assertIsNone(client.parse_response(FakeResponse(b"", status=200)))


class StatusFromCodeTests(unittest.TestCase):
    def test_compound_code_takes_last_segment(self):
        self.assertEqual(client.status_from_code("InferHub.002002009.404"), 404)

    def test_plain_http_status(self):
        self.assertEqual(client.status_from_code("400"), 400)
        self.assertEqual(client.status_from_code("503"), 503)

    def test_non_http_numbers_are_not_statuses(self):
        # 4005 是业务码，不是状态码；交给调用方兜底 400。
        self.assertIsNone(client.status_from_code("4005"))
        self.assertIsNone(client.status_from_code("999"))

    def test_garbage_is_none(self):
        for raw in ("", None, "abc", "TM.00001041"):
            with self.subTest(raw=raw):
                self.assertIsNone(client.status_from_code(raw))


class SessionIdTests(unittest.TestCase):
    def test_session_id_is_stable_for_same_credentials(self):
        # 上游按它统计并发（每账号 3 个），漂移会让每个请求都算新会话。
        first = client.upstream_session_id("domain", "AK")
        second = client.upstream_session_id("domain", "AK")
        self.assertEqual(first, second)

    def test_session_id_changes_with_credentials(self):
        self.assertNotEqual(client.upstream_session_id("d1", "AK"),
                            client.upstream_session_id("d2", "AK"))
        self.assertNotEqual(client.upstream_session_id("d", "AK1"),
                            client.upstream_session_id("d", "AK2"))

    def test_session_id_shape(self):
        value = client.upstream_session_id("d", "AK")
        self.assertTrue(value.startswith("ses_"))
        self.assertEqual(len(value), 4 + 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in value[4:]))


class HeaderTests(unittest.TestCase):
    def test_model_appears_in_all_three_places(self):
        # 上游要求 body.model 与 model-id / model-name 完全一致，
        # 否则 InferHub.001001011.406。
        headers = client.chat_headers("kimi-k3", session())
        self.assertEqual(headers["model-id"], "kimi-k3")
        self.assertEqual(headers["model-name"], "kimi-k3")

    def test_no_maas_type(self):
        # benefit 通道 2026-10 下线；继续发送会被路由到不存在的通道。
        self.assertNotIn("maas_type", client.chat_headers("glm-5.3", session()))

    def test_session_header_comes_from_credentials(self):
        headers = client.chat_headers("kimi-k3", session())
        self.assertEqual(headers["x-ot-session-id"],
                         client.upstream_session_id(session().domain_id,
                                                    session().access_key))

    def test_trace_ids_are_random_per_call(self):
        a = client.chat_headers("m", session())
        b = client.chat_headers("m", session())
        self.assertNotEqual(a["x-ot-trace-id"], b["x-ot-trace-id"])
        self.assertNotEqual(a["user-msg-id"], b["user-msg-id"])


class StreamOnlyTests(unittest.TestCase):
    def test_glm53_is_marked_stream_only(self):
        self.assertIn("glm-5.3", client.STREAM_ONLY_MODELS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
