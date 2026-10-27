"""Google OAuth credentials must come from the environment only.

Run with: python tests/_test_oauth_env_only.py
No network or real credentials are used.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "antigravity-proxy-py")
sys.path.insert(0, APP)

import config


class OAuthEnvOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oauth-env-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self.patch = mock.patch.object(config, "CONFIG_PATH", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_env_wins_and_secret_is_scrubbed_from_config_file(self):
        self._write({"port": 43811,
                     "oauthClientId": "file-client",
                     "oauthClientSecret": "file-secret"})
        env = {"OAUTH_CLIENT_ID": "env-client",
               "OAUTH_CLIENT_SECRET": "env-secret"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = config.load_config()

        self.assertEqual(cfg["oauthClientId"], "env-client")
        self.assertEqual(cfg["oauthClientSecret"], "env-secret")
        saved = self._read()
        self.assertNotIn("oauthClientId", saved)
        self.assertNotIn("oauthClientSecret", saved)
        self.assertEqual(saved["port"], 43811)

    def test_missing_env_means_no_credentials_not_a_hardcoded_fallback(self):
        self._write({"port": 43811,
                     "oauthClientId": "file-client",
                     "oauthClientSecret": "file-secret"})
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET")}
        with mock.patch.dict(os.environ, clean, clear=True):
            cfg = config.load_config()

        self.assertEqual(cfg["oauthClientId"], "")
        self.assertEqual(cfg["oauthClientSecret"], "")
        saved = self._read()
        self.assertNotIn("oauthClientId", saved)
        self.assertNotIn("oauthClientSecret", saved)

    def test_save_config_never_persists_credentials(self):
        config.save_config({"port": 43811, "oauthClientId": "a",
                            "oauthClientSecret": "b"})
        saved = self._read()
        self.assertEqual(saved, {"port": 43811})

    def test_source_has_no_compiled_in_oauth_secret(self):
        for rel in ("config.py", "pool/refresher.py", "admin/routes.py"):
            with open(os.path.join(APP, rel), encoding="utf-8") as fh:
                source = fh.read()
            self.assertNotIn("GOCSPX-", source, rel)
            self.assertNotIn("1071006060591-", source, rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
