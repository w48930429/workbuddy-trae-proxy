"""CodeArts 多账号管理与出口代理隔离池 (codearts_accounts.py).

功能：
1. 管理 accounts/codearts_accounts.json 多账号池。
2. 仿照 WorkBuddy 的 proxy_slot 机制，将每个账号绑定至 settings.json 中的代理槽位，
   实现出口 IP 强隔离（每个账号走各自独立的出口代理），防止 IP 聚集风控。
3. 提供并发控制（单账号限制最大并发度 3）与轮询选号调度。
4. 提供账号级风控熔断（restrictedUntil）与频控冷却（cooldownUntil）。
5. 自动与已有的 accounts/codearts_account.json 单账号兼容与同步。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from codearts.codearts_session import CodeArtsSession

MAX_CONCURRENCY_PER_ACCOUNT = 3
DEFAULT_RESTRICTED_COOLDOWN = 7200.0  # 2 小时风控冷却
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0    # 1 分钟频控冷却


class CodeArtsAccount:
    """单个华为云 CodeArts 账号对象。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}
        acct_meta = data.get("account") if isinstance(data.get("account"), dict) else {}
        self.id = str(
            data.get("id")
            or acct_meta.get("id")
            or data.get("domainId")
            or data.get("domain_id")
            or uuid.uuid4().hex[:12]
        ).strip()
        self.label = str(
            data.get("label")
            or acct_meta.get("label")
            or data.get("account_name")
            or self.id
        ).strip()

        # 凭据字段
        self.access_key = str(data.get("accessKey") or data.get("access_key") or "").strip()
        self.secret_key = str(data.get("secretKey") or data.get("secret_key") or "").strip()
        self.security_token = str(
            data.get("securityToken")
            or data.get("securitytoken")
            or data.get("security_token")
            or ""
        ).strip()
        self.expires_at = str(data.get("expiresAt") or data.get("expires_at") or "").strip()
        self.refresh_token = str(
            data.get("refreshToken")
            or data.get("refresh_token")
            or ""
        ).strip()
        self.domain_id = str(data.get("domainId") or data.get("domain_id") or "").strip()
        self.safely_renew_token_interval = int(data.get("safelyRenewTokenInterval") or 3600)
        self.login_context = (
            data.get("loginContext")
            if isinstance(data.get("loginContext"), dict)
            else (data.get("login_context") if isinstance(data.get("login_context"), dict) else {})
        )
        self.source = str(data.get("source") or "web_oauth")

        # 代理槽位绑定（仿照 WorkBuddy 设计）
        self.proxy_slot = str(data.get("proxySlot") or data.get("proxy_slot") or "").strip()
        self.proxy_legacy = str(data.get("proxy") or "").strip()
        # 运行时解析得到的出口代理 URL（由 AccountPool.apply_proxy_slots 解析）
        self.proxy = self.proxy_legacy

        self.enabled = data.get("enabled", True) is not False
        self.last_error = str(data.get("lastError") or data.get("last_error") or "")
        self.last_used = float(data.get("lastUsed") or data.get("last_used") or 0.0)

        # 冷却与风控机制（与 WorkBuddy 完全一致）
        self.cooldown_until = float(data.get("cooldownUntil") or data.get("cooldown_until") or 0.0)
        self.restricted_until = float(data.get("restrictedUntil") or data.get("restricted_until") or 0.0)
        self.restricted_detail = str(
            data.get("restrictedDetail") or data.get("restricted_detail") or ""
        )[:2000]

        # 运行时并发度计数器（华为云 CodeArts 单 Session 并发上限为 3）
        self.in_flight = 0

    @property
    def has_sts(self) -> bool:
        return bool(self.security_token)

    def is_cooling(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return self.cooldown_until > t

    def is_restricted(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return self.restricted_until > t

    def is_expired(self, now_dt: Optional[datetime.datetime] = None) -> bool:
        if not self.expires_at:
            return False
        try:
            stamp = datetime.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        current = now_dt or datetime.datetime.now(datetime.timezone.utc)
        return stamp <= current

    def is_available(self, now: Optional[float] = None) -> bool:
        """检查账号当前是否可用于调度。"""
        if not self.enabled:
            return False
        if not self.access_key or not self.secret_key:
            return False
        t = now if now is not None else time.time()
        if self.is_cooling(t) or self.is_restricted(t):
            return False
        if self.in_flight >= MAX_CONCURRENCY_PER_ACCOUNT:
            return False
        return True

    @contextlib.contextmanager
    def slot(self):
        """并发槽位上下文管理器。保证请求完成后计数器正确释放。"""
        self.in_flight += 1
        self.last_used = time.time()
        try:
            yield self
        finally:
            self.in_flight = max(0, self.in_flight - 1)

    def to_session(self) -> CodeArtsSession:
        """转为 codearts_signer / codearts_client 所需的 CodeArtsSession。"""
        return CodeArtsSession(
            access_key=self.access_key,
            secret_key=self.secret_key,
            security_token=self.security_token,
            expires_at=self.expires_at,
            refresh_token=self.refresh_token,
            domain_id=self.domain_id,
            account_label=self.label,
            source=self.source,
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化供持久化到 codearts_accounts.json。"""
        return {
            "id": self.id,
            "label": self.label,
            "accessKey": self.access_key,
            "secretKey": self.secret_key,
            "securityToken": self.security_token,
            "expiresAt": self.expires_at,
            "refreshToken": self.refresh_token,
            "domainId": self.domain_id,
            "safelyRenewTokenInterval": self.safely_renew_token_interval,
            "loginContext": self.login_context,
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy_legacy,
            "enabled": self.enabled,
            "source": self.source,
            "cooldownUntil": self.cooldown_until,
            "restrictedUntil": self.restricted_until,
            "restrictedDetail": self.restricted_detail,
            "lastUsed": self.last_used,
            "lastError": self.last_error,
        }

    def masked(self) -> Dict[str, Any]:
        """脱敏视图，供看板及管理 API 展示。"""
        now = time.time()
        cooling = self.is_cooling(now)
        restricted = self.is_restricted(now)
        expired = self.is_expired()

        if restricted:
            status = "restricted"
        elif cooling:
            status = "cooling"
        elif not self.enabled:
            status = "disabled"
        elif self.in_flight >= MAX_CONCURRENCY_PER_ACCOUNT:
            status = "busy"
        elif expired and not self.refresh_token:
            status = "expired"
        else:
            status = "ready"

        ak = self.access_key
        ak_masked = (ak[:6] + "…" + ak[-4:]) if len(ak) > 12 else ("…" if ak else "")

        return {
            "id": self.id,
            "label": self.label,
            "accessKey": ak_masked,
            "hasSecretKey": bool(self.secret_key),
            "hasSts": self.has_sts,
            "expiresAt": self.expires_at,
            "domainId": (self.domain_id[:8] + "…") if self.domain_id else "",
            "source": self.source,
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy,
            "enabled": self.enabled,
            "inFlight": self.in_flight,
            "maxConcurrency": MAX_CONCURRENCY_PER_ACCOUNT,
            "isCooling": cooling,
            "isRestricted": restricted,
            "isExpired": expired,
            "status": status,
            "cooldownRemaining": max(0, int(self.cooldown_until - now)) if cooling else 0,
            "restrictedRemaining": max(0, int(self.restricted_until - now)) if restricted else 0,
            "restrictedDetail": self.restricted_detail,
            "lastUsed": self.last_used,
            "lastError": self.last_error,
        }

    def save(self, *args, **kwargs) -> None:
        """兼容 wb_settings.drop_missing_bindings 调用签名。"""
        pass


class CodeArtsAccountPool:
    """华为云 CodeArts 多账号管理与代理分流池。"""

    def __init__(self, accounts_dir: str):
        self.accounts_dir = os.path.abspath(accounts_dir)
        self.file_path = os.path.join(self.accounts_dir, "codearts_accounts.json")
        self.legacy_single_file = os.path.join(self.accounts_dir, "codearts_account.json")
        self.accounts: List[CodeArtsAccount] = []
        self._lock = threading.Lock()
        self._cursor = 0
        self.load()

    @property
    def dir(self) -> str:
        return self.accounts_dir

    def load(self) -> None:
        """加载账号池。如果多账号文件不存在，尝试从单账号文件导入初始账号。"""
        with self._lock:
            loaded_accounts: List[CodeArtsAccount] = []
            if os.path.isfile(self.file_path):
                try:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, list):
                        for item in raw:
                            if isinstance(item, dict):
                                loaded_accounts.append(CodeArtsAccount(item))
                    elif isinstance(raw, dict):
                        loaded_accounts.append(CodeArtsAccount(raw))
                except Exception as exc:
                    print(f"[CodeArtsPool] Failed to parse {self.file_path}: {exc}")

            # 兼容迁移：若 codearts_accounts.json 没有任何账号，但单账号文件存在，则自动导入
            if not loaded_accounts and os.path.isfile(self.legacy_single_file):
                try:
                    with open(self.legacy_single_file, "r", encoding="utf-8") as f:
                        single_data = json.load(f)
                    if isinstance(single_data, dict) and (
                        single_data.get("accessKey") or single_data.get("access_key")
                    ):
                        acct = CodeArtsAccount(single_data)
                        loaded_accounts.append(acct)
                        print(f"[CodeArtsPool] Auto-migrated single account to pool: {acct.label}")
                except Exception as exc:
                    print(f"[CodeArtsPool] Failed to auto-migrate single account: {exc}")

            self.accounts = loaded_accounts

        self.apply_proxy_slots()
        # 若刚才发生了单账号自动迁移，顺便落盘一次 codearts_accounts.json
        if self.accounts and not os.path.isfile(self.file_path):
            self.save()

    def save(self) -> None:
        """原子持久化 accounts/codearts_accounts.json。"""
        os.makedirs(self.accounts_dir, exist_ok=True)
        with self._lock:
            data = [acct.to_dict() for acct in self.accounts]
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.file_path)

    def apply_proxy_slots(self, slots: Optional[List[Dict[str, Any]]] = None) -> None:
        """根据 settings.json 中的 proxy_slots 重新解析每个账号运行时的 proxy。

        proxy_slot 是持久化存储的槽位 id（如 slot-1）；
        proxy 是解析后的出口代理地址（如 http://127.0.0.1:7890）。
        槽位不存在或被禁用时，回退到 proxy_legacy 或直连（空字符串）。
        """
        if slots is None:
            try:
                import wb_settings
                slots = wb_settings.proxy_slots(self.accounts_dir)
            except Exception:
                slots = []

        by_id = {
            entry["id"]: entry for entry in slots if isinstance(entry, dict) and entry.get("enabled", True)
        }
        with self._lock:
            for account in self.accounts:
                slot = by_id.get(account.proxy_slot)
                if slot:
                    account.proxy = slot["url"]
                else:
                    account.proxy = account.proxy_legacy

    def get(self, account_id: str) -> Optional[CodeArtsAccount]:
        with self._lock:
            for a in self.accounts:
                if a.id == account_id:
                    return a
        return None

    def add(self, account: CodeArtsAccount) -> CodeArtsAccount:
        """添加或替换账号并落盘。"""
        with self._lock:
            existing = None
            for idx, a in enumerate(self.accounts):
                if a.id == account.id:
                    existing = idx
                    break
            if existing is not None:
                self.accounts[existing] = account
            else:
                self.accounts.append(account)
        self.apply_proxy_slots()
        self.save()
        return account

    def remove(self, account_id: str) -> bool:
        """删除指定账号并落盘。"""
        with self._lock:
            target = None
            for a in self.accounts:
                if a.id == account_id:
                    target = a
                    break
            if not target:
                return False
            self.accounts.remove(target)
        self.save()
        return True

    def set_enabled(self, account_id: str, enabled: bool) -> bool:
        with self._lock:
            acct = None
            for a in self.accounts:
                if a.id == account_id:
                    acct = a
                    break
            if not acct:
                return False
            acct.enabled = enabled
        self.save()
        return True

    def set_proxy_slot(self, account_id: str, slot_id: str) -> bool:
        """将指定账号绑定到特定 Proxy Slot。"""
        with self._lock:
            acct = None
            for a in self.accounts:
                if a.id == account_id:
                    acct = a
                    break
            if not acct:
                return False
            acct.proxy_slot = str(slot_id or "").strip()
        self.apply_proxy_slots()
        self.save()
        return True

    def mark_cooldown(self, account_id: str, duration: float = DEFAULT_RATE_LIMIT_COOLDOWN) -> None:
        """对账号实施临时频控冷却（如触发 429）。"""
        with self._lock:
            acct = self.get(account_id)
            if acct:
                acct.cooldown_until = time.time() + duration

    def mark_restricted(
        self,
        account_id: str,
        duration: float = DEFAULT_RESTRICTED_COOLDOWN,
        detail: str = "",
    ) -> None:
        """对账号实施风控限制隔离（如触发 403 / 11140），并持久化到文件。"""
        with self._lock:
            acct = self.get(account_id)
            if acct:
                acct.restricted_until = time.time() + duration
                acct.restricted_detail = str(detail or "Upstream account restricted")[:2000]
        self.save()

    def pick_account(self) -> Tuple[Optional[CodeArtsAccount], Optional[CodeArtsSession]]:
        """从可用账号中选取最合适的一个（最小在飞并发 + 轮询）。

        若选中的账号 STS 凭据已过期或濒临过期（剩余 < 300s），且具备 refreshToken 与 loginContext，
        则会自动走该账号专属的 proxy 执行 STS 刷新。
        """
        now = time.time()
        with self._lock:
            available = [a for a in self.accounts if a.is_available(now)]
            if not available:
                return None, None

            # 优先选择当前在飞请求数 (in_flight) 最少的账号，若相同则轮询
            available.sort(key=lambda a: (a.in_flight, (self._cursor - self.accounts.index(a)) % len(self.accounts)))
            picked = available[0]
            self._cursor = (self.accounts.index(picked) + 1) % len(self.accounts)

        # 检查是否需要自动刷新 STS 凭据
        if picked.has_sts and picked.refresh_token and picked.login_context:
            needs_refresh = False
            if picked.is_expired():
                needs_refresh = True
            elif picked.expires_at:
                try:
                    stamp = datetime.datetime.fromisoformat(picked.expires_at.replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                    remaining = (stamp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                    if remaining < 300:
                        needs_refresh = True
                except Exception:
                    pass

            if needs_refresh:
                try:
                    from codearts import codearts_oauth
                    # 关键：走该账号自身的出口代理进行刷新，保证 IP 隔离一致性
                    codearts_oauth.refresh_sts_token_for_account(picked, proxy=picked.proxy)
                    self.save()
                except Exception as exc:
                    print(f"[CodeArtsPool] Failed auto-refreshing STS for account {picked.label}: {exc}")

        return picked, picked.to_session()

    def public_view(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [a.masked() for a in self.accounts]
