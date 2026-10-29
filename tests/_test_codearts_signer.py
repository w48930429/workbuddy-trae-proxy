"""SDK-HMAC-SHA256 签名器的金标准测试（离线，不联网）。

期望值来自上游 JS signer 语义的独立复刻（见 codearts_signer_vectors 文件头）。
这四个 Authorization 向量是「签名是否与上游一致」的唯一可信判据 ——
在线探测不可靠：APIG 先校验 security token 是否过期，再校验签名，
因此用过期凭据 + 故意写错的 secretKey 也会返回 `APIG.0602` 而不是
`APIG.0301`，无法据此判断签名对错。
"""
import os
import re
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "codearts"))

import codearts_signer as signer
import codearts_signer_vectors as vectors


class SignRequestVectorTests(unittest.TestCase):
    def test_authorization_matches_golden_vectors(self):
        for case in vectors.CASES:
            with self.subTest(case=case["name"]):
                got = signer.sign_request(
                    case["ak"], case["sk"], case["sts"], case["method"],
                    case["url"], case["body"], case["headers"])
                self.assertEqual(got["Authorization"], case["want"])

    def test_host_always_derived_from_url(self):
        for case in vectors.CASES:
            with self.subTest(case=case["name"]):
                got = signer.sign_request(
                    case["ak"], case["sk"], case["sts"], case["method"],
                    case["url"], case["body"], case["headers"])
                self.assertEqual(got["host"], urllib.parse.urlsplit(case["url"]).netloc)

    def test_security_token_only_when_present(self):
        for case in vectors.CASES:
            with self.subTest(case=case["name"]):
                got = signer.sign_request(
                    case["ak"], case["sk"], case["sts"], case["method"],
                    case["url"], case["body"], case["headers"])
                self.assertEqual("x-security-token" in got, bool(case["sts"]))

    def test_header_keys_are_lowercased(self):
        got = signer.sign_request("AK", "SK", "", "GET", "https://h/p", b"",
                                  {"X-Sdk-Date": "20260101T000000Z"})
        self.assertIn("x-sdk-date", got)
        self.assertNotIn("X-Sdk-Date", got)

    def test_input_headers_are_not_mutated(self):
        supplied = {"Content-Type": "application/json",
                    "X-Sdk-Date": "20260101T000000Z"}
        before = dict(supplied)
        signer.sign_request("AK", "SK", "STS", "POST", "https://h/p",
                            b"{}", supplied)
        self.assertEqual(supplied, before, "caller's dict must not be modified")
        self.assertNotIn("host", supplied)

    def test_auto_date_format(self):
        got = signer.sign_request("AK", "SK", "", "GET", "https://h/p")
        self.assertRegex(got["x-sdk-date"], r"^\d{8}T\d{6}Z$")
        self.assertTrue(got["Authorization"].startswith("SDK-HMAC-SHA256 Access=AK, "))


class CanonicalisationTests(unittest.TestCase):
    def test_canonical_uri(self):
        for raw, want in vectors.URI_CASES:
            with self.subTest(url=raw):
                got = signer.canonical_uri(urllib.parse.urlsplit(raw))
                self.assertEqual(got, want)

    def test_canonical_query(self):
        for raw, want in vectors.QUERY_CASES:
            with self.subTest(query=raw):
                parts = urllib.parse.urlsplit("https://h/p?" + raw)
                self.assertEqual(signer.canonical_query(parts), want)

    def test_js_url_encode(self):
        for raw, want in vectors.ENCODE_CASES:
            with self.subTest(text=raw):
                self.assertEqual(signer.js_url_encode(raw), want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
