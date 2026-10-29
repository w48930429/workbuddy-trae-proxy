"""会话桥的离线测试。

用**合成**夹具验证解析与解密，不碰本机真实凭据：真实登录态是单次轮转且
DPoP 绑定的，测试里读写它会损坏用户的 IDE 登录。

覆盖：
  - v10 封装格式（前缀 / nonce / ciphertext+tag 的切分）
  - DPAPI 前缀剥离
  - 缺失密钥、缺失条目的报错路径
  - 两种字段大小写（上游不同版本用 securitytoken / securityToken）
  - 过期判定（解析失败按「不知道」处理，不能误报过期）
"""
import base64
import datetime
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "codearts"))

import codearts_session as sessions

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:      # pragma: no cover - cryptography is a repo dependency
    HAS_CRYPTO = False


SAMPLE = {
    "account": {"label": "hw12345678", "uid": "u-1"},
    "accessKey": "HSTAAAAAAAAAAAAAAAAAAA",
    "secretKey": "sk-secret-0123456789abcdef",
    "securitytoken": "sts-token-abc123",
    "expires_at": "2026-10-29T03:34:23.406Z",
    "refresh_token": "eyJhbGciOiJFUzI1NiJ9.payload.sig",
    "domainId": "08d862d7bd0010690f3cc018f928b1c0",
}


def pack(key, plaintext, prefix=b"v10"):
    """按 OSCrypt 的 v10 布局封装：prefix | nonce(12) | ciphertext+tag。"""
    nonce = b"\x11" * 12
    body = AESGCM(key).encrypt(nonce, plaintext, None)
    return prefix + nonce + body


@unittest.skipUnless(HAS_CRYPTO, "cryptography not installed")
class DecryptTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))

    def test_roundtrip(self):
        blob = pack(self.key, json.dumps(SAMPLE).encode())
        plain = sessions._aes_gcm_decrypt(self.key, blob)
        self.assertEqual(json.loads(plain)["accessKey"], SAMPLE["accessKey"])

    def test_rejects_short_payload(self):
        # 少于 prefix+nonce+tag 的长度无法切分，必须明确报错而不是算出一堆垃圾。
        with self.assertRaises(sessions.SessionUndecryptable):
            sessions._aes_gcm_decrypt(self.key, b"v10" + b"\x00" * 4)

    def test_rejects_wrong_prefix(self):
        blob = pack(self.key, b"{}", prefix=b"v11")
        with self.assertRaises(sessions.SessionUndecryptable) as caught:
            sessions._aes_gcm_decrypt(self.key, blob)
        self.assertIn("v10", str(caught.exception))

    def test_rejects_wrong_key(self):
        # GCM 认证失败说明 Local State 与本条目不匹配（换了 Windows 账户等）。
        blob = pack(self.key, json.dumps(SAMPLE).encode())
        with self.assertRaises(sessions.SessionUndecryptable):
            sessions._aes_gcm_decrypt(bytes(32), blob)


class ParseTests(unittest.TestCase):
    def test_parses_all_fields(self):
        session = sessions._parse_session(json.dumps(SAMPLE))
        self.assertEqual(session.access_key, SAMPLE["accessKey"])
        self.assertEqual(session.security_token, "sts-token-abc123")
        self.assertEqual(session.domain_id, SAMPLE["domainId"])
        self.assertEqual(session.account_label, "hw12345678")
        self.assertTrue(session.has_sts)

    def test_accepts_camel_case_variant(self):
        # 上游不同版本用 securitytoken / securityToken 两种写法。
        raw = dict(SAMPLE)
        del raw["securitytoken"]
        raw["securityToken"] = "camel-token"
        session = sessions._parse_session(json.dumps(raw))
        self.assertEqual(session.security_token, "camel-token")

    def test_missing_credentials_are_rejected(self):
        raw = dict(SAMPLE)
        del raw["accessKey"]
        with self.assertRaises(sessions.SessionUndecryptable) as caught:
            sessions._parse_session(json.dumps(raw))
        self.assertIn("accessKey", str(caught.exception))

    def test_non_json_is_rejected(self):
        with self.assertRaises(sessions.SessionUndecryptable):
            sessions._parse_session("not json at all")

    def test_masked_view_hides_secrets(self):
        session = sessions._parse_session(json.dumps(SAMPLE))
        view = session.masked()
        self.assertNotIn(SAMPLE["secretKey"], json.dumps(view))
        self.assertTrue(view["hasSecretKey"])
        self.assertTrue(view["hasSts"])
        # 脱敏后仍能认出是哪个账号，否则看板没法用。
        self.assertEqual(view["account"], "hw12345678")


class ExpiryTests(unittest.TestCase):
    def _session(self, stamp):
        return sessions.CodeArtsSession("AK", "SK", expires_at=stamp)

    def test_utc_z_suffix_is_parsed(self):
        parsed = sessions.expires_at_datetime(self._session("2026-10-29T03:34:23.406Z"))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, datetime.timezone.utc)

    def test_expired_is_detected(self):
        past = "2020-01-01T00:00:00Z"
        self.assertTrue(sessions.is_expired(self._session(past)))

    def test_future_is_not_expired(self):
        future = "2099-01-01T00:00:00Z"
        self.assertFalse(sessions.is_expired(self._session(future)))

    def test_unparsable_is_not_treated_as_expired(self):
        # 误报过期会拒掉本来能用的请求；不知道就该放行，让上游去判定。
        self.assertFalse(sessions.is_expired(self._session("not-a-date")))
        self.assertFalse(sessions.is_expired(self._session("")))

    def test_naive_timestamp_is_treated_as_utc(self):
        naive = "2099-01-01T00:00:00"
        self.assertFalse(sessions.is_expired(self._session(naive)))


class LocalStateTests(unittest.TestCase):
    def test_missing_local_state_is_a_clear_error(self):
        with self.assertRaises(sessions.SessionMissing) as caught:
            sessions.local_state_master_key(
                os.path.join(_ROOT, "tests", "__no_such_profile__"))
        self.assertIn("Local State", str(caught.exception))

    def test_non_dpapi_key_is_rejected(self):
        # 非 Windows 导出的 Local State 没有 DPAPI 包装，无法用本机主密钥解开。
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "Local State"), "w", encoding="utf-8") as fh:
                json.dump({"os_crypt": {"encrypted_key":
                                        base64.b64encode(b"v10rawkey").decode()}}, fh)
            with self.assertRaises(sessions.SessionUndecryptable) as caught:
                sessions.local_state_master_key(folder)
            self.assertIn("DPAPI", str(caught.exception))

    def test_missing_encrypted_key_is_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "Local State"), "w", encoding="utf-8") as fh:
                json.dump({"os_crypt": {}}, fh)
            with self.assertRaises(sessions.SessionMissing):
                sessions.local_state_master_key(folder)


if __name__ == "__main__":
    unittest.main(verbosity=2)
