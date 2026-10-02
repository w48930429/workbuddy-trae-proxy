# icube 设备凭证提取（移植自 TraeWorkAssistant src-tauri/src/icube_auth.rs）。
#
# Trae CN/SOLO 客户端把 OAuth 设备的 EC P-256 密钥对存于
# `%APPDATA%/<app>/User/globalStorage/storage.json` 的 `iCubeAuthInfo://icube-dc:<deviceId>`
# 键，值为「tc」信封加密（byteCrypto.js 逆向，BlueChonk 交叉验证）：pepper 是随
# 安装包分发的公开常量表（混淆非加密）。私钥只在内存流转，不落盘不进日志。
#
# DeviceInfo.ClientVersion 用安装目录 package.json 的 version。

import base64
import hashlib
import json
import os
from typing import Dict, List, Optional

# byteCrypto 四常量表（Trae CN resources/app/out/main.js 2026-09-16 实测提取；
# Woe^Voe=AES 模式 pepper / joe^Hoe=AES_PRIVATE 模式 pepper）
WOE_T = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251, 124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
VOE_T = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])
JOE_T = bytes([191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176, 135, 99, 96, 18, 127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156, 51, 229, 121, 35, 17, 153, 141, 177, 110, 131, 150, 128, 172, 255, 254, 6, 18, 140, 55, 62, 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209])
HOE_T = bytes([246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145, 50, 196, 165, 42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106, 175, 148, 31, 204, 186, 186, 165, 182, 87, 142, 49, 10, 39, 110, 26, 154, 86, 56, 173, 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170])

# tc 信封常量：magic [116,99,5,16,0,0]，random 32B，header 6B，SHA512 tag 64B
TC_HEADER = bytes([116, 99, 5, 16, 0, 0])
TC_RANDOM_LEN = 32
TC_HEADER_LEN = 6
TC_SHA512_LEN = 64

_APPS = ["Trae CN", "TRAE SOLO CN", "Trae", "Trae Work"]


class DeviceCredential:
    __slots__ = ("device_id", "private_key_pem", "machine_id", "app_version", "source_app")

    def __init__(self, device_id: str, private_key_pem: str, machine_id: str, app_version: str, source_app: str):
        self.device_id = device_id
        self.private_key_pem = private_key_pem
        self.machine_id = machine_id
        self.app_version = app_version
        self.source_app = source_app


def _sha512(buf: bytes) -> bytes:
    return hashlib.sha512(buf).digest()


def _derive_keys(random: bytes, pepper: bytes):
    """byteCrypto deriveKeys：SHA512(random) || pepper → SHA512 → 前 32B 切 aesKey/iv。"""
    o = _sha512(random)
    n = o + pepper
    c = _sha512(n)
    return c[:16], c[16:32]


def tc_decrypt(b64_text: str, private_mode: bool = False) -> str:
    """tc 信封解密（AES-128-CBC + PKCS7，SHA512 完整性 tag）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    raw = base64.b64decode(b64_text.strip())
    if len(raw) < TC_HEADER_LEN + TC_RANDOM_LEN + 16:
        raise ValueError(f"tc 信封长度异常: {len(raw)}")
    if raw[:TC_HEADER_LEN] != TC_HEADER:
        raise ValueError("tc 信封 magic 不匹配")
    random = raw[TC_HEADER_LEN:TC_HEADER_LEN + TC_RANDOM_LEN]
    cipher_bytes = raw[TC_HEADER_LEN + TC_RANDOM_LEN:]
    if private_mode:
        pepper = bytes(a ^ b for a, b in zip(JOE_T, HOE_T))
    else:
        pepper = bytes(a ^ b for a, b in zip(WOE_T, VOE_T))
    key, iv = _derive_keys(random, pepper)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(cipher_bytes) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    if len(plain) <= TC_SHA512_LEN:
        raise ValueError("tc 解密后明文过短")
    tag, body = plain[:TC_SHA512_LEN], plain[TC_SHA512_LEN:]
    if _sha512(body) != tag:
        raise ValueError("tc 信封完整性校验失败（SHA512 不匹配）")
    return body.decode("utf-8", errors="strict")


def _app_version_for(app: str) -> str:
    """Trae 安装目录可能不在默认位置（如 E:/ProgramFiles/Trae CN），
    依次探测 LOCALAPPDATA/Programs 与若干常见根目录下的 resources/app/package.json。"""
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Programs", app, "resources", "app", "package.json"))
    # 按盘符探测常见安装根（与 %LOCALAPPDATA% 平级的自定义安装位）
    for drive in ("C:", "D:", "E:", "F:"):
        candidates.append(os.path.join(drive + "\\", "ProgramFiles", app, "resources", "app", "package.json"))
        candidates.append(os.path.join(drive + "\\", "Program Files", app, "resources", "app", "package.json"))
    for p in candidates:
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f).get("version", "") or ""
        except Exception:
            continue
    return ""


def extract_device_credentials() -> List[DeviceCredential]:
    """扫描本机各 Trae 客户端 storage.json 提取设备凭证。找不到/解密失败返回空表。"""
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if not appdata:
        return []
    out: List[DeviceCredential] = []
    for app in _APPS:
        path = os.path.join(appdata, app, "User", "globalStorage", "storage.json")
        try:
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue
        machine_id = str(obj.get("telemetry.machineId", ""))
        app_version = _app_version_for(app)
        for key, val in obj.items():
            device_id = key[len("iCubeAuthInfo://icube-dc:"):] if key.startswith("iCubeAuthInfo://icube-dc:") else None
            if not device_id or not isinstance(val, str):
                continue
            try:
                plain = tc_decrypt(val, private_mode=False)
                parsed = json.loads(plain)
            except Exception:
                continue
            pem = parsed.get("privateKeyPEM")
            if isinstance(pem, str) and "BEGIN" in pem:
                out.append(DeviceCredential(device_id, pem, machine_id, app_version, app))
    return out


_CRED_CACHE: Optional[List[DeviceCredential]] = None


def get_device_credentials() -> List[DeviceCredential]:
    """进程内缓存版 extract_device_credentials。"""
    global _CRED_CACHE
    if _CRED_CACHE is None:
        _CRED_CACHE = extract_device_credentials()
    return _CRED_CACHE


def device_public_key_pem(cred: DeviceCredential) -> str:
    """设备私钥 → SPKI PEM 公钥（DeviceInfo.DevicePublicKey 用）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    signing = serialization.load_pem_private_key(cred.private_key_pem.encode(), password=None)
    if not isinstance(signing, ec.EllipticCurvePrivateKey):
        raise ValueError("设备私钥不是 EC 密钥")
    pub = signing.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return pub.decode("ascii")
