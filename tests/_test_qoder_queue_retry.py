"""排队限流（10605 / isQueued）必须原地等待重试，而不是直接失败。

Run with: python tests/_test_qoder_queue_retry.py
No upstream credentials or outbound network are used.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qoder"))

import qoder_client as client
import qoder_pool as pool
import qoder_route as route

QUEUE_DETAIL = (
    '{"code":"403","message":"{\\"code\\":\\"10605\\","message\\":'
    '{\\\\"isQueued\\\\":true,\\\\"modelKey\\\\":\\\\"qfmodel\\\\",'
    '\\\\"queueCount\\\\":0,\\\\"queueType\\\\":\\\\"p3\\\\",'
    '\\\\"retryAfterSeconds\\\\":30}}"'
)


class FakeIdentity:
    user_type = "personal_standard"


class FakeSession:
    identity = FakeIdentity()


class FakeSlot:
    id = "acct-fake"
    region = "cn"
    sess = FakeSession()


def fake_slot():
    return FakeSlot()


class QueueRetryTests(unittest.TestCase):
    def test_detects_queue_detail(self):
        self.assertTrue(pool.is_queued_detail(QUEUE_DETAIL))
        self.assertTrue(pool.is_queued_error(client.UpstreamError(403, QUEUE_DETAIL)))
        self.assertEqual(pool.retry_after_seconds(client.UpstreamError(403, QUEUE_DETAIL)), 30)
        # 普通 403 不是排队
        self.assertFalse(pool.is_queued_detail('{"code":"403","message":"forbidden"}'))

    def test_waits_and_retries_same_account(self):
        """排队一次 → 等 30s → 同一账号重试成功，不冷却也不换号。"""
        slot = fake_slot()
        calls = []

        def fake_stream(slot_arg, url, body, extra, on_delta):
            calls.append(body.get("request_id"))
            if len(calls) == 1:
                return False, client.UpstreamError(403, QUEUE_DETAIL)
            on_delta(client.Delta(role="assistant", content="pong"))
            return True, None

        with mock.patch.object(route, "stream_with_slot", fake_stream), \
             mock.patch.object(route, "pick_slot_for_model",
                               return_value=(slot, True)), \
             mock.patch.object(route, "get_pool", return_value=mock.Mock(
                   size=lambda: 1, note_success=mock.Mock(), cool=mock.Mock())), \
             mock.patch.object(route, "build_qoder_request_body",
                               return_value={"model_config": {}}), \
             mock.patch.object(route, "_get_template_base", return_value={}), \
             mock.patch.object(route.time, "sleep") as slept:
            status, body = route.call_qoder(
                {"model": "Qwen3.8-Flash", "messages": [], "stream": True},
                on_delta=lambda chunk: None)

        self.assertEqual(status, 200, body)
        self.assertEqual(len(calls), 2, "排队后必须在同一账号上重试一次")
        self.assertEqual(slept.call_args[0][0], 30, "应按 retryAfterSeconds 等待")

    def test_waits_at_most_queue_max_waits(self):
        """一直排队也只原地等 QUEUE_MAX_WAITS 次，然后按原路径换号/失败。"""
        slot = fake_slot()
        attempts = []

        def fake_stream(slot_arg, url, body, extra, on_delta):
            attempts.append(1)
            return False, client.UpstreamError(403, QUEUE_DETAIL)

        pool_mock = mock.Mock(size=lambda: 1, note_success=mock.Mock(), cool=mock.Mock())
        with mock.patch.object(route, "stream_with_slot", fake_stream), \
             mock.patch.object(route, "pick_slot_for_model",
                               side_effect=[(slot, True), (None, False)]), \
             mock.patch.object(route, "get_pool", return_value=pool_mock), \
             mock.patch.object(route, "build_qoder_request_body",
                               return_value={"model_config": {}}), \
             mock.patch.object(route, "_get_template_base", return_value={}), \
             mock.patch.object(route.time, "sleep") as slept:
            status, body = route.call_qoder(
                {"model": "Qwen3.8-Flash", "messages": [], "stream": True},
                on_delta=lambda chunk: None)

        self.assertEqual(status, 403, body)
        self.assertEqual(slept.call_count, route.QUEUE_MAX_WAITS)
        # 等不到后才交给原路径：冷却 30s（retryAfterSeconds）并换号
        pool_mock.cool.assert_called_once_with(slot.id, 30, mock.ANY)
        self.assertEqual(len(attempts), route.QUEUE_MAX_WAITS + 1)

    def test_no_wait_when_content_already_emitted(self):
        """已开始出内容后遇到排队错误，直接按原逻辑失败，不吞输出。"""
        slot = fake_slot()
        attempts = []

        def fake_stream(slot_arg, url, body, extra, on_delta):
            attempts.append(1)
            on_delta(client.Delta(content="partial"))
            return True, client.UpstreamError(403, QUEUE_DETAIL)

        with mock.patch.object(route, "stream_with_slot", fake_stream), \
             mock.patch.object(route, "pick_slot_for_model", return_value=(slot, True)), \
             mock.patch.object(route, "get_pool", return_value=mock.Mock(
                   size=lambda: 1, note_success=mock.Mock(), cool=mock.Mock())), \
             mock.patch.object(route, "build_qoder_request_body",
                               return_value={"model_config": {}}), \
             mock.patch.object(route, "_get_template_base", return_value={}), \
             mock.patch.object(route.time, "sleep") as slept:
            status, body = route.call_qoder(
                {"model": "Qwen3.8-Flash", "messages": [], "stream": True},
                on_delta=lambda chunk: None)

        self.assertEqual(status, 403, body)
        self.assertEqual(len(attempts), 1)
        slept.assert_not_called()

    def test_queue_wait_does_not_block_content_flow(self):
        """排队重试成功后内容照常透传（非流式聚合路径）。"""
        slot = fake_slot()
        calls = []

        def fake_stream(slot_arg, url, body, extra, on_delta):
            calls.append(1)
            if len(calls) == 1:
                return False, client.UpstreamError(403, QUEUE_DETAIL)
            on_delta(client.Delta(content="hi "))
            on_delta(client.Delta(content="there"))
            on_delta(client.Delta(input_tokens=3, output_tokens=2))
            return True, None

        with mock.patch.object(route, "stream_with_slot", fake_stream), \
             mock.patch.object(route, "pick_slot_for_model", return_value=(slot, True)), \
             mock.patch.object(route, "get_pool", return_value=mock.Mock(
                   size=lambda: 1, note_success=mock.Mock(), cool=mock.Mock())), \
             mock.patch.object(route, "build_qoder_request_body",
                               return_value={"model_config": {}}), \
             mock.patch.object(route, "_get_template_base", return_value={}), \
             mock.patch.object(route.time, "sleep"):
            status, body = route.call_qoder(
                {"model": "Qwen3.8-Flash", "messages": [], "stream": False})

        self.assertEqual(status, 200, body)
        self.assertEqual(len(calls), 2)

    def test_banned_account_gets_a_day_not_a_queue_wait(self):
        """账号封禁/试用限制按天冷却，不能当成 30 秒队列原地重试。"""
        banned = client.UpstreamError(403, json.dumps({
            "code": 403,
            "message": "You have too many free trial accounts, so usage for "
                       "this account is temporarily restricted.",
        }))
        self.assertTrue(pool.is_account_banned(banned))
        self.assertEqual(pool.cooldown_for(banned), pool.COOLDOWN_BANNED)
        self.assertFalse(pool.is_queued_error(banned))
        # 普通 403 仍是 30 分钟，内容审核不是封禁
        self.assertFalse(pool.is_account_banned(
            client.UpstreamError(403, '{"code":"403","message":"forbidden"}')))
        self.assertEqual(pool.cooldown_for(
            client.UpstreamError(403, '{"code":"403","message":"forbidden"}')),
            pool.COOLDOWN_QUOTA)

    def test_code_105_and_115_are_per_account_states(self):
        """105 = 登录过期（24h），115 = agent 额度（到重置时刻）。"""
        expired = client.UpstreamError(0, "upstream error code=105: Login expired")
        self.assertTrue(pool.is_auth_expired_error(expired))
        self.assertTrue(pool.is_token_expired(expired))
        self.assertTrue(pool.should_failover(expired))
        self.assertEqual(pool.cooldown_for(expired), pool.AUTH_EXPIRED_COOLDOWN)

        reset_ms = int((time.time() + 1800) * 1000)
        limited = client.UpstreamError(0, json.dumps({
            "code": "115",
            "message": json.dumps({"agentLimitResetTime": reset_ms}),
        }))
        self.assertTrue(pool.is_agent_limit_error(limited))
        self.assertTrue(pool.should_failover(limited))
        self.assertAlmostEqual(pool.cooldown_for(limited), 1800, delta=5)
        self.assertEqual(pool.agent_limit_reset_at(limited), reset_ms / 1000.0)

        # 115 没有重置时间时用 10 分钟兜底
        no_reset = client.UpstreamError(0, '{"code":"115","message":"limit"}')
        self.assertEqual(pool.cooldown_for(no_reset),
                         pool.AGENT_LIMIT_DEFAULT_COOLDOWN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
