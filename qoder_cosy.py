"""Qoder `cosy` 协议基础层 —— 纯 Python 1:1 移植。

来源：qoder2api/internal/cosy/*.go（signature.go / encoding.go / fingerprint.go /
session.go / auth.go）。全部为 MD5/SHA512/RSA/AES-CBC 等标准算法组合，
无任何不可移植的黑盒。

模块职责：
- 签名：SignLegacy（PAT/refresh token 换取 jobToken）、SignRequest（业务请求）
- 编码：Encode/Decode（Qoder 自定义字母表 base64 + 三分重排）
- 指纹：由账号 UID 稳定派生 machineid / machinetype / machinetoken
- 会话：RSA 加密临时密钥 + AES-CBC 加密身份载荷，组装 cosy Bearer

自测（含 Go 端测试向量）：
    python qoder_cosy.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding


# ---------------------------------------------------------------------------
# signature.go
# ---------------------------------------------------------------------------

APP_CODE = "cosy"
# base64("war, war never changes")
SECRET_B64 = "d2FyLCB3YXIgbmV2ZXIgY2hhbmdlcw=="
SEP = "&"


def current_date() -> str:
    """等价 Go: time.Now().UTC().Format("Mon, 02 Jan 2006 15:04:05 GMT")。"""
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def md5_hex(s: bytes) -> str:
    return hashlib.md5(s).hexdigest()


def sign_legacy(date: str) -> str:
    """等价 Go: Md5Hex(AppCode + sep + SecretB64 + sep + date)。"""
    return md5_hex((APP_CODE + SEP + SECRET_B64 + SEP + date).encode())


# ---------------------------------------------------------------------------
# encoding.go  —— 自定义字母表 base64 + 三分重排
# ---------------------------------------------------------------------------

_CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
_STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUSTOM_PAD = "$"

# Encode 用：std 字符 → custom 字符
_S2C: dict[int, int] = {}
# Decode 用：custom 字符 → std 字符
_C2S: dict[int, int] = {}
for _i in range(64):
    _S2C[ord(_STD_ALPHABET[_i])] = ord(_CUSTOM_ALPHABET[_i])
    _C2S[ord(_CUSTOM_ALPHABET[_i])] = ord(_STD_ALPHABET[_i])
_S2C[ord("=")] = ord(_CUSTOM_PAD)
_C2S[ord(_CUSTOM_PAD)] = ord("=")


def encode(plaintext: bytes) -> str:
    """等价 Go: Encode —— 标准 base64 后做三分重排，再映射到自定义字母表。

    重排规则（a = n/3）：rearranged = std[n-a:] + std[a:n-a] + std[:a]
    """
    std = base64.b64encode(plaintext).decode("ascii")
    n = len(std)
    a = n // 3
    rearranged = std[n - a:] + std[a:n - a] + std[:a]
    out = []
    for ch in rearranged:
        c = ord(ch)
        mapped = _S2C.get(c, -1)
        if mapped < 0:
            raise ValueError(f"char out of alphabet: {c}")
        out.append(chr(mapped))
    return "".join(out)


def decode(encoded: str) -> bytes:
    """等价 Go: Decode —— Encode 的逆过程。"""
    n = len(encoded)
    mapped = []
    for ch in encoded:
        c = ord(ch)
        std = _C2S.get(c, -1)
        if std < 0:
            raise ValueError(f"char out of custom alphabet: {c}")
        mapped.append(chr(std))
    a = n // 3
    std = "".join(mapped[n - a:]) + "".join(mapped[a:n - a]) + "".join(mapped[:a])
    return base64.b64decode(std)


# ---------------------------------------------------------------------------
# fingerprint.go —— 稳定设备指纹派生
# ---------------------------------------------------------------------------

_install_salt: str = ""


def set_install_salt(salt: str) -> None:
    """设置本机指纹盐（等价 Go: SetInstallSalt）。变更盐即整体指纹漂移。"""
    global _install_salt
    _install_salt = salt or ""


def _salted_seed(seed: str) -> str:
    """等价 Go: saltedSeed —— 盐为空时原样返回（hub 兼容）。"""
    if not _install_salt:
        return seed
    return seed + "|salt:" + _install_salt


def _derive_id(seed: str, salt: str) -> str:
    """等价 Go: deriveID —— md5(salt + ":" + saltedSeed(seed))。"""
    return md5_hex((salt + ":" + _salted_seed(seed)).encode())


def fingerprint_seed(uid: str, credential: str) -> str:
    """等价 Go: FingerprintSeed —— 优先账号 UID，未知时退回凭证种子。"""
    if uid:
        return uid
    return "cred:" + credential


def derive_machine_id(seed: str) -> str:
    """派生的 cosy-machineid（32 位十六进制）。"""
    return _derive_id(seed, "machine")


def derive_session_id(seed: str) -> str:
    """派生的稳定会话标识（32 位十六进制）。"""
    return _derive_id(seed, "session")


def derive_machine_type(seed: str) -> str:
    """派生的 18 位 machine type。"""
    return _derive_id(seed, "machinetype")[:18]


def derive_machine_token(seed: str) -> str:
    """派生的 machine token（sha512 → base64url 截 43 位）。"""
    digest = hashlib.sha512(("machinetoken:" + _salted_seed(seed)).encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")[:43]


# ---------------------------------------------------------------------------
# session.go —— 会话上下文 / 加密 / token 生成
# ---------------------------------------------------------------------------

VERSION = "1.0.10"

SERVER_PUB_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


@dataclass
class AuthIdentity:
    name: str = ""
    aid: str = ""
    uid: str = ""
    yx_uid: str = ""
    organization_id: str = ""
    organization_name: str = ""
    user_type: str = ""
    security_oauth_token: str = ""
    refresh_token: str = ""


@dataclass
class SessionContext:
    temp_key: bytes
    cosy_key: str
    info: str
    identity: AuthIdentity
    machine_id: str
    machine_token: str
    machine_type: str


def new_uuid() -> str:
    """等价 Go: NewUUID —— 8-4-4-4-12 十六进制。"""
    h = os.urandom(16).hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def new_request_id() -> str:
    """等价 Go: NewRequestID —— 24 位十六进制。"""
    return os.urandom(16).hex()[:24]


def new_base64_token() -> str:
    """等价 Go: NewBase64Token —— 两个 UUID 拼接截 50 位后 base64url。"""
    raw = (new_uuid() + new_uuid())[:50]
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def new_hex_token(n: int) -> str:
    """等价 Go: NewHexToken —— n 位十六进制。"""
    b = os.urandom((n + 1) // 2)
    return b.hex()[:n]


def unix_sec() -> int:
    return int(time.time())


def unix_ms() -> int:
    return int(time.time() * 1000)


def _rsa_encrypt(plaintext: bytes) -> bytes:
    """等价 Go: rsaEncrypt —— 固定服务端公钥 + PKCS1v15。"""
    pub = serialization.load_pem_public_key(SERVER_PUB_KEY_PEM.encode())
    return pub.encrypt(plaintext, asym_padding.PKCS1v15())


def _aes_encrypt(plain: bytes, key: bytes) -> bytes:
    """等价 Go: aesEncrypt —— AES-CBC + PKCS7，IV = key[:block]。"""
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _auth_payload_json(ident: AuthIdentity) -> bytes:
    m = {
        "name": ident.name,
        "aid": ident.aid,
        "uid": ident.uid,
        "yx_uid": ident.yx_uid,
        "organization_id": ident.organization_id,
        "organization_name": ident.organization_name,
        "user_type": ident.user_type,
        "security_oauth_token": ident.security_oauth_token,
        "refresh_token": ident.refresh_token,
    }
    return json.dumps(m, separators=(",", ":"), ensure_ascii=False).encode()


def new_session(ident: AuthIdentity, machine_id: str, machine_token: str,
                machine_type: str) -> SessionContext:
    """等价 Go: NewSession —— 生成随机临时密钥，RSA 加密成 cosyKey，
    AES 加密身份载荷成 info。"""
    temp_key = os.urandom(16).hex()[:16].encode()
    cosy_key = base64.b64encode(_rsa_encrypt(temp_key)).decode("ascii")
    info = base64.b64encode(
        _aes_encrypt(_auth_payload_json(ident), temp_key)
    ).decode("ascii")
    return SessionContext(
        temp_key=temp_key,
        cosy_key=cosy_key,
        info=info,
        identity=ident,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )


def build_payload_b64(info: str) -> str:
    """等价 Go: BuildPayloadB64。"""
    m = {
        "cosyVersion": VERSION,
        "ideVersion": "",
        "info": info,
        "requestId": new_uuid(),
        "version": "v1",
    }
    data = json.dumps(m, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.b64encode(data).decode("ascii")


def sign_request(payload_b64: str, cosy_key: str, cosy_date: str,
                 body: str, path_without_algo: str) -> str:
    """等价 Go: SignRequest。"""
    s = "\n".join([payload_b64, cosy_key, cosy_date, body, path_without_algo])
    return md5_hex(s.encode())


def compose_bearer(payload_b64: str, sig: str) -> str:
    """等价 Go: ComposeBearer。"""
    return "Bearer COSY." + payload_b64 + "." + sig


# ---------------------------------------------------------------------------
# auth.go —— PAT / refresh token 换取 jobToken
# ---------------------------------------------------------------------------

DEFAULT_JOB_TOKEN_URL = "https://center.qoder.sh/algo/api/v3/user/jobToken?Encode=1"


def exchange_job_token(token: str, machine_id: str, machine_token: str,
                       machine_type: str, job_token_url: str = "") -> dict:
    """等价 Go: ExchangeJobToken —— 用 cosy 签名把 PAT/refresh 换成 jobToken。

    token 以 "drt-" 开头时视为 refreshToken，否则视为 personalToken。
    """
    import urllib.error
    import urllib.request

    date = current_date()
    sig = sign_legacy(date)

    if token.startswith("drt-"):
        personal_token, refresh_token = "", token
    else:
        personal_token, refresh_token = token, ""

    inner = {
        "personalToken": personal_token,
        "securityOauthToken": "",
        "refreshToken": refresh_token,
        "needRefresh": refresh_token != "",
        "authInfo": {},
    }
    outer = {
        "payload": json.dumps(inner, separators=(",", ":"), ensure_ascii=False),
        "encodeVersion": "1",
    }
    body = encode(json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode())

    endpoint = job_token_url or DEFAULT_JOB_TOKEN_URL
    headers = {
        "cosy-machinetoken": machine_token,
        "cosy-machinetype": machine_type,
        "login-version": "v2",
        "appcode": APP_CODE,
        "accept": "application/json",
        "accept-encoding": "identity",
        "cosy-version": VERSION,
        "cosy-clienttype": "5",
        "date": date,
        "signature": sig,
        "content-type": "application/json",
        "cosy-machineid": machine_id,
        "user-agent": "Go-http-client/2.0",
    }

    req = urllib.request.Request(
        endpoint, data=body.encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace") if e.fp else str(e)
        raise RuntimeError(f"HTTP {e.code} at jobToken body={detail}") from e
    return json.loads(data.decode())


# ---------------------------------------------------------------------------
# 自测：含 Go 端测试向量（internal/cosy/fingerprint_test.go）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    failures = []

    # 1) 指纹测试向量（无盐，seed=test-uid-123）
    set_install_salt("")
    vectors = {
        "machine": ("005b8945c0659064f8d25299980a27b3", derive_machine_id("test-uid-123")),
        "session": ("e1527624132e0a39ebcb328440517365", derive_session_id("test-uid-123")),
        "mtype":   ("66ea01f7983702b088", derive_machine_type("test-uid-123")),
        "mtoken":  ("zzpUYGGMSPEfJVrGQWHj7SBYaRUMwPMK0B4QN_aqKP0", derive_machine_token("test-uid-123")),
    }
    for name, (want, got) in vectors.items():
        status = "OK" if want == got else "FAIL"
        if want != got:
            failures.append(name)
        print(f"[{status}] derive_{name}: {got}")

    # 2) 指纹稳定性/隔离/格式
    assert derive_machine_id("acct-a") != derive_machine_id("acct-b"), "指纹跨账号冲突"
    assert len(derive_machine_id("x")) == 32
    assert len(derive_machine_type("x")) == 18
    assert len(derive_machine_token("x")) == 43
    assert fingerprint_seed("", "pt-secret") == "cred:pt-secret"
    print("[OK] 指纹稳定性/隔离/格式")

    # 3) 编码往返 + 重排验证
    for sample in (b"", b"a", b"ab", b"abc", b"hello world", os.urandom(97)):
        assert decode(encode(sample)) == sample, f"encode/decode 往返失败: {sample!r}"
    print("[OK] Encode/Decode 往返")

    # 4) 签名确定性
    assert sign_legacy("Mon, 02 Jan 2006 15:04:05 GMT").__len__() == 32
    sr1 = sign_request("PB", "KEY", "1700000000", "body", "/api/v2/model/list")
    assert sr1 == sign_request("PB", "KEY", "1700000000", "body", "/api/v2/model/list")
    assert sr1 != sign_request("PB", "KEY", "1700000001", "body", "/api/v2/model/list")
    print("[OK] 签名确定性")

    # 5) 会话/加密可构造
    sess = new_session(
        AuthIdentity(uid="u1", name="n", refresh_token="rt"),
        "mid", "mtok", "mtype",
    )
    assert len(sess.cosy_key) > 0 and len(sess.info) > 0
    assert len(sess.temp_key) == 16
    print("[OK] 会话/加密构造")

    # 6) token 生成格式
    assert len(new_uuid()) == 36
    assert len(new_request_id()) == 24
    assert len(new_hex_token(32)) == 32
    print("[OK] token 生成")

    if failures:
        raise SystemExit(f"自测失败: {failures}")
    print("\n全部通过 ✅")