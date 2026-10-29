"""华为云 SDK-HMAC-SHA256 签名器 —— 纯 Python 复刻。

来源：`@huaweicloud/huaweicloud-sdk-core` 的 JS signer（随 CodeArts Agent
扩展打包），协议事实由 2951461586/CodeArts2API 的 `internal/codearts/signer.go`
与 `docs/UPSTREAM.md` 记录。本文件不引入任何 Go/Rust 依赖，也不起子进程。

与官方文档不同、必须一起复刻的两处行为（漏一处上游必然回 `APIG.0301`）：

  1. CanonicalURI **逐段编码后强制补尾斜杠**
     `/api/v2/chat/completions` → `/api/v2/chat/completions/`
  2. CanonicalHeaders 每行自带尾 ``\\n``，拼装 CanonicalRequest 时又补一个
     → ``SignedHeaders`` 之前多出一个空行

另有三条容易被「顺手修干净」却会导致签名失配的细节：

  - URL 编码只保留 ``A-Za-z0-9-._~``；``$&+,:;=@`` 等 Go ``url.PathEscape``
    会放过的字符必须编码，且 ``%XX`` 用大写十六进制。
  - 只对 header **值** 做 trim，不对 header **名** 做 trim。
  - 非 ``PUT``/``PATCH``/``POST`` 的 body 不参与签名（按空串哈希）。

自测：``python codearts/codearts_signer.py``
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import urllib.parse

SDK_SIGNING_ALGORITHM = "SDK-HMAC-SHA256"
SDK_DATE_FORMAT = "%Y%m%dT%H%M%SZ"

# RFC 3986 unreserved —— 只有这些字符不被百分号编码。
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

# 只有这三个方法的 body 参与签名。HEAD/GET/DELETE 即使带了 body 也按空串算。
_SIGNED_METHODS = frozenset(("PUT", "PATCH", "POST"))


def js_url_encode(text):
    """按 JS signer 的规则编码：unreserved 之外逐字节 ``%XX``（大写）。"""
    out = []
    for ch in text:
        if ch in _UNRESERVED:
            out.append(ch)
        else:
            for byte in ch.encode("utf-8"):
                out.append("%%%02X" % byte)
    return "".join(out)


def canonical_uri(url_parts):
    """逐段编码后补尾斜杠（怪癖 1）。"""
    path = url_parts.path or "/"
    # urllib 解析出的 path 已是转义态，先还原成原文再按 JS 规则重编码，
    # 否则会把 %20 当成字面量再编码成 %2520。
    segments = [js_url_encode(urllib.parse.unquote(seg)) for seg in path.split("/")]
    uri = "/".join(segments)
    if not uri.endswith("/"):
        uri += "/"
    return uri


def canonical_query(url_parts):
    """键、值各自排序后再编码；空查询返回空串（不是 ``?``）。"""
    if not url_parts.query:
        return ""
    pairs = urllib.parse.parse_qsl(url_parts.query, keep_blank_values=True)
    encoded = sorted((js_url_encode(k), js_url_encode(v)) for k, v in pairs)
    return "&".join("%s=%s" % (k, v) for k, v in encoded)


def sha256_hex(payload):
    return hashlib.sha256(payload).hexdigest()


def sign_request(access_key, secret_key, security_token, method, url, body=b"",
                 headers=None, now=None):
    """生成可直接作为 HTTP 头使用的签名 map。

    返回值里所有键都是小写；``Authorization`` 是完整头值。调用方传入的
    ``headers`` 不会被就地修改（复制后处理）。
    """
    url_parts = urllib.parse.urlsplit(url)
    out = {}
    for key, value in (headers or {}).items():
        out[key.lower()] = value
    out["host"] = url_parts.netloc
    if "x-sdk-date" not in out:
        stamp = now or datetime.datetime.now(datetime.timezone.utc)
        out["x-sdk-date"] = stamp.strftime(SDK_DATE_FORMAT)
    if security_token:
        out["x-security-token"] = security_token

    if isinstance(body, str):
        body = body.encode("utf-8")
    payload = body if method.upper() in _SIGNED_METHODS else b""

    names = sorted(out)
    # 怪癖 2：每行自带尾 \n，下面 join 再补一个 —— 于是多出空行。
    canonical_headers = "".join(
        "%s:%s\n" % (name, out[name].strip()) for name in names)
    signed_headers = ";".join(names)

    canonical_request = "\n".join([
        method.upper(),
        canonical_uri(url_parts),
        canonical_query(url_parts),
        canonical_headers,
        signed_headers,
        sha256_hex(payload),
    ])
    string_to_sign = "\n".join([
        SDK_SIGNING_ALGORITHM,
        out["x-sdk-date"],
        sha256_hex(canonical_request.encode("utf-8")),
    ])
    signature = hmac.new(secret_key.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    out["Authorization"] = "%s Access=%s, SignedHeaders=%s, Signature=%s" % (
        SDK_SIGNING_ALGORITHM, access_key, signed_headers, signature)
    return out
