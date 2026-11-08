"""Qoder provider_error 必须解出内层原因、保留原文，并触发换号。

上游把后端供应商的真实错误套在 provider_error 的 details 里（可能带 SSE 的
`data: ` 前缀、也可能多层 JSON 字符串）。此前这段原文被 UpstreamError 截到
200 字符，且内层的 invalid_parameter_error 命中 CLIENT_FAULT_MARKERS，导致
「换号就能成功」的请求被当成客户端参数错误直接返回 400。

Run with: python tests/_test_qoder_provider_error.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qoder"))

import qoder_client as client
import qoder_pool as pool
import qoder_route as route

INNER = {
    "error": {
        "code": "invalid_parameter_error",
        "message": "Invalid parameter max_tokens. Provider maximum value is 16384 "
                   "but requested 32768.",
    }
}
PROVIDER_DETAIL = json.dumps({
    "code": "provider_error",
    "message": "Error in upstream response",
    "request_id": "00fd7ce7-4c55-6102-0431-303789488a5a",
    "type": "provider_error",
    "details": "data: " + json.dumps(INNER, ensure_ascii=False),
}, ensure_ascii=False)

# 真正的客户端参数错误：顶层就是 invalid_parameter_error，没有 provider 包装
CLIENT_FAULT = json.dumps({"error": {"code": "invalid_parameter_error",
                                     "message": "messages is required"}})


class FakeIdentity:
    user_type = "personal_standard"


class FakeSession:
    identity = FakeIdentity()


class FakeSlot:
    def __init__(self, sid):
        self.id = sid
        self.region = "cn"
        self.sess = FakeSession()


class ProviderErrorTests(unittest.TestCase):
    def test_unwraps_nested_provider_details(self):
        self.assertTrue(client.is_provider_error(PROVIDER_DETAIL))
        code, msg = client.provider_error_inner(PROVIDER_DETAIL)
        self.assertEqual(code, "invalid_parameter_error")
        self.assertIn("Provider maximum value is 16384", msg)

        doubled = json.dumps({
            "code": "provider_error", "type": "provider_error",
            "details": json.dumps({"details": json.dumps(INNER)}),
        })
        self.assertIn("16384", client.provider_error_inner(doubled)[1])

        self.assertIsNone(client.provider_error_inner(CLIENT_FAULT))
        self.assertFalse(client.is_provider_error(CLIENT_FAULT))

    def test_error_detail_is_not_truncated(self):
        """200 字符的截断会切掉 details 里的唯一线索。"""
        err = client.UpstreamError(400, PROVIDER_DETAIL)
        self.assertEqual(err.detail, PROVIDER_DETAIL)
        self.assertIn(PROVIDER_DETAIL, str(err))
        self.assertGreater(len(str(err)), 250)

    def test_classification_and_status(self):
        self.assertTrue(pool.is_transient_upstream(400, PROVIDER_DETAIL))
        self.assertTrue(pool.should_failover(client.UpstreamError(400, PROVIDER_DETAIL)))
        # 顶层客户端参数错误仍属确定性拒绝
        self.assertFalse(pool.is_transient_upstream(400, CLIENT_FAULT))
        self.assertFalse(pool.should_failover(client.UpstreamError(400, CLIENT_FAULT)))

        msg, etype = pool.friendly_upstream_error(400, PROVIDER_DETAIL)
        self.assertEqual(etype, "upstream_transient_error")
        self.assertIn("invalid_parameter_error", msg)
        self.assertIn("Provider maximum value is 16384", msg)
        self.assertIn(PROVIDER_DETAIL, msg, "上游原文必须完整透传")
        self.assertEqual(pool.error_status(client.UpstreamError(400, PROVIDER_DETAIL),
                                           etype), 502)

    def test_content_policy_classification_survives(self):
        """流内中文审核文案没有英文标记，必须靠 err_type 保留分类。"""
        detail = "upstream error code=115: 您的输入内容可能包含敏感信息"
        err = client.UpstreamError(0, detail, err_type="content_policy_rejected")
        msg, etype = route._friendly_error(err)
        self.assertEqual(etype, "content_policy_rejected")
        self.assertIn("内容安全审核未通过", msg)
        self.assertEqual(pool.error_status(err, etype), 400)

    def test_provider_error_fails_over_across_accounts(self):
        slots = [FakeSlot("acct-1"), FakeSlot("acct-2"), FakeSlot("acct-3")]
        seen = []

        def pick(p, model_key, exclude=None, realm=""):
            for s in slots:
                if s.id not in (exclude or set()):
                    return s, True
            return None, False

        def fake_stream(slot, url, body, extra, on_delta):
            seen.append(body["request_id"])
            return False, client.UpstreamError(400, PROVIDER_DETAIL)

        recorded = []
        with mock.patch.object(route, "stream_with_slot", fake_stream), \
             mock.patch.object(route, "pick_slot_for_model", pick), \
             mock.patch.object(route, "get_pool", return_value=mock.Mock(
                 size=lambda: len(slots), note_success=mock.Mock(), cool=mock.Mock())), \
             mock.patch.object(route, "build_qoder_request_body",
                               return_value={"model_config": {}}), \
             mock.patch.object(route, "_get_template_base", return_value={}), \
             mock.patch.object(route, "record_qoder_usage",
                               lambda *a, **k: recorded.append(a)):
            status, body = route.call_qoder({"model": "QoDuck", "stream": True,
                                             "messages": [{"role": "user",
                                                           "content": "hi"}]})

        err = json.loads(body)["error"]
        self.assertEqual(len(seen), 3, "三个账号都应被试一次")
        self.assertEqual(len(set(seen)), 3, "每次尝试要换新的 request_id")
        self.assertEqual(status, 502)
        self.assertEqual(err["type"], "upstream_transient_error")
        self.assertIn("Provider maximum value is 16384", err["message"])
        self.assertEqual(recorded[-1][1], "acct-3", "失败记录要写明真实尝试的槽位")

    def test_client_fault_is_not_retried(self):
        slots = [FakeSlot("acct-1"), FakeSlot("acct-2")]
        seen = []

        def pick(p, model_key, exclude=None, realm=""):
            for s in slots:
                if s.id not in (exclude or set()):
                    return s, True
            return None, False

        def fake_stream(slot, url, body, extra, on_delta):
            seen.append(slot.id)
            return False, client.UpstreamError(400, CLIENT_FAULT)

        with mock.patch.object(route, "stream_with_slot", fake_stream), \
             mock.patch.object(route, "pick_slot_for_model", pick), \
             mock.patch.object(route, "get_pool", return_value=mock.Mock(
                 size=lambda: len(slots), note_success=mock.Mock(), cool=mock.Mock())), \
             mock.patch.object(route, "build_qoder_request_body",
                               return_value={"model_config": {}}), \
             mock.patch.object(route, "_get_template_base", return_value={}), \
             mock.patch.object(route, "record_qoder_usage", lambda *a, **k: None):
            status, body = route.call_qoder({"model": "QoDuck", "stream": True,
                                             "messages": [{"role": "user",
                                                           "content": "hi"}]})

        err = json.loads(body)["error"]
        self.assertEqual(seen, ["acct-1"], "客户端参数错误不应换号重试")
        self.assertEqual(status, 400)
        self.assertIn("messages is required", err["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
