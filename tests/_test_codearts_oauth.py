"""CodeArts OAuth2 PKCE + DPoP 单元测试。

验证：
1. PKCE (128-hex verifier + SHA-256 base64url challenge)
2. DPoP 密钥对生成、JWK 序列化与反序列化
3. RFC 9449 规范 DPoP 证明生成与 ES256 签名校验
4. 授权 URL 构造（必须绑定本项目监听端口）
5. 回调解析与错误处理
6. 会话加载多级回退机制 (Web OAuth -> Config AK/SK -> Desktop DPAPI)
"""

import base64
import hashlib
import json
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CODEARTS_DIR = os.path.join(_ROOT, "codearts")
if _CODEARTS_DIR not in sys.path:
    sys.path.insert(0, _CODEARTS_DIR)

import codearts_oauth
import codearts_session
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


class PkceTests(unittest.TestCase):
    def test_pkce_generation(self):
        verifier, challenge = codearts_oauth.generate_pkce()
        self.assertEqual(len(verifier), 128)
        self.assertTrue(all(c in "0123456789abcdef" for c in verifier))

        # 验证 challenge 确为 verifier 的 SHA-256 base64url
        expected_digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected_challenge = base64.urlsafe_b64encode(expected_digest).decode("ascii").rstrip("=")
        self.assertEqual(challenge, expected_challenge)


class DPoPTests(unittest.TestCase):
    def test_dpop_key_generation_and_jwk_export(self):
        pair = codearts_oauth.generate_dpop_key_pair()
        pub_jwk = pair["publicKeyJwk"]
        priv_jwk = pair["privateKeyJwk"]
        self.assertEqual(pub_jwk["kty"], "EC")
        self.assertEqual(pub_jwk["crv"], "P-256")
        self.assertTrue(pub_jwk["x"])
        self.assertTrue(pub_jwk["y"])
        self.assertNotIn("d", pub_jwk)

        self.assertEqual(priv_jwk["kty"], "EC")
        self.assertEqual(priv_jwk["crv"], "P-256")
        self.assertTrue(priv_jwk["d"])

    def test_jwk_roundtrip(self):
        pair = codearts_oauth.generate_dpop_key_pair()
        pub_jwk = pair["publicKeyJwk"]
        priv_jwk = pair["privateKeyJwk"]
        priv_key = codearts_oauth.import_private_key_from_jwk(priv_jwk)
        re_priv, re_pub = codearts_oauth.export_jwk_from_private_key(priv_key)
        self.assertEqual(pub_jwk, re_pub)
        self.assertEqual(priv_jwk, re_priv)

    def test_dpop_proof_verification(self):
        pair = codearts_oauth.generate_dpop_key_pair()
        pub_jwk = pair["publicKeyJwk"]
        priv_jwk = pair["privateKeyJwk"]
        priv_key = codearts_oauth.import_private_key_from_jwk(priv_jwk)
        htm = "POST"
        htu = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"

        proof = codearts_oauth.create_dpop_proof(pair, htm, htu)
        parts = proof.split(".")
        self.assertEqual(len(parts), 3)
        def b64url_decode(s):
            padding = "=" * ((4 - len(s) % 4) % 4)
            return base64.urlsafe_b64decode(s + padding)

        header = json.loads(b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
        raw_sig = b64url_decode(parts[2])

        self.assertEqual(header["typ"], "dpop+jwt")
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["jwk"]["x"], pub_jwk["x"])
        self.assertEqual(payload["htm"], htm)
        self.assertEqual(payload["htu"], htu)
        self.assertTrue("jti" in payload)
        self.assertTrue("iat" in payload)

        # 验证 IEEE P1363 64 字节签名正确性
        self.assertEqual(len(raw_sig), 64)
        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        der_sig = encode_dss_signature(r, s)

        tbs = f"{parts[0]}.{parts[1]}".encode("ascii")
        pub_key = priv_key.public_key()
        # verify 不抛异常即为成功
        pub_key.verify(der_sig, tbs, ec.ECDSA(hashes.SHA256()))


class AuthorizeUrlTests(unittest.TestCase):
    def test_authorize_url_formatting(self):
        port = 28999
        challenge = "test-challenge-123"
        url = codearts_oauth.build_authorize_url(port, challenge)

        self.assertTrue(url.startswith("https://codearts.huaweicloud.com/portal/authorize?"))
        self.assertIn("port=28999", url)
        self.assertIn("code_challenge=test-challenge-123", url)
        self.assertIn("code_challenge_method=SHA-256", url)
        self.assertIn("client_id=codearts-agent", url)
        self.assertIn("uri_scheme=codearts-agent", url)

    def test_custom_port(self):
        url = codearts_oauth.build_authorize_url(39999, "abc")
        self.assertIn("port=39999", url)


class OAuthLifecycleTests(unittest.TestCase):
    def test_start_and_poll_session(self):
        info = codearts_oauth.start_oauth_login(28999)
        self.assertTrue(info["login_url"])
        self.assertEqual(info["port"], 28999)

        poll = codearts_oauth.get_oauth_poll_result()
        self.assertEqual(poll["status"], "pending")

    def test_callback_error_query(self):
        query = {"error": ["access_denied"], "error_description": ["User cancelled"]}
        ok, html, result = codearts_oauth.handle_oauth_callback(query, project_port=28999)
        self.assertFalse(ok)
        self.assertIn("access_denied", html)
        self.assertEqual(result["status"], "failed")

    def test_callback_missing_code(self):
        query = {}
        ok, html, result = codearts_oauth.handle_oauth_callback(query, project_port=28999)
        self.assertFalse(ok)
        self.assertIn("Missing authorization code", html)
        self.assertEqual(result["status"], "failed")


class SessionTierTests(unittest.TestCase):
    def test_priority_web_oauth(self):
        fake_web_session = codearts_session.CodeArtsSession(
            access_key="AK_WEB_123456",
            secret_key="SK_WEB_SECRET",
            account_label="WebUser",
            source="web_oauth"
        )
        with patch.object(codearts_session, "load_web_session", return_value=fake_web_session):
            session = codearts_session.load_session()
            self.assertEqual(session.source, "web_oauth")
            self.assertEqual(session.access_key, "AK_WEB_123456")

    def test_priority_config_ak_sk(self):
        fake_cfg_session = codearts_session.CodeArtsSession(
            access_key="AK_CONFIG_123456",
            secret_key="SK_CONFIG_SECRET",
            account_label="ConfigUser",
            source="config_ak_sk"
        )
        with patch.object(codearts_session, "load_web_session", return_value=None):
            with patch.object(codearts_session, "load_config_ak_sk", return_value=fake_cfg_session):
                session = codearts_session.load_session()
                self.assertEqual(session.source, "config_ak_sk")
                self.assertEqual(session.access_key, "AK_CONFIG_123456")

    def test_priority_desktop_fallback(self):
        fake_desk_session = codearts_session.CodeArtsSession(
            access_key="AK_DESK_123456",
            secret_key="SK_DESK_SECRET",
            account_label="DesktopUser",
            source="desktop"
        )
        with patch.object(codearts_session, "load_web_session", return_value=None):
            with patch.object(codearts_session, "load_config_ak_sk", return_value=None):
                with patch.object(codearts_session, "load_desktop_session", return_value=fake_desk_session):
                    session = codearts_session.load_session()
                    self.assertEqual(session.source, "desktop")
                    self.assertEqual(session.access_key, "AK_DESK_123456")


if __name__ == "__main__":
    unittest.main()
