"""金标准向量来自 2951461586/CodeArts2API 的 ``internal/codearts/signer_test.go``。

那份测试的期望值不是在它自己的 Go 实现里生成的，而是按
``@huaweicloud/huaweicloud-sdk-core`` 的 JS signer 语义独立复刻后算出的 ——
所以拿它当基线，等于用上游真实行为校验本文件，而不是自我印证。

四个 Authorization 向量覆盖：POST+body+STS、GET 带查询且 body 不参与签名、
需要 JS 编码的路径/查询、PUT 空 body。任何一处 canonical 拼装改动都会让
这里失败。

未来若上游变更签名语义，先改测试向量（并在提交信息里附证据），再改实现。
"""

CASES = [
    {
        "name": "POST with body, STS and padded header value",
        "ak": "HSTAAAAAAAAAAAAAAAAAAA",
        "sk": "sk-secret-0123456789abcdef",
        "sts": "sts-token-abc123",
        "method": "POST",
        "url": "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions",
        "body": '{"model":"glm-5.3-flash","stream":false}',
        "headers": {
            "Content-Type": "application/json",
            "X-Sdk-Date": "20260101T000000Z",
            "maas_type": "benefit",
            "model-id": "glm-5.3-flash",
            "X-Ot-Function": "  agent-general  ",
        },
        "want": ("SDK-HMAC-SHA256 Access=HSTAAAAAAAAAAAAAAAAAAA"
                 ", SignedHeaders=content-type;host;maas_type;model-id;"
                 "x-ot-function;x-sdk-date;x-security-token"
                 ", Signature=d74d70f38c5d7e69b73c728740a2c7397a4191737d93f207d1a6f5fce49e99c4"),
    },
    {
        "name": "GET with query, body must not be signed",
        "ak": "AKID",
        "sk": "SECRET",
        "sts": "",
        "method": "GET",
        "url": ("https://opengw.developer.huaweicloud.com"
                "/api/v1/user/tokens/balance?b=2&a=1&a=0"),
        "body": '{"ignored":true}',
        "headers": {"X-Sdk-Date": "20260203T040506Z"},
        "want": ("SDK-HMAC-SHA256 Access=AKID, SignedHeaders=host;x-sdk-date"
                 ", Signature=0acd5e903f49fd4e91d71aee6188d446d6f76863c5d8f13a4d428c0af017af45"),
    },
    {
        "name": "path and query need JS urlencode",
        "ak": "AKID",
        "sk": "SECRET",
        "sts": "TOK",
        "method": "POST",
        "url": ("https://snap-access.cn-north-4.myhuaweicloud.com"
                "/api/v2/models/模型 名称/a$b+c?q=a b&z=~-._"),
        "body": "{}",
        "headers": {"X-Sdk-Date": "20260304T050607Z",
                    "x-ot-session-id": "ses_deadbeefdeadbeef"},
        "want": ("SDK-HMAC-SHA256 Access=AKID"
                 ", SignedHeaders=host;x-ot-session-id;x-sdk-date;x-security-token"
                 ", Signature=177aeae3dc48dc4b2dec46b1754ee2e1ea3af8d3f76044c33fe74f74df5f7224"),
    },
    {
        "name": "PUT with empty body",
        "ak": "AKID",
        "sk": "SECRET",
        "sts": "",
        "method": "PUT",
        "url": "https://snap-access.cn-north-4.myhuaweicloud.com/v1/ops/claim",
        "body": "",
        "headers": {"X-Sdk-Date": "20260405T060708Z"},
        "want": ("SDK-HMAC-SHA256 Access=AKID, SignedHeaders=host;x-sdk-date"
                 ", Signature=1ee2960c0fad87a352395c32d2e4cb4f1fc2755cd92d09822dea0994700f3f79"),
    },
]

# canonicalURI 固化「逐段编码 + 强制补尾斜杠」。
URI_CASES = [
    ("https://h/api/v2/chat/completions", "/api/v2/chat/completions/"),
    ("https://h/", "/"),
    ("https://h", "/"),
    ("https://h/a/b/", "/a/b/"),
    ("https://h/a$b+c", "/a%24b%2Bc/"),
    ("https://h/模型 名称", "/%E6%A8%A1%E5%9E%8B%20%E5%90%8D%E7%A7%B0/"),
]

QUERY_CASES = [
    ("", ""),
    ("b=2&a=1&a=0", "a=0&a=1&b=2"),
    ("z=~-._", "z=~-._"),
    ("q=a b", "q=a%20b"),
]

ENCODE_CASES = [
    ("abcXYZ019-._~", "abcXYZ019-._~"),
    ("a b", "a%20b"),
    ("$&+,:;=@", "%24%26%2B%2C%3A%3B%3D%40"),
    ("模型", "%E6%A8%A1%E5%9E%8B"),
    ("a/b", "a%2Fb"),
    ("100%", "100%25"),
]
