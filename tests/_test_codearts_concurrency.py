"""CodeArts 并发闸门的端到端测试（真实 HTTP 服务 + 假上游，不联网）。

回归的是线上事故的形状：华为云单账号只允许 3 个并发会话（按 x-ot-session-id），
超了回 400 + TM.00001041。旧实现会
  1. 把并发满当「账号故障」拉黑 15s；
  2. 拉黑后回退到 load_session() 单会话路径，用**同一套凭据**继续打上游、还不计数；
  3. 最终把 400 抛给客户端，客户端（omp）以为模型坏了，立刻换模型 ——
     用户看到的就是「用着用着自动跳到别的模型」。

现在要求：第 4 个并发请求**排队等空位**，等不到才回 429（可重试），永不回 400，
且上游同时最多只有 3 个请求在飞。
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import wb_proxy
from codearts.codearts_accounts import CodeArtsAccount, CodeArtsAccountPool


class _FakePool(CodeArtsAccountPool):
    def __init__(self):
        self.accounts = [CodeArtsAccount({
            "id": "acct_only",
            "label": "Only",
            "accessKey": "AK_ONLY",
            "secretKey": "SK_ONLY",
            "enabled": True,
        })]
        self._lock = threading.RLock()
        self._cursor = 0
        self.accounts_dir = "."
        self.file_path = "dummy_pool.json"

    def save(self):
        pass


class _UpstreamProbe:
    """假上游：记录同时在飞的数量，按需阻塞。"""

    def __init__(self, hold_seconds):
        self.hold = hold_seconds
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.calls = 0

    def stream(self, payload, session=None, proxy=""):
        with self.lock:
            self.calls += 1
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(self.hold)

        def gen():
            try:
                yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
                yield b"data: [DONE]\n\n"
            finally:
                with self.lock:
                    self.inflight -= 1

        return gen()


class ConcurrencyGateTests(unittest.TestCase):
    def _serve(self, probe, queue_wait):
        pool = _FakePool()
        patches = [
            patch.object(wb_proxy, "get_codearts_pool", return_value=pool),
            patch.object(wb_proxy.codearts_route, "stream", side_effect=probe.stream),
            patch.object(wb_proxy.codearts_route, "load_session"),
            patch.object(wb_proxy.Handler, "_authorized", return_value=True),
            patch.object(wb_proxy, "CODEARTS_QUEUE_WAIT_SECONDS", queue_wait),
            patch.object(wb_proxy, "cors_origin_allowed", return_value=False),
        ]
        self._patches = patches
        started = [p.start() for p in patches]
        self.load_session = started[2]
        server = ThreadingHTTPServer(("127.0.0.1", 0), wb_proxy.Handler)
        server.server_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, pool

    def tearDown(self):
        for p in getattr(self, "_patches", []):
            p.stop()
        self._patches = []

    def _post(self, port, results, index):
        body = json.dumps({"model": "codearts-kimi-k3", "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % port, data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer x"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                results[index] = (resp.status, resp.read().decode("utf-8", "replace"),
                                  dict(resp.headers))
        except urllib.error.HTTPError as exc:
            results[index] = (exc.code, exc.read().decode("utf-8", "replace"),
                              dict(exc.headers))

    def _fire(self, port, count):
        results = [None] * count
        threads = [threading.Thread(target=self._post, args=(port, results, i))
                   for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return results

    def test_fourth_concurrent_request_queues_then_429(self):
        """并发上限 3：前 3 个跑，第 4 个等不到空位 → 429，且上游峰值不超过 3。"""
        probe = _UpstreamProbe(hold_seconds=1.0)
        server, pool = self._serve(probe, queue_wait=0.2)
        try:
            results = self._fire(server.server_port, 4)
        finally:
            server.shutdown()
            server.server_close()

        statuses = sorted(r[0] for r in results if r)
        self.assertEqual(statuses, [200, 200, 200, 429], results)
        self.assertLessEqual(probe.peak, 3, "上游并发峰值必须 <= 3")
        self.assertEqual(pool.accounts[0].in_flight, 0, "结束后槽位必须全部归还")
        self.load_session.assert_not_called()

    def test_queued_request_succeeds_when_slot_frees(self):
        probe = _UpstreamProbe(hold_seconds=0.3)
        server, _pool = self._serve(probe, queue_wait=10)
        try:
            results = self._fire(server.server_port, 4)
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual([r[0] for r in results], [200, 200, 200, 200], results)
        self.assertLessEqual(probe.peak, 3)
        self.assertEqual(probe.calls, 4, "排队后每个请求只打一次上游，不能重复重试")

    def test_zero_wait_fast_fails_with_retry_after(self):
        """默认 0 等待：槽位满立刻 429 + Retry-After，不再原地挂 20s。"""
        probe = _UpstreamProbe(hold_seconds=1.0)
        server, _pool = self._serve(probe, queue_wait=0)
        try:
            results = self._fire(server.server_port, 4)
        finally:
            server.shutdown()
            server.server_close()

        statuses = sorted(r[0] for r in results if r)
        self.assertEqual(statuses, [200, 200, 200, 429], results)
        rejected = [r for r in results if r and r[0] == 429]
        self.assertEqual(len(rejected), 1, results)
        self.assertTrue(rejected[0][2].get("Retry-After"),
                        "429 必须带 Retry-After，客户端才知道何时重试: %r"
                        % (rejected[0][2],))
        self.assertLessEqual(probe.peak, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
