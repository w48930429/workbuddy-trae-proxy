"""Unit and integration tests for Qoder dual-realm (global/intl vs cn) isolation.

Run with:
  "C:/Users/wy073/AppData/Local/Programs/Python/Python312/python.exe" tests/test_qoder_dual_realm.py
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qoder"))

import qoder_client as client
import qoder_pool as pool
import qoder_route as route
import qoder_claude as claude
import qoder_codex as codex
import qoder_accounts as accounts


class MockBearer:
    def __init__(self, region="cn"):
        self.region = region

    def call_get(self, url):
        return {"region": self.region, "url": url}


def make_slot(slot_id: str, region: str = "cn", enabled: bool = True, cooling_sec: int = 0) -> pool.Slot:
    slot = pool.Slot(
        id=slot_id,
        name=f"Name-{slot_id}",
        region=region,
        bearer=MockBearer(region),
        enabled=enabled,
    )
    if cooling_sec > 0:
        slot.cool(cooling_sec, "Test cooling")
    return slot


class TestQoderDualRealm(unittest.TestCase):

    def test_normalize_realm(self):
        """Verify region normalization handles aliases consistently."""
        self.assertEqual(pool.normalize_realm("intl"), "global")
        self.assertEqual(pool.normalize_realm("international"), "global")
        self.assertEqual(pool.normalize_realm("global"), "global")
        self.assertEqual(pool.normalize_realm("GLOBAL"), "global")
        self.assertEqual(pool.normalize_realm("InTl"), "global")

        self.assertEqual(pool.normalize_realm("cn"), "cn")
        self.assertEqual(pool.normalize_realm("china"), "cn")
        self.assertEqual(pool.normalize_realm("domestic"), "cn")
        self.assertEqual(pool.normalize_realm("CN"), "cn")
        self.assertEqual(pool.normalize_realm("China"), "cn")

        self.assertEqual(pool.normalize_realm(""), "")
        self.assertEqual(pool.normalize_realm(None), "")
        self.assertEqual(pool.normalize_realm("other"), "other")

    def test_upstream_model_ids_map_to_their_own_key(self):
        """Every model exposed to clients must map to its own upstream key.

        `Kimi-K3` used to fall through the substring rule to `kmodel`
        (Kimi-K2.8-Preview), silently serving the older model under the
        newer model's name.
        """
        expected = {
            "Auto": "auto",
            "Ultimate": "ultimate",
            "Efficient": "efficient",
            "Qwen3.8-Max": "qmodel_38max",
            "Qwen3.8-Flash": "qfmodel",
            "Kimi-K3.1-Pro": "kmodel_latest",
            "Kimi-K2.8-Preview": "kmodel",
            "GLM-5.3": "gmodel",
            "GLM-5.3-Flash": "gfmodel",
            "DeepSeek-V4-Pro": "dmodel",
            "DeepSeek-Flash": "dfmodel",
            "MiniMax-M3.1-Flash": "mmodel",
            "Step-5-Preview": "jmodel",
        }
        for model_id, key in expected.items():
            self.assertEqual(route.map_model(model_id), key, model_id)

    def test_pool_size_and_snapshot_by_realm(self):
        """Verify Pool size and snapshot filter by realm correctly."""
        slots = [
            make_slot("cn-1", region="cn"),
            make_slot("cn-2", region="cn"),
            make_slot("intl-1", region="global"),
            make_slot("intl-2", region="global"),
            make_slot("intl-3", region="global"),
        ]
        p = pool.Pool(slots=slots)

        self.assertEqual(p.size(), 5)
        self.assertEqual(p.size(realm="cn"), 2)
        self.assertEqual(p.size(realm="global"), 3)
        self.assertEqual(p.size(realm="intl"), 3)

        snap_all = p.snapshot()
        self.assertEqual(len(snap_all), 5)

        snap_cn = p.snapshot(realm="cn")
        self.assertEqual(len(snap_cn), 2)
        for s in snap_cn:
            self.assertEqual(s.region, "cn")

        snap_intl = p.snapshot(realm="intl")
        self.assertEqual(len(snap_intl), 3)
        for s in snap_intl:
            self.assertEqual(s.region, "global")

    def test_pool_pick_realm_isolation(self):
        """Verify Pool.pick never picks a slot from the wrong realm."""
        s_cn = make_slot("cn-slot", region="cn")
        s_global = make_slot("global-slot", region="global")
        p = pool.Pool(slots=[s_cn, s_global])

        # Explicit global / intl
        for _ in range(5):
            picked, ok = p.pick(realm="global")
            self.assertTrue(ok)
            self.assertIsNotNone(picked)
            self.assertEqual(picked.id, "global-slot")
            self.assertEqual(picked.region, "global")

            picked_intl, ok_intl = p.pick(realm="intl")
            self.assertTrue(ok_intl)
            self.assertIsNotNone(picked_intl)
            self.assertEqual(picked_intl.id, "global-slot")

        # Explicit cn
        for _ in range(5):
            picked_cn, ok_cn = p.pick(realm="cn")
            self.assertTrue(ok_cn)
            self.assertIsNotNone(picked_cn)
            self.assertEqual(picked_cn.id, "cn-slot")
            self.assertEqual(picked_cn.region, "cn")

        # Empty realm if no matching accounts
        p_only_cn = pool.Pool(slots=[s_cn])
        picked_none, ok_none = p_only_cn.pick(realm="global")
        self.assertFalse(ok_none)
        self.assertIsNone(picked_none)

        picked_none2, ok_none2 = p_only_cn.pick(realm="intl")
        self.assertFalse(ok_none2)
        self.assertIsNone(picked_none2)

        picked_ok, ok_cn2 = p_only_cn.pick(realm="cn")
        self.assertTrue(ok_cn2)
        self.assertEqual(picked_ok.id, "cn-slot")

    def test_pick_slot_for_model_dual_realm(self):
        """Verify route.pick_slot_for_model isolates slots and supports model catalog."""
        s_cn = make_slot("cn-active", region="cn")
        s_global = make_slot("global-active", region="global")
        p = pool.Pool(slots=[s_cn, s_global])

        # Pick for global
        slot_g, ok_g = route.pick_slot_for_model(p, "DeepSeek-Flash", realm="global")
        self.assertTrue(ok_g)
        self.assertEqual(slot_g.id, "global-active")
        self.assertEqual(slot_g.region, "global")

        # Pick for cn
        slot_c, ok_c = route.pick_slot_for_model(p, "DeepSeek-Flash", realm="cn")
        self.assertTrue(ok_c)
        self.assertEqual(slot_c.id, "cn-active")
        self.assertEqual(slot_c.region, "cn")

        # Pick with no slots in target realm returns (None, False)
        p_cn_only = pool.Pool(slots=[s_cn])
        slot_none, ok_none = route.pick_slot_for_model(p_cn_only, "DeepSeek-Flash", realm="intl")
        self.assertFalse(ok_none)
        self.assertIsNone(slot_none)

    def test_friendly_error_realm_awareness(self):
        """Verify _friendly_error incorporates realm context when accounts are missing or cooling."""
        s_cn = make_slot("cn-1", region="cn", cooling_sec=60)
        p = pool.Pool(slots=[s_cn])

        # When realm has no accounts configured
        msg_no_acct, err_type = route._friendly_error(pool.NoAccountError(), p=p, realm="global")
        self.assertEqual(err_type, "no_available_account")
        self.assertIn("global", msg_no_acct)

        # When all accounts in realm are cooling
        msg_cooling, err_type2 = route._friendly_error(pool.NoAccountError(), p=p, realm="cn")
        self.assertEqual(err_type2, "no_available_account")
        self.assertIn("cn", msg_cooling)
        self.assertIn("冷却中", msg_cooling)

    def test_claude_route_realm_propagation(self):
        """Verify call_qoder_claude forwards realm down to _call_with_pool."""
        s_cn = make_slot("cn-slot", region="cn")
        s_global = make_slot("global-slot", region="global")
        mock_p = pool.Pool(slots=[s_cn, s_global])

        with mock.patch("qoder_route.get_pool", return_value=mock_p):
            with mock.patch("qoder_claude._call_with_pool") as mock_call:
                mock_call.return_value = (200, '{"id":"msg_1"}')
                status, body = claude.call_qoder_claude(
                    {"model": "claude-3-5-sonnet-20241022", "messages": []},
                    realm="global"
                )
                self.assertEqual(status, 200)
                mock_call.assert_called_once()
                _, kwargs = mock_call.call_args
                self.assertEqual(kwargs.get("realm"), "global")

    def test_codex_route_realm_propagation(self):
        """Verify call_qoder_codex forwards realm down to _call_with_pool."""
        s_cn = make_slot("cn-slot", region="cn")
        s_global = make_slot("global-slot", region="global")
        mock_p = pool.Pool(slots=[s_cn, s_global])

        with mock.patch("qoder_route.get_pool", return_value=mock_p):
            with mock.patch("qoder_codex._call_with_pool") as mock_call:
                mock_call.return_value = (200, '{"id":"resp_1"}')
                status, body = codex.call_qoder_codex(
                    {"model": "codex", "messages": []},
                    realm="cn"
                )
                self.assertEqual(status, 200)
                mock_call.assert_called_once()
                _, kwargs = mock_call.call_args
                self.assertEqual(kwargs.get("realm"), "cn")

    def test_qoder_models_dual_realm_probing(self):
        """Verify route.qoder_models probes slots per realm or merges both."""
        s_cn = make_slot("cn-slot", region="cn")
        s_global = make_slot("global-slot", region="global")
        mock_p = pool.Pool(slots=[s_cn, s_global])

        def fake_parse_qoder_models(resp):
            if resp.get("region") == "cn":
                return [
                    {"key": "qfmodel", "display_name": "DeepSeek-Flash", "context_window": 64000},
                    {"key": "qwen3.8-flash", "display_name": "Qwen3.8-Flash", "context_window": 128000},
                ]
            else:
                return [
                    {"key": "qfmodel", "display_name": "DeepSeek-Flash", "context_window": 64000},
                    {"key": "claude-sonnet", "display_name": "Claude-3.5-Sonnet", "context_window": 200000},
                ]

        with mock.patch("qoder_route.get_pool", return_value=mock_p):
            with mock.patch("qoder_client.parse_qoder_models", side_effect=fake_parse_qoder_models):
                # Realm cn
                models_cn = route.qoder_models(realm="cn")
                ids_cn = [m["id"] for m in models_cn]
                self.assertIn("DeepSeek-Flash", ids_cn)
                self.assertIn("Qwen3.8-Flash", ids_cn)
                self.assertNotIn("Claude-3.5-Sonnet", ids_cn)

                # Realm global
                models_global = route.qoder_models(realm="global")
                ids_global = [m["id"] for m in models_global]
                self.assertIn("DeepSeek-Flash", ids_global)
                self.assertIn("Claude-3.5-Sonnet", ids_global)
                self.assertNotIn("Qwen3.8-Flash", ids_global)

                # Realm "" (merged probe across cn and global)
                models_all = route.qoder_models(realm="")
                ids_all = [m["id"] for m in models_all]
                self.assertIn("DeepSeek-Flash", ids_all)
                self.assertIn("Qwen3.8-Flash", ids_all)
                self.assertIn("Claude-3.5-Sonnet", ids_all)
                self.assertEqual(len(ids_all), len(set(ids_all)))

    def test_list_accounts_filter_by_region(self):
        """Verify qoder_accounts.list_accounts filters by normalized region."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("qoder_accounts.accounts_dir", return_value=tmpdir):
                accounts.save_account(accounts.Account(id="a1", name="CN 1", email="c1@example.com", region="cn"))
                accounts.save_account(accounts.Account(id="a2", name="CN 2", email="c2@example.com", region="cn"))
                accounts.save_account(accounts.Account(id="a3", name="Intl 1", email="i1@example.com", region="global"))

                all_accts = accounts.list_accounts()
                self.assertEqual(len(all_accts), 3)

                cn_accts = accounts.list_accounts(region="cn")
                self.assertEqual(len(cn_accts), 2)
                for a in cn_accts:
                    self.assertEqual(a.region, "cn")

                intl_accts = accounts.list_accounts(region="intl")
                self.assertEqual(len(intl_accts), 1)
                self.assertEqual(intl_accts[0].region, "global")

                global_accts = accounts.list_accounts(region="global")
                self.assertEqual(len(global_accts), 1)
                self.assertEqual(global_accts[0].region, "global")

    def test_gateway_request_realm_resolution(self):
        """Verify wb_proxy RequestHandler _request_realm extracts realm according to priority."""
        import wb_proxy

        class DummyHandler:
            def __init__(self, path="/", headers=None):
                self.path = path
                self.headers = headers or {}
            _panel_ok = lambda self: False
            _key_realm = lambda self: None
            _request_realm = wb_proxy.Handler._request_realm

        # Explicit takes highest priority
        h1 = DummyHandler(path="/v1/chat/completions?realm=cn", headers={"X-Realm": "cn"})
        self.assertEqual(h1._request_realm(explicit="global"), "global")

        # URL query parameter
        h2 = DummyHandler(path="/v1/chat/completions?realm=intl")
        self.assertEqual(h2._request_realm(), "intl")

        # Header X-Realm
        h3 = DummyHandler(path="/v1/chat/completions", headers={"X-Realm": "global"})
        self.assertEqual(h3._request_realm(), "global")

        # None when no realm specified
        h4 = DummyHandler(path="/v1/chat/completions", headers={})
        self.assertIsNone(h4._request_realm())

    def test_qoder_models_merged_regions_coverage(self):
        """Merged catalog lists every realm a model appears in, not just the first."""
        s_cn = make_slot("cn-slot", region="cn")
        s_global = make_slot("global-slot", region="global")
        mock_p = pool.Pool(slots=[s_cn, s_global])

        def fake_parse_qoder_models(resp):
            if resp.get("region") == "cn":
                return [
                    {"key": "qfmodel", "display_name": "Shared-Model", "context_window": 64000},
                    {"key": "jmodel", "display_name": "Cn-Only", "context_window": 64000},
                ]
            return [
                {"key": "qfmodel", "display_name": "Shared-Model", "context_window": 128000},
                {"key": "ultimate", "display_name": "Global-Only", "context_window": 200000},
            ]

        with mock.patch("qoder_route.get_pool", return_value=mock_p):
            with mock.patch("qoder_client.parse_qoder_models", side_effect=fake_parse_qoder_models):
                merged = {m["id"]: m for m in route.qoder_models(realm="")}
                self.assertEqual(sorted(merged["Shared-Model"]["regions"]), ["cn", "global"])
                self.assertEqual(merged["Cn-Only"]["regions"], ["cn"])
                self.assertEqual(merged["Global-Only"]["regions"], ["global"])

                only_global = {m["id"]: m for m in route.qoder_models(realm="global")}
                self.assertEqual(only_global["Shared-Model"]["regions"], ["global"])
                self.assertEqual(only_global["Shared-Model"]["context_window"], 128000)

    def test_qoder_usage_realm_filtering(self):
        """Usage totals and rows are filtered by the region of the account that served them."""
        import tempfile
        import time as _time
        with tempfile.TemporaryDirectory() as tmpdir:
            usage_file = os.path.join(tmpdir, "usage.jsonl")
            with open(usage_file, "w", encoding="utf-8") as f:
                for account_id, model, tokens in (("a1", "Qwen3.8-Max", 10),
                                                  ("a2", "GLM-5.3", 20),
                                                  ("i1", "Ultimate", 40)):
                    f.write(json.dumps({
                        "seq": 1, "at": int(_time.time()), "iso": "2026-11-07T00:00:00",
                        "model": model, "account": account_id, "stream": True,
                        "outcome": "completed", "elapsed_ms": 100,
                        "prompt_tokens": tokens, "completion_tokens": tokens,
                        "total_tokens": tokens, "error": "",
                    }) + "\n")
            with mock.patch("qoder_accounts.accounts_dir", return_value=tmpdir):
                accounts.save_account(accounts.Account(id="a1", name="CN 1", email="c1@example.com", region="cn"))
                accounts.save_account(accounts.Account(id="a2", name="CN 2", email="c2@example.com", region="cn"))
                accounts.save_account(accounts.Account(id="i1", name="Intl 1", email="i1@example.com", region="global"))
                with mock.patch.object(route, "USAGE_FILE", usage_file):
                    all_snap = route.qoder_usage_snapshot()
                    self.assertEqual(all_snap["requests"], 3)
                    self.assertEqual(all_snap["total_tokens"], 70)

                    cn_snap = route.qoder_usage_snapshot(realm="cn")
                    self.assertEqual(cn_snap["requests"], 2)
                    self.assertEqual(cn_snap["total_tokens"], 30)
                    self.assertEqual(sorted(cn_snap["by_account"]), ["a1", "a2"])

                    global_snap = route.qoder_usage_snapshot(realm="global")
                    self.assertEqual(global_snap["requests"], 1)
                    self.assertEqual(list(global_snap["by_model"]), ["Ultimate"])

                    self.assertEqual(route.recent_qoder_usage(20, 1)["total"], 3)
                    self.assertEqual(route.recent_qoder_usage(20, 1, realm="cn")["total"], 2)
                    global_rows = route.recent_qoder_usage(20, 1, realm="global")
                    self.assertEqual(global_rows["total"], 1)
                    self.assertEqual(global_rows["rows"][0]["account"], "i1")

    def test_compat_routes_report_serving_account(self):
        """Codex/Claude compat routes must record the slot that served the call."""
        slot = make_slot("acct-cn-1", region="cn")
        mock_p = pool.Pool(slots=[slot])
        mock_p.note_success = lambda *a, **k: None

        with mock.patch("qoder_route.pick_slot_for_model", return_value=(slot, True)):
            with mock.patch("qoder_route.stream_with_slot", return_value=(False, None)):
                for mod in (codex, claude):
                    out = []
                    status, _body = mod._call_with_pool(
                        mock_p, {}, {}, "qfmodel", set(), lambda d: None,
                        realm="cn", account_out=out)
                    self.assertEqual(status, 200)
                    self.assertEqual(out, ["acct-cn-1"])

                # Callers without the optional out-list keep working unchanged.
                status, _body = codex._call_with_pool(
                    mock_p, {}, {}, "qfmodel", set(), lambda d: None, realm="cn")
                self.assertEqual(status, 200)

    def test_missing_price_factor_is_not_a_free_model(self):
        """An absent price_factor must stay None, not become a 0x (free) model.

        The panel renders price_factor == 0 as a free model, so coercing a
        missing field to 0 advertises a discount upstream never quoted.
        """
        parsed = client.parse_qoder_models({"assistant": [
            {"key": "kmodel", "display_name": "No-Factor", "context_window": 64000},
            {"key": "gfmodel", "display_name": "Zero-Factor", "context_window": 64000,
             "price_factor": 0},
            {"key": "qmodel", "display_name": "Priced", "context_window": 64000,
             "price_factor": 0.5},
        ]})
        by_id = {m["display_name"]: m for m in parsed}
        self.assertIsNone(by_id["No-Factor"]["price_factor"])
        self.assertEqual(by_id["Zero-Factor"]["price_factor"], 0.0)
        self.assertEqual(by_id["Priced"]["price_factor"], 0.5)

    def test_parse_qoder_models_includes_app_category_qoduck(self):
        """Verify parse_qoder_models aggregates app category and maps QoDuck (0.0x free)."""
        resp = {
            "app": [
                {
                    "key": "lmodel",
                    "display_name": "QoDuck",
                    "enable": True,
                    "price_factor": 0.0,
                    "is_reasoning": True,
                    "context_config": {"default_window": {"token_count": 200000}},
                }
            ],
            "assistant": [
                {
                    "key": "auto",
                    "display_name": "Auto",
                    "enable": True,
                    "price_factor": 0.5,
                    "is_reasoning": True,
                }
            ],
        }
        parsed = client.parse_qoder_models(resp)
        self.assertEqual(len(parsed), 2)
        qoduck = next((m for m in parsed if m["key"] == "lmodel"), None)
        self.assertIsNotNone(qoduck)
        self.assertEqual(qoduck["display_name"], "QoDuck")
        self.assertEqual(qoduck["price_factor"], 0.0)
        self.assertTrue(qoduck["is_reasoning"])
        self.assertEqual(qoduck["context_window"], 200000)
        self.assertEqual(qoduck["max_output_tokens"], 32768)

    def test_model_mapping_qoduck(self):
        """Verify QoDuck and lmodel aliases resolve correctly in route.map_model."""
        self.assertEqual(route.map_model("QoDuck"), "lmodel")
        self.assertEqual(route.map_model("qoduck"), "lmodel")
        self.assertEqual(route.map_model("lmodel"), "lmodel")
        self.assertEqual(route.map_model("LMODEL"), "lmodel")

if __name__ == "__main__":
    unittest.main()
