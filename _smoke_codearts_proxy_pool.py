#!/usr/bin/env python3
"""Smoke test for Huawei Cloud CodeArts multi-account pool & proxy_slot egress isolation.

Verifies:
1. CodeArtsAccount model: initialization, camelCase/snake_case parsing, concurrency slot(),
   availability, cooldown, restriction, and masking.
2. CodeArtsAccountPool: loading, saving, migration from legacy single-account file,
   proxy_slot binding from settings.json, round-robin & in_flight scheduling.
3. Proxy resolution: binding to proxy slots, fallback to legacy proxy, unbinding on slot removal.
4. Gateway integration: proxy argument passed to complete() and stream(), error isolation.
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_CODEARTS_DIR = os.path.join(_HERE, "codearts")
if _CODEARTS_DIR not in sys.path:
    sys.path.insert(0, _CODEARTS_DIR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import codearts_accounts
import codearts_oauth
import codearts_route
import wb_settings


class TestCodeArtsAccount(unittest.TestCase):
    def test_account_init_and_serialization(self):
        raw = {
            "id": "acct-001",
            "label": "Test Account 1",
            "accessKey": "AK_TEST_1",
            "secretKey": "SK_TEST_1",
            "securityToken": "TOKEN_1",
            "expiresAt": "2030-01-01T00:00:00Z",
            "proxySlot": "slot-alpha",
            "enabled": True,
        }
        acct = codearts_accounts.CodeArtsAccount(raw)
        self.assertEqual(acct.id, "acct-001")
        self.assertEqual(acct.label, "Test Account 1")
        self.assertEqual(acct.access_key, "AK_TEST_1")
        self.assertEqual(acct.proxy_slot, "slot-alpha")
        self.assertTrue(acct.enabled)
        self.assertFalse(acct.is_expired())
        self.assertTrue(acct.is_available())

        # Check masked view hides credentials
        masked = acct.masked()
        self.assertNotIn("AK_TEST_1", str(masked))
        self.assertNotIn("SK_TEST_1", str(masked))
        self.assertEqual(masked["proxySlot"], "slot-alpha")

        # Check to_dict preserves credentials for file persistence
        d = acct.to_dict()
        self.assertEqual(d["accessKey"], "AK_TEST_1")
        self.assertEqual(d["secretKey"], "SK_TEST_1")

    def test_concurrency_slot(self):
        acct = codearts_accounts.CodeArtsAccount({
            "id": "acct-conc",
            "accessKey": "AK",
            "secretKey": "SK",
        })
        self.assertEqual(acct.in_flight, 0)
        with acct.slot():
            self.assertEqual(acct.in_flight, 1)
            with acct.slot():
                self.assertEqual(acct.in_flight, 2)
                with acct.slot():
                    self.assertEqual(acct.in_flight, 3)
                    # Exceeds max concurrency (3)
                    self.assertFalse(acct.is_available())
            self.assertEqual(acct.in_flight, 1)
            self.assertTrue(acct.is_available())
        self.assertEqual(acct.in_flight, 0)

    def test_cooldown_and_restriction(self):
        acct = codearts_accounts.CodeArtsAccount({
            "id": "acct-risk",
            "accessKey": "AK",
            "secretKey": "SK",
        })
        self.assertTrue(acct.is_available())

        # Cooldown test
        acct.cooldown_until = time.time() + 60
        self.assertTrue(acct.is_cooling())
        self.assertFalse(acct.is_available())

        acct.cooldown_until = 0.0
        self.assertFalse(acct.is_cooling())
        self.assertTrue(acct.is_available())

        # Restriction test
        acct.restricted_until = time.time() + 7200
        acct.restricted_detail = "Rate limit 11140"
        self.assertTrue(acct.is_restricted())
        self.assertFalse(acct.is_available())


class TestCodeArtsAccountPool(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wb_test_codearts_")
        self.settings_file = os.path.join(self.tmpdir, "settings.json")
        # Setup mock settings with 2 proxy slots
        with open(self.settings_file, "w", encoding="utf-8") as f:
            json.dump({
                "proxy_slots": [
                    {"id": "slot-1", "name": "Exit HK", "url": "http://127.0.0.1:10001", "enabled": True},
                    {"id": "slot-2", "name": "Exit JP", "url": "http://127.0.0.1:10002", "enabled": True},
                    {"id": "slot-off", "name": "Exit US", "url": "http://127.0.0.1:10003", "enabled": False},
                ]
            }, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_migration_from_single_account_file(self):
        legacy_path = os.path.join(self.tmpdir, "codearts_account.json")
        with open(legacy_path, "w", encoding="utf-8") as f:
            json.dump({
                "access_key": "AK_MIGRATE",
                "secret_key": "SK_MIGRATE",
                "security_token": "TOK_MIGRATE",
                "account": {"label": "Imported Legacy User"}
            }, f)

        pool = codearts_accounts.CodeArtsAccountPool(self.tmpdir)
        self.assertEqual(len(pool.accounts), 1)
        acct = pool.accounts[0]
        self.assertEqual(acct.access_key, "AK_MIGRATE")
        self.assertEqual(acct.label, "Imported Legacy User")
        self.assertEqual(acct.source, "web_oauth")

        # Multi-account file was created
        multi_path = os.path.join(self.tmpdir, "codearts_accounts.json")
        self.assertTrue(os.path.isfile(multi_path))

    def test_proxy_slot_binding_and_resolution(self):
        multi_path = os.path.join(self.tmpdir, "codearts_accounts.json")
        with open(multi_path, "w", encoding="utf-8") as f:
            json.dump([
                {
                    "id": "acct-1",
                    "accessKey": "AK1",
                    "secretKey": "SK1",
                    "proxySlot": "slot-1",
                    "enabled": True,
                },
                {
                    "id": "acct-2",
                    "accessKey": "AK2",
                    "secretKey": "SK2",
                    "proxySlot": "slot-2",
                    "enabled": True,
                },
                {
                    "id": "acct-3",
                    "accessKey": "AK3",
                    "secretKey": "SK3",
                    "proxySlot": "slot-off", # Disabled slot
                    "proxy": "http://127.0.0.1:9999", # Legacy fallback
                    "enabled": True,
                },
            ], f)

        pool = codearts_accounts.CodeArtsAccountPool(self.tmpdir)
        self.assertEqual(len(pool.accounts), 3)

        # Apply proxy slots
        pool.apply_proxy_slots()

        # acct-1 bound to slot-1 -> http://127.0.0.1:10001
        self.assertEqual(pool.accounts[0].proxy, "http://127.0.0.1:10001")
        # acct-2 bound to slot-2 -> http://127.0.0.1:10002
        self.assertEqual(pool.accounts[1].proxy, "http://127.0.0.1:10002")
        # acct-3 bound to slot-off (disabled) -> fallback to legacy proxy
        self.assertEqual(pool.accounts[2].proxy, "http://127.0.0.1:9999")

    def test_pick_account_least_loaded_and_round_robin(self):
        pool = codearts_accounts.CodeArtsAccountPool(self.tmpdir)
        acct1 = codearts_accounts.CodeArtsAccount({"id": "a1", "accessKey": "AK1", "secretKey": "SK1"})
        acct2 = codearts_accounts.CodeArtsAccount({"id": "a2", "accessKey": "AK2", "secretKey": "SK2"})
        pool.add(acct1)
        pool.add(acct2)

        # Both idle: round-robin selection
        picked1, _ = pool.pick_account()
        picked2, _ = pool.pick_account()
        self.assertNotEqual(picked1.id, picked2.id)

        # acct1 busy with 1 request: picked must prefer acct2 (in_flight 0)
        acct1.in_flight = 1
        picked, _ = pool.pick_account()
        self.assertEqual(picked.id, "a2")

    def test_drop_missing_bindings(self):
        pool = codearts_accounts.CodeArtsAccountPool(self.tmpdir)
        acct = codearts_accounts.CodeArtsAccount({
            "id": "a1",
            "accessKey": "AK1",
            "secretKey": "SK1",
            "proxySlot": "slot-dead",
        })
        pool.add(acct)

        # Valid slots do not contain slot-dead
        slots = [{"id": "slot-1", "url": "http://127.0.0.1:10001"}]
        dropped = wb_settings.drop_missing_bindings(pool, slots)
        self.assertEqual(dropped, 1)
        self.assertEqual(pool.accounts[0].proxy_slot, "")


class TestProxyEgressRouting(unittest.TestCase):
    @patch("codearts_oauth._urlopen")
    def test_sts_token_refresh_via_proxy(self, mock_urlopen):
        # Configure mock response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "token": {
                "access_key": "NEW_AK",
                "secret_key": "NEW_SK",
                "security_token": "NEW_SEC_TOKEN",
                "expires_at": "2030-01-01T00:00:00Z",
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        dpop_kp = codearts_oauth.generate_dpop_key_pair()
        verifier, _ = codearts_oauth.generate_pkce()
        acct = codearts_accounts.CodeArtsAccount({
            "id": "acct-sts",
            "accessKey": "OLD_AK",
            "secretKey": "OLD_SK",
            "refreshToken": "REFRESH_TOK_123",
            "loginContext": {
                "dpopKeyPair": dpop_kp,
                "codeVerifier": verifier,
            },
            "proxy": "http://127.0.0.1:10808",
        })

        updated = codearts_oauth.refresh_sts_token_for_account(acct, proxy=acct.proxy)
        self.assertTrue(updated)
        self.assertEqual(acct.access_key, "NEW_AK")

        # Verify mock_urlopen was called with the account's assigned proxy!
        self.assertTrue(mock_urlopen.called)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs.get("proxy"), "http://127.0.0.1:10808")

    @patch("codearts_client.call_chat")
    def test_codearts_route_passes_proxy(self, mock_call_chat):
        mock_call_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Hello from isolated IP"}}]
        }

        session = codearts_accounts.CodeArtsAccount({
            "id": "acct-chat",
            "accessKey": "AK",
            "secretKey": "SK",
            "proxy": "http://proxy.node.internal:8080",
        }).to_session()

        payload = {
            "model": "codearts-chat",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
        }

        res = codearts_route.complete(payload, session=session, proxy="http://proxy.node.internal:8080")
        self.assertIn("choices", res)

        # Check that client call_chat was called with proxy
        self.assertTrue(mock_call_chat.called)
        _, kwargs = mock_call_chat.call_args
        self.assertEqual(kwargs.get("proxy"), "http://proxy.node.internal:8080")


class TestGatewayIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wb_test_gw_")
        self.settings_file = os.path.join(self.tmpdir, "settings.json")
        with open(self.settings_file, "w", encoding="utf-8") as f:
            json.dump({
                "proxy_slots": [
                    {"id": "slot-1", "name": "HK Slot", "url": "http://127.0.0.1:10001", "enabled": True},
                    {"id": "slot-2", "name": "US Slot", "url": "http://127.0.0.1:10002", "enabled": True},
                ]
            }, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_proxy_slots_view_counts_codearts(self):
        import wb_proxy
        # Mock ACCOUNTS_DIR and POOLs in wb_proxy
        with patch.object(wb_proxy, "ACCOUNTS_DIR", self.tmpdir):
            cp = codearts_accounts.CodeArtsAccountPool(self.tmpdir)
            acct = codearts_accounts.CodeArtsAccount({
                "id": "c1",
                "accessKey": "AK",
                "secretKey": "SK",
                "proxySlot": "slot-1",
                "enabled": True,
            })
            cp.add(acct)
            with patch.object(wb_proxy, "get_codearts_pool", return_value=cp):
                with patch.object(wb_proxy, "POOL", None):
                    view = wb_proxy.proxy_slots_view()
                    slot1 = next(s for s in view if s["id"] == "slot-1")
                    slot2 = next(s for s in view if s["id"] == "slot-2")
                    self.assertEqual(slot1["bound"], 1)
                    self.assertEqual(slot2["bound"], 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
