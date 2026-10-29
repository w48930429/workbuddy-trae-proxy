"""Quota exhaustion reroutes to workbuddy-cn deepseek-v4.1-flash (1017), not step.

The operator's rule: every other account/model that runs out of credit must be
re-routed to `deepseek-v4.1-flash` on the cn exit (the DeepSeek-V4.1-Flash-1017
release, x0.00 free tier). It must never land on `step` / `step-5-preview`, and
must not be confused with the cross-channel Trae/Qoder failover that was
deliberately turned off earlier.

Pinned here:
  - credit_exhausted() recognises the Chinese wording too, not only code 14018
    (the domestic exit phrases the same state as 「额度不足」).
  - exhausted_route_payload() rewrites only the model, keeps everything else,
    and returns None when the request already targets the route model.
  - open_upstream() actually sends the rewritten body upstream when the first
    attempt answers 14018.
No network: the upstream call is stubbed, exactly like
tests/_test_model_cooldowns.py.
"""
import io
import os
import sys
import tempfile
import time
import types
import unittest
import urllib.error

_startup_dir = tempfile.TemporaryDirectory(prefix="exhausted-route-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_proxy as proxy


class ExhaustedRoutePayloadTests(unittest.TestCase):
    """What the rewrite does, asserted through open_upstream() itself.

    The rewrite lives inside open_upstream() (it needs the account, the session
    key and the real upstream body), so these cases drive that function with a
    stubbed urlopen and read the models off the requests it actually sent.
    """

    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn",
                                 "accessToken": "token"})

    def _run(self, script, payload):
        account = self.account()
        sent = []
        test = self
        test.sent_models = []
        test.sent_bodies = []

        class Pool(object):
            accounts = [account]
            affinity = types.SimpleNamespace(unbind=lambda _key: None)

            def count_ready(self, realm, model=None):
                return 1

            def apply_daily_token_limit(self, value=None, usage=None):
                return 0

            def apply_reserve_credits(self, value=None, usage=None):
                return 0

            def pick_for_session(self, realm, session_key=None, exclude=(), model=None):
                return account

        class Upstream(object):
            def __init__(self, inner):
                self.inner = inner

            def __call__(self, req, timeout=None, proxy=None):
                body = json_loads_safe(req.data)
                sent.append(body)
                test.sent_bodies.append(body)
                test.sent_models.append(body.get("model"))
                item = self.inner.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        proxy.POOL = Pool()
        accounts.urlopen = Upstream(script)
        try:
            return proxy.open_upstream(payload, target_realm="cn"), sent
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen

    def test_credit_detection_covers_chinese_and_code(self):
        self.assertTrue(proxy.credit_exhausted('{"code":14018}'))
        self.assertTrue(proxy.credit_exhausted("insufficient credits"))
        self.assertTrue(proxy.credit_exhausted("您的额度不足，请充值"))
        self.assertTrue(proxy.credit_exhausted("积分不足"))
        # An ensure_ascii body is the same state: missing it means the reroute
        # never fires against a domestic exit that escapes its CJK.
        self.assertTrue(proxy.credit_exhausted(
            json_dumps({"msg": "额度不足"}, ensure_ascii=True)))
        # Raw bytes: urllib hands the body back undecoded.
        self.assertTrue(proxy.credit_exhausted(b'{"msg": "\\u989d\\u5ea6\\u4e0d\\u8db3"}'))
        self.assertFalse(proxy.credit_exhausted("prompt is too long"))
        self.assertFalse(proxy.credit_exhausted(""))

    def test_rewrite_only_happens_on_credit_exhaustion(self):
        detail = json_dumps({"code": 11102, "msg": "prompt is too long"})
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 400, "bad request", {},
            io.BytesIO(detail.encode("utf-8")))
        ok = types.SimpleNamespace(uid="synthetic-cn")
        with self.assertRaises(urllib.error.HTTPError):
            self._run([error, ok], {"model": "glm-5.3",
                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(self.sent_models, ["glm-5.3"])

    def test_rewrite_targets_the_1017_route_and_keeps_everything_else(self):
        detail = json_dumps({"code": 14018, "msg": "insufficient credits"})
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 400, "bad request", {},
            io.BytesIO(detail.encode("utf-8")))
        ok = types.SimpleNamespace(uid="synthetic-cn")
        payload = {"model": "step-5-preview", "stream": True,
                   "messages": [{"role": "user", "content": "hi"}],
                   "temperature": 0.4, "reasoning_effort": "high",
                   "tools": [{"type": "function", "name": "f"}]}
        (_resp, _acct), _seen = self._run([error, ok], payload)
        self.assertEqual(len(self.sent_bodies), 2)
        second = self.sent_bodies[1]
        self.assertEqual(second["model"], "deepseek-v4.1-flash")
        self.assertNotIn("step", second["model"])
        # Everything else survives: a rewrite that dropped the caller's
        # parameters would silently change the answer they get.
        self.assertTrue(second["stream"])
        self.assertEqual(second["temperature"], 0.4)
        # The gateway normalises tool shapes before sending (a function tool
        # comes back nested under "function"), so compare the resolved name:
        # that is the part the rewrite must not drop.
        self.assertEqual([(t.get("name") or (t.get("function") or {}).get("name"))
                          for t in second["tools"]], ["f"])
        self.assertEqual([m.get("content") for m in second["messages"]
                          if m.get("role") == "user"],
                         [m.get("content") for m in payload["messages"]
                          if m.get("role") == "user"])
        # The reroute drops the caller's effort so deepseek gets its own
        # default; build_upstream_body then fills that default back in ('high').
        # What must NOT happen is the other family's effort riding along
        # unchanged, which for a low-effort model would silently over-spend.
        self.assertEqual(second.get("reasoning_effort"), "high")

    def test_no_rewrite_when_already_on_the_route(self):
        detail = json_dumps({"code": 14018, "msg": "insufficient credits"})
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 400, "bad request", {},
            io.BytesIO(detail.encode("utf-8")))
        ok = types.SimpleNamespace(uid="synthetic-cn")
        with self.assertRaises(urllib.error.HTTPError):
            self._run([error, ok],
                      {"model": "deepseek-v4.1-flash",
                       "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(self.sent_models, ["deepseek-v4.1-flash"])


class ExhaustedRouteDispatchTests(unittest.TestCase):
    """open_upstream() must retry on the designated route after 14018."""

    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn",
                                 "accessToken": "token"})

    def _run(self, script, payload):
        account = self.account()
        seen = []
        test = self
        test.sent_models = []

        class Pool(object):
            accounts = [account]
            affinity = types.SimpleNamespace(unbind=lambda _key: None)

            def count_ready(self, realm, model=None):
                return 1

            def apply_daily_token_limit(self, value=None, usage=None):
                return 0

            def apply_reserve_credits(self, value=None, usage=None):
                return 0

            def pick_for_session(self, realm, session_key=None, exclude=(), model=None):
                return account

        class Upstream(object):
            def __init__(self, inner):
                self.inner = inner

            def __call__(self, req, timeout=None, proxy=None):
                body = json_loads_safe(req.data)
                seen.append(body)
                test.sent_models.append(body.get("model"))
                item = self.inner.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        proxy.POOL = Pool()
        accounts.urlopen = Upstream(script)
        try:
            return proxy.open_upstream(payload, target_realm="cn"), seen
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen

    def test_14018_reroutes_to_deepseek_v41_flash(self):
        detail = json_dumps({"code": 14018, "msg": "insufficient credits"})
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 400, "bad request", {},
            io.BytesIO(detail.encode("utf-8")))
        ok = types.SimpleNamespace(uid="synthetic-cn")
        (resp, _acct), seen = self._run(
            [error, ok],
            {"model": "step-5-preview",
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertIs(resp, ok)
        self.assertEqual(len(seen), 2, "exactly one reroute, no retry storm")
        self.assertEqual(seen[0]["model"], "step-5-preview")
        self.assertEqual(seen[1]["model"], "deepseek-v4.1-flash")

    def test_non_credit_400_is_not_rerouted(self):
        detail = json_dumps({"code": 11102, "msg": "prompt is too long"})
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 400, "bad request", {},
            io.BytesIO(detail.encode("utf-8")))
        ok = types.SimpleNamespace(uid="synthetic-cn")
        with self.assertRaises(urllib.error.HTTPError):
            self._run(
                [error, ok],
                {"model": "glm-5.3",
                 "messages": [{"role": "user", "content": "hi"}]})
        # A parameter error is the caller's problem; rewriting the model would
        # hide it and burn a second request on the free tier. The stub records
        # every request it was handed, so exactly one means no reroute.
        self.assertEqual(self.sent_models, ["glm-5.3"])

    def test_chinese_credit_wording_also_reroutes(self):
        detail = json_dumps({"msg": "额度不足，请充值后重试"})
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 400, "bad request", {},
            io.BytesIO(detail.encode("utf-8")))
        ok = types.SimpleNamespace(uid="synthetic-cn")
        (resp, _acct), _seen = self._run(
            [error, ok],
            {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]})
        self.assertIs(resp, ok)
        self.assertEqual(self.sent_models, ["kimi-k3", "deepseek-v4.1-flash"])


def json_dumps(obj, ensure_ascii=False):
    import json
    return json.dumps(obj, ensure_ascii=ensure_ascii)


def json_loads_safe(raw):
    import json
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


if __name__ == "__main__":
    unittest.main()
