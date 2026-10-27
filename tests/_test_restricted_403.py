"""上游风控限制 403（code 11140）：账号冷却、持久化、默认不换号连坐。

Run with: python tests/_test_restricted_403.py
No upstream credentials or outbound network are used.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as P

RESTRICT_BODY = json.dumps({
    "code": 11140,
    "msg": "request illegal",
    "displayMsg": {
        "en": "Your account has been restricted due to unusual activity",
        "zh": "账号存在异常行为，已被限制使用",
    },
}).encode("utf-8")

CONTENT_BODY = json.dumps({"code": 10001, "msg": "content rejected"}).encode("utf-8")


def http_error(code, body):
    return urllib.error.HTTPError("https://upstream/chat", code, "err", {},
                                  io.BytesIO(body))


class StubPool(object):
    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = wb_accounts.SessionAffinity()

    def count_ready(self, realm, model=None):
        return sum(1 for a in self.accounts
                   if a.realm == realm and a.ready(model=model))

    def apply_daily_token_limit(self, *args, **kwargs):
        return 0

    def apply_reserve_credits(self, *args, **kwargs):
        return 0

    def pick_for_session(self, realm=None, session_key=None, exclude=(),
                         model=None, page=None):
        for a in self.accounts:
            if a.uid in exclude:
                continue
            if realm and a.realm != realm:
                continue
            if a.ready(model=model):
                return a
        return None


class ScriptedUpstream(object):
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, proxy=None):
        self.calls.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_account(uid):
    return wb_accounts.Account({"uid": uid, "accessToken": "t-" + uid,
                                "realm": "intl"})


PAYLOAD = {"model": "deepseek-v4.1-flash",
           "messages": [{"role": "user", "content": "hi"}]}


class RestrictionDetectionTests(unittest.TestCase):
    def test_real_restriction_body_matches(self):
        self.assertTrue(P.restriction_is_account_level(RESTRICT_BODY.decode()))
        self.assertTrue(P.restriction_is_account_level(
            RESTRICT_BODY.decode()[:400]))
        self.assertTrue(P.restriction_is_account_level('{"code": "11140"}'))

    def test_content_and_junk_do_not_match(self):
        self.assertFalse(P.restriction_is_account_level(CONTENT_BODY.decode()))
        self.assertFalse(P.restriction_is_account_level(""))
        self.assertFalse(P.restriction_is_account_level(
            '{"msg":"x","requestId":"11140abcdef"}'))


class AccountRestrictionTests(unittest.TestCase):
    def test_restriction_is_persisted_and_blocks_the_pool(self):
        directory = tempfile.mkdtemp(prefix="wb-restrict-")
        account = make_account("p1")
        account.note_restricted(detail=RESTRICT_BODY.decode())
        account.save(directory)

        with open(account.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertGreater(data.get("restrictedUntil") or 0, time.time() + 7000)

        reloaded = wb_accounts.Account(data, account.path)
        self.assertGreater(reloaded.restricted_until, time.time() + 7000)
        self.assertFalse(reloaded.ready())
        self.assertGreater(reloaded.throttle_wait(), 7000)

    def test_clear_error_does_not_lift_it_but_success_does(self):
        account = make_account("p2")
        account.note_restricted(detail="{}")
        account.clear_error()
        self.assertGreater(account.restricted_until, time.time() + 7000)
        account.clear_restriction()
        self.assertEqual(account.restricted_until, 0.0)
        self.assertTrue(account.ready())

    def test_repeated_detection_does_not_extend_the_window(self):
        account = make_account("p3")
        account.note_restricted(detail="{}")
        first = account.restricted_until
        account.note_restricted(detail="{}")
        self.assertAlmostEqual(account.restricted_until, first, delta=0.01)


class RestrictionDispatchTests(unittest.TestCase):
    def setUp(self):
        self.old_pool = P.POOL
        self.old_rotate = P.RESTRICTED_403_ROTATE_MAX
        self.old_min = P.RESTRICTION_WAVE_MIN
        self.old_window = P.RESTRICTION_WAVE_WINDOW
        self.old_hold = P.RESTRICTION_HOLD_SECONDS
        self.old_urlopen = wb_accounts.urlopen
        P.RESTRICTED_403_ROTATE_MAX = 0
        P.RESTRICTION_WAVE_MIN = 2
        P.RESTRICTION_WAVE_WINDOW = 900.0
        P.RESTRICTION_HOLD_SECONDS = 1800.0
        P.reset_restriction_state()

    def tearDown(self):
        P.POOL = self.old_pool
        P.RESTRICTED_403_ROTATE_MAX = self.old_rotate
        P.RESTRICTION_WAVE_MIN = self.old_min
        P.RESTRICTION_WAVE_WINDOW = self.old_window
        P.RESTRICTION_HOLD_SECONDS = self.old_hold
        wb_accounts.urlopen = self.old_urlopen
        P.reset_restriction_state()

    def _run(self, accounts, script):
        P.POOL = StubPool(accounts)
        upstream = ScriptedUpstream(script)
        wb_accounts.urlopen = upstream
        try:
            result = P.open_upstream(dict(PAYLOAD), target_realm="intl")
            raised = None
        except Exception as exc:
            result = None
            raised = exc
        return result, raised, upstream

    def test_restricted_account_stops_the_request(self):
        restricted = make_account("r1")
        healthy = make_account("h1")
        resp, raised, upstream = self._run(
            [restricted, healthy],
            [http_error(403, RESTRICT_BODY), SimpleNamespace()])
        # 默认不换号：一条请求只打一个号就停手，健康账号不被牵连。
        self.assertEqual(len(upstream.calls), 1)
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)
        self.assertGreaterEqual(restricted.restricted_until - time.time(), 7000)
        self.assertEqual(restricted.last_error, "HTTP 403 (account restricted)")
        self.assertIn("11140", raised.detail)
        self.assertEqual(healthy.restricted_until, 0.0)

    def test_rotate_switch_restores_old_behaviour(self):
        P.RESTRICTED_403_ROTATE_MAX = 1
        restricted = make_account("r2")
        healthy = make_account("h2")
        resp, raised, upstream = self._run(
            [restricted, healthy],
            [http_error(403, RESTRICT_BODY), SimpleNamespace()])
        self.assertIsNone(raised)
        self.assertEqual(resp[1].uid, "h2")
        self.assertEqual(len(upstream.calls), 2)
        self.assertGreaterEqual(restricted.restricted_until - time.time(), 7000)

    def test_two_restrictions_pause_the_whole_realm(self):
        first = make_account("a1")
        second = make_account("b1")
        P.POOL = StubPool([first, second])
        upstream = ScriptedUpstream([http_error(403, RESTRICT_BODY),
                                     http_error(403, RESTRICT_BODY)])
        wb_accounts.urlopen = upstream
        with self.assertRaises(P.ContentRejected):
            P.open_upstream(dict(PAYLOAD), target_realm="intl")
        with self.assertRaises(P.ContentRejected):
            P.open_upstream(dict(PAYLOAD), target_realm="intl")
        self.assertGreater(P.restriction_hold_remaining("intl"), 1700)
        # 暂停期间一个上游请求都不发
        with self.assertRaises(P.RateLimited) as ctx:
            P.open_upstream(dict(PAYLOAD), target_realm="intl")
        self.assertEqual(len(upstream.calls), 2)
        self.assertIn("暂停", ctx.exception.message)

    def test_content_403_keeps_its_old_path(self):
        a = make_account("c1")
        b = make_account("c2")
        resp, raised, upstream = self._run(
            [a, b],
            [http_error(403, CONTENT_BODY), http_error(403, CONTENT_BODY)])
        self.assertEqual(a.restricted_until, 0.0)
        self.assertEqual(b.restricted_until, 0.0)
        self.assertIsNone(resp)
        self.assertIsInstance(raised, P.ContentRejected)


class SchedulerSkipTests(unittest.TestCase):
    def test_scheduler_skips_restricted_accounts(self):
        import wb_scheduler

        account = make_account("skip1")
        account.restricted_until = time.time() + 600
        account.can_daily_chat = lambda: (_ for _ in ()).throw(
            AssertionError("restricted account must not reach daily chat"))

        class Pool:
            accounts = [account]

        scheduler = wb_scheduler.Scheduler(Pool())
        scheduler._run_cycle("test")
        text = "\n".join(scheduler.logs)
        self.assertIn("跳过受限账号 [skip1]", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
