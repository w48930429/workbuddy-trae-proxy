"""Trae vault 加密存储 - 移植自 TraeWorkAssistant vault.rs

使用 Windows DPAPI 加密存储 Trae token，明文不落盘。
"""

import base64
import json
import os
from typing import Optional, Dict

try:
    import win32crypt
    HAS_DAPI = True
except ImportError:
    HAS_DAPI = False

# Vault 存储路径
VAULT_DIR = os.path.join(os.path.dirname(__file__), "vault")
VAULT_FILE = os.path.join(VAULT_DIR, "trae_secrets.json")


def ensure_vault_dir():
    """确保 vault 目录存在"""
    os.makedirs(VAULT_DIR, exist_ok=True)


def encrypt_secret(data: str) -> bytes:
    """使用 DPAPI 加密数据"""
    if HAS_DAPI:
        try:
            _, encrypted = win32crypt.CryptProtectData(
                data.encode("utf-8"),
                "trae_vault",
                None,
                None,
                None,
                0
            )
            return encrypted
        except Exception:
            pass
    
    # Fallback: 简单 base64 编码（不安全，仅作兼容）
    return base64.b64encode(data.encode("utf-8"))


def decrypt_secret(encrypted: bytes) -> str:
    """使用 DPAPI 解密数据"""
    if HAS_DAPI:
        try:
            _, decrypted = win32crypt.CryptUnprotectData(
                encrypted,
                None,
                None,
                None,
                0
            )
            return decrypted.decode("utf-8")
        except Exception:
            pass
    
    # Fallback
    return base64.b64decode(encrypted).decode("utf-8")


def save_trae_secrets(secrets_dict: Dict[str, dict]) -> bool:
    """保存 Trae 账号到加密 vault
    
    Args:
        secrets_dict: {user_id: {"token": "...", "refresh_token": "...", "expires_at": ...}}
    
    Returns:
        是否成功
    """
    ensure_vault_dir()
    
    # 加密每个账号的敏感字段
    encrypted = {}
    for uid, data in secrets_dict.items():
        entry = {
            "token": encrypt_secret(data.get("token", "")).hex() if data.get("token") else "",
            "refresh_token": encrypt_secret(data.get("refresh_token", "")).hex() if data.get("refresh_token") else "",
            "user_name": data.get("user_name", ""),
            "avatar": data.get("avatar", ""),
            "expires_at": data.get("expires_at", 0),
        }
        encrypted[uid] = entry
    
    try:
        # 写临时文件，然后原子重命名
        tmp_path = VAULT_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(encrypted, f, indent=2)
        
        os.replace(tmp_path, VAULT_FILE)
        return True
    except Exception as e:
        print(f"Vault save failed: {e}")
        return False


def load_trae_secrets() -> Dict[str, dict]:
    """从 vault 加载 Trae 账号
    
    Returns:
        {user_id: {"token": "...", "refresh_token": "...", ...}}
    """
    if not os.path.exists(VAULT_FILE):
        return {}
    
    try:
        with open(VAULT_FILE, "r", encoding="utf-8") as f:
            encrypted = json.load(f)
    except Exception:
        return {}
    
    # 解密敏感字段
    decrypted = {}
    for uid, data in encrypted.items():
        entry = {
            "token": decrypt_secret(bytes.fromhex(data["token"])) if data.get("token") else "",
            "refresh_token": decrypt_secret(bytes.fromhex(data["refresh_token"])) if data.get("refresh_token") else "",
            "user_name": data.get("user_name", ""),
            "avatar": data.get("avatar", ""),
            "expires_at": data.get("expires_at", 0),
        }
        decrypted[uid] = entry
    
    return decrypted


def delete_trae_secrets() -> bool:
    """删除所有 Trae 账号"""
    try:
        if os.path.exists(VAULT_FILE):
            os.remove(VAULT_FILE)
        return True
    except Exception:
        return False