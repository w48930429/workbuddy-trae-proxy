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
# STS 续期被上游拒绝（refresh token 已被使用/失效）属于持久故障，重试无法自愈；
# 隔离 5 分钟，避免每个请求都去打一次 STS 把失败伪装成「并发满载」。
CREDENTIAL_ERROR_COOLDOWN = 300.0


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
        # 站点：cn 国内 / intl 海外（ap-southeast-1）。聊天与积分主机按它分流，
        # 官方扩展也是这么按 scenario 切 snapEngineApiHost 的。
        self.region = str(data.get("region") or "cn").strip().lower() or "cn"

        # 代理槽位绑定（仿照 WorkBuddy 设计）
        self.proxy_slot = str(data.get("proxySlot") or data.get("proxy_slot") or "").strip()
        self.proxy_legacy = str(data.get("proxy") or "").strip()
        # 运行时解析得到的出口代理 URL（由 AccountPool.apply_proxy_slots 解析）
        self.proxy = self.proxy_legacy

        self.enabled = data.get("enabled", True) is not False
        self.last_error = str(data.get("lastError") or data.get("last_error") or "")
        self.last_used = float(data.get("lastUsed") or data.get("last_used") or 0.0)

        self.last_checkin_time = str(data.get("lastCheckinTime") or data.get("last_checkin_time") or "")
        self.last_checkin_result = (
            data.get("lastCheckinResult")
            if isinstance(data.get("lastCheckinResult"), dict)
            else (data.get("last_checkin_result") if isinstance(data.get("last_checkin_result"), dict) else {})
        )
        # 冷却与风控机制（与 WorkBuddy 完全一致）
        self.cooldown_until = float(data.get("cooldownUntil") or data.get("cooldown_until") or 0.0)
        self.restricted_until = float(data.get("restrictedUntil") or data.get("restricted_until") or 0.0)
        self.restricted_detail = str(
            data.get("restrictedDetail") or data.get("restricted_detail") or ""
        )[:2000]

        # 积分余额与套餐配额缓存
        self.credits = (
            data.get("credits")
            if isinstance(data.get("credits"), dict)
            else (data.get("credits_data") if isinstance(data.get("credits_data"), dict) else {})
        )
        self.credits_updated_at = float(data.get("creditsUpdatedAt") or data.get("credits_updated_at") or 0.0)
        # 运行时并发度计数器（华为云 CodeArts 单 Session 并发上限为 3）。
        # 计数与「抢占」必须在同一把条件变量里完成：先 is_available() 再把
        # in_flight += 1，两步之间另一个线程也能通过检查，计数器就会越过 3，
        # 上游随即回 TM.00001041，客户端再把 400 当成「模型坏了」换模型。
        self.in_flight = 0
        self._slot_cond = threading.Condition()
        # 「凭据不可用」隔离：STS 续期被上游拒绝（典型 STS5.1806 refresh token
        # has been used）时，重试多少次都不会自愈，先隔离一段时间等用户重新授权
        # 或在后台线程里重试，避免每个请求都去打一次 STS 并把失败伪装成并发满。
        self.credential_error_until = float(
            data.get("credentialErrorUntil") or data.get("credential_error_until") or 0.0
        )
        self.credential_error_detail = str(
            data.get("credentialErrorDetail") or data.get("credential_error_detail") or ""
        )[:500]
        # refresh_token 单次轮转：同一账号的两个并发刷新会把同一份 token 用两次，
        # 后到的请求必然拿到 STS5.1806。所有刷新入口（取号、后台续期、面板按钮）
        # 共用这一把锁，保证同一账号同一时刻只有一个刷新在飞。
        self._refresh_lock = threading.Lock()

    @property
    def has_sts(self) -> bool:
        return bool(self.security_token)

    def is_cooling(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return self.cooldown_until > t

    def is_restricted(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return self.restricted_until > t

    def is_credential_error(self, now: Optional[float] = None) -> bool:
        """STS 续期被拒后的一段时间内，该账号不再参与调度。"""
        t = now if now is not None else time.time()
        return self.credential_error_until > t

    def mark_credential_error(self, duration: float, detail: Any) -> None:
        """标记凭据失效并给出最短隔离时间（不会覆盖新的成功刷新）。"""
        duration = max(1.0, float(duration or 1.0))
        text = str(detail or "STS refresh failed")[:500]
        with self._slot_cond:
            self.credential_error_until = time.time() + duration
            self.credential_error_detail = text
        self.last_error = text
        print("[CodeArtsPool] account %s marked credential_error for %ss: %s"
              % (self.label, int(duration), text))

    def clear_credential_error(self) -> None:
        with self._slot_cond:
            self.credential_error_until = 0.0
            self.credential_error_detail = ""

    def is_expired(self, now_dt: Optional[Any] = None) -> bool:
        if not self.expires_at:
            return False
        try:
            stamp = datetime.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        if isinstance(now_dt, (int, float)):
            current = datetime.datetime.fromtimestamp(now_dt, tz=datetime.timezone.utc)
        elif isinstance(now_dt, datetime.datetime):
            current = now_dt if now_dt.tzinfo else now_dt.replace(tzinfo=datetime.timezone.utc)
        else:
            current = datetime.datetime.now(datetime.timezone.utc)
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
        if self.is_credential_error(t):
            return False
        if self.has_sts and self.is_expired(t) and not (self.refresh_token and self.login_context) and self.source != "desktop":
            return False
        if self.in_flight >= MAX_CONCURRENCY_PER_ACCOUNT:
            return False
        return True

    def acquire_slot(self, timeout: Optional[float] = None) -> bool:
        """原子抢占一个并发槽位；满员时最多等 ``timeout`` 秒。

        返回 False 表示等不到空位（调用方应回 429，而不是打上游碰运气）。
        """
        deadline = None if timeout is None else time.time() + timeout
        with self._slot_cond:
            while self.in_flight >= MAX_CONCURRENCY_PER_ACCOUNT:
                if deadline is None:
                    self._slot_cond.wait()
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._slot_cond.wait(remaining)
            self.in_flight += 1
            self.last_used = time.time()
            return True

    def release_slot(self) -> None:
        with self._slot_cond:
            self.in_flight = max(0, self.in_flight - 1)
            self._slot_cond.notify()

    def reserve_slot(self) -> None:
        """在调用方已持有池锁、并确认 ``is_available()`` 之后占用一个槽位。"""
        with self._slot_cond:
            self.in_flight += 1
            self.last_used = time.time()

    @contextlib.contextmanager
    def reserved_slot(self):
        """配套 :meth:`reserve_slot` 使用：只负责在结束时归还槽位。"""
        try:
            yield self
        finally:
            self.release_slot()

    @contextlib.contextmanager
    def slot(self):
        """并发槽位上下文管理器（阻塞等待空位）。"""
        self.acquire_slot()
        try:
            yield self
        finally:
            self.release_slot()

    def renew_threshold(self) -> float:
        """到期前多久开始主动续期。

        官方 ``safelyRenewTokenInterval`` 默认 3600s，而 STS 本身通常只有 1 小时
        有效期；直接照搬会让后台线程每一轮都刷新、频繁轮转单次使用的 refresh
        token，反而更容易和桌面端互相把 token 用掉。这里收敛到 60s~600s。
        """
        try:
            interval = float(self.safely_renew_token_interval or 0)
        except (TypeError, ValueError):
            interval = 3600.0
        return min(600.0, max(60.0, interval))

    def needs_renew(self, threshold: Optional[float] = None) -> bool:
        """STS 是否已过期或剩余不足阈值（默认 300s）。"""
        if self.source == "desktop":
            if not self.expires_at:
                return True
            try:
                stamp = datetime.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                remaining = (stamp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                return remaining < (threshold if threshold is not None else 300.0)
            except Exception:
                return True
        if not (self.has_sts and self.refresh_token and self.login_context):
            return False
        if not self.expires_at:
            return False
        try:
            stamp = datetime.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            remaining = (stamp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            return remaining < (threshold if threshold is not None else 300.0)
        except Exception:
            return False

    def ensure_fresh_sts(self, save=None, threshold: Optional[float] = None) -> None:
        """STS 已过期或剩余不足阈值时，用该账号自己的出口代理续期。

        ``refresh_token`` 单次轮转且 DPoP 绑定，所以：
          * 只能用账号自带的 ``loginContext`` 这**一份**去换，不能借用其它账号；
          * 同一账号的刷新必须串行 —— 两个请求同时拿同一份 refresh_token 去换，
            先到的成功并轮转出新 token，后到的必被拒
            ``STS5.1806 invalid refresh token: the refresh token has been used``。
        """
        with self._refresh_lock:
            # 双重检查：等锁期间别的线程可能已经刷新过，不能再用旧 token 刷一次。
            if not self.needs_renew(threshold):
                return
            if self.source == "desktop":
                try:
                    from codearts import codearts_session
                    ds = codearts_session.load_desktop_session()
                    if ds and ds.access_key and not codearts_session.is_expired(ds):
                        self.access_key = ds.access_key
                        self.secret_key = ds.secret_key
                        self.security_token = ds.security_token
                        self.expires_at = ds.expires_at
                        self.refresh_token = ds.refresh_token
                        self.clear_credential_error()
                        if save:
                            save()
                        print("[CodeArtsPool] re-read fresh desktop session for %s (expires_at=%s)"
                              % (self.label, self.expires_at), flush=True)
                        return
                except Exception as exc:
                    print(f"[CodeArtsPool] failed reading desktop session: {exc}", flush=True)
                raise RuntimeError("desktop session expired; please sign in via CodeArts IDE or Web OAuth")
            from codearts import codearts_oauth
            codearts_oauth.refresh_sts_token_for_account(self, proxy=self.proxy)
            self.clear_credential_error()
            if save:
                save()
            print("[CodeArtsPool] renewed STS for %s (expires_at=%s)"
                  % (self.label, self.expires_at), flush=True)

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
            region=self.region,
        )

    def get_welfare_delivery(self) -> Dict[str, Any]:
        """获取当前账号的福利发放与活动列表。"""
        try:
            from codearts import codearts_client as cac
        except ImportError:
            import codearts_client as cac
        sess = self.to_session()
        return cac.fetch_welfare_delivery(sess, proxy=self.proxy)

    def checkin(self) -> Dict[str, Any]:
        """对当前账号执行每日签到（领取所有可用福利，特别是每日签到）。"""
        try:
            from codearts import codearts_client as cac
        except ImportError:
            import codearts_client as cac
        sess = self.to_session()
        res = cac.claim_all_eligible_welfare(sess, proxy=self.proxy)
        if res.get("ok"):
            self.last_checkin_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.last_checkin_result = {
                "ok": True,
                "already_claimed": res.get("already_claimed", False),
                "total_amount": res.get("total_amount", 0.0),
                "message": res.get("message", ""),
                "timestamp": self.last_checkin_time,
            }
            try:
                self.fetch_credits(force=True)
            except Exception:
                pass
        return res

    def fetch_credits(self, force: bool = False, timeout: int = 15) -> Dict[str, Any]:
        """获取当前账号的积分与用量配额。具备 60 秒内存缓存以避免频繁调用华为云接口。"""
        now = time.time()
        if not force and self.credits and (now - self.credits_updated_at < 60.0):
            return self.credits
        try:
            from codearts import codearts_client as cac
        except ImportError:
            import codearts_client as cac
        sess = self.to_session()
        raw = cac.fetch_credits(sess, timeout=timeout, proxy=self.proxy)
        remain = 0.0
        total = 0.0
        used = 0.0
        metrics = raw.get("metrics") or []
        tot_metric = next((m for m in metrics if m.get("name") == "usageTotalPackageCredit"), None)
        if tot_metric:
            remain = float(tot_metric.get("package_credit_remain", 0.0) or 0.0)
            total = float(tot_metric.get("package_credit_amount", 0.0) or 0.0)
            used = float(tot_metric.get("package_credit_used", 0.0) or 0.0)
        elif raw.get("package"):
            total = 1000.0
            remain = 1000.0
        pkg_name = (raw.get("package") or {}).get("package_name_cn") or "体验版"
        end_date = (raw.get("package") or {}).get("end_date") or raw.get("end_date") or ""
        self.credits = {
            "remain": round(remain, 2),
            "total": round(total, 2),
            "used": round(used, 2),
            "package_name": pkg_name,
            "end_date": end_date,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.credits_updated_at = now
        return self.credits

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
            "region": self.region,
            "cooldownUntil": self.cooldown_until,
            "restrictedUntil": self.restricted_until,
            "restrictedDetail": self.restricted_detail,
            "credentialErrorUntil": self.credential_error_until,
            "credentialErrorDetail": self.credential_error_detail,
            "lastUsed": self.last_used,
            "lastError": self.last_error,
            "lastCheckinTime": self.last_checkin_time,
            "lastCheckinResult": self.last_checkin_result,
            "credits": self.credits,
            "creditsUpdatedAt": self.credits_updated_at,
        }

    def masked(self) -> Dict[str, Any]:
        """脱敏视图，供看板及管理 API 展示。"""
        now = time.time()
        cooling = self.is_cooling(now)
        restricted = self.is_restricted(now)
        credential_error = self.is_credential_error(now)
        expired = self.is_expired()

        if restricted:
            status = "restricted"
        elif cooling:
            status = "cooling"
        elif not self.enabled:
            status = "disabled"
        elif credential_error:
            status = "credential_error"
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
            "region": self.region,
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy,
            "enabled": self.enabled,
            "inFlight": self.in_flight,
            "maxConcurrency": MAX_CONCURRENCY_PER_ACCOUNT,
            "isCooling": cooling,
            "isRestricted": restricted,
            "isCredentialError": credential_error,
            "isExpired": expired,
            "status": status,
            "cooldownRemaining": max(0, int(self.cooldown_until - now)) if cooling else 0,
            "restrictedRemaining": max(0, int(self.restricted_until - now)) if restricted else 0,
            "restrictedDetail": self.restricted_detail,
            "credentialErrorRemaining": (max(0, int(self.credential_error_until - now))
                                         if credential_error else 0),
            "credentialErrorDetail": self.credential_error_detail,
            "lastUsed": self.last_used,
            "lastError": self.last_error,
            "lastCheckinTime": self.last_checkin_time,
            "lastCheckinResult": self.last_checkin_result,
            "credits": self.credits,
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
        self._lock = threading.RLock()
        self._cursor = 0
        # 最近一次「取号时 STS 刷新被拒」的信息。网关据此区分「凭据失效」
        # 与「并发满载」——两者最终都表现为拿不到账号，但客户端该做的事完全不同。
        self._last_credential_error: Optional[Dict[str, Any]] = None
        self._renew_thread: Optional[threading.Thread] = None
        self._renew_stop = threading.Event()
        self.load()

    @property
    def dir(self) -> str:
        return self.accounts_dir

    @staticmethod
    def _same_account(a: CodeArtsAccount, b: CodeArtsAccount) -> bool:
        """判断是否可视为同一个账号。

        只在**同一来源**内匹配：``desktop`` 与 ``web_oauth`` 可能是同一个华为
        账号（domain 相同），但 refresh token 的消费方与轮转策略不同，网页
        授权凭据绝不能覆盖桌面端条目，否则会把 IDE 的登录态写坏。
        """
        b_source = (b.source or "web_oauth").strip().lower()
        a_source = (a.source or "web_oauth").strip().lower()
        if a_source != b_source:
            return False
        return bool(
            (b.domain_id and a.domain_id == b.domain_id)
            or (b.label and a.label == b.label)
            or (b.id and a.id == b.id)
        )

    @classmethod
    def _find_merge_target(cls, accounts: List[CodeArtsAccount],
                           fresh: CodeArtsAccount) -> Optional[CodeArtsAccount]:
        """在同来源账号里找合并目标，优先启用中的条目。

        同名账号可能同时存在 desktop 与 web_oauth 两条；同一来源也可能有
        启用/停用两份历史副本。新授权必须落在正在使用的那条上，否则会出现
        「登录成功了，但面板里启用的账号还是旧凭据」。
        """
        candidates = [a for a in accounts if cls._same_account(a, fresh)]
        if not candidates:
            return None
        candidates.sort(key=lambda a: 0 if a.enabled else 1)
        return candidates[0]

    @staticmethod
    def _expiry_datetime(acct: CodeArtsAccount):
        if not acct.expires_at:
            return None
        try:
            stamp = datetime.datetime.fromisoformat(acct.expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp

    @staticmethod
    def _apply_credentials(target: CodeArtsAccount, fresh: CodeArtsAccount) -> None:
        """用一份新授权凭据覆盖账号，同时清除凭据失效标记。"""
        target.access_key = fresh.access_key
        target.secret_key = fresh.secret_key
        target.security_token = fresh.security_token
        target.expires_at = fresh.expires_at
        target.refresh_token = fresh.refresh_token
        target.login_context = fresh.login_context
        target.domain_id = fresh.domain_id or target.domain_id
        target.safely_renew_token_interval = fresh.safely_renew_token_interval
        # source 是账号的来源标识（desktop / web_oauth），不能因为一次刷新
        # 就被改成别的来源；它决定这个条目由哪条通道续期和消费。
        target.region = fresh.region or target.region
        target.clear_credential_error()

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

            # 兼容迁移与自动合并：确保 codearts_account.json 单账号已包含在池中
            if os.path.isfile(self.legacy_single_file):
                try:
                    with open(self.legacy_single_file, "r", encoding="utf-8") as f:
                        single_data = json.load(f)
                    if isinstance(single_data, dict) and (
                        single_data.get("accessKey") or single_data.get("access_key")
                    ):
                        single_acct = CodeArtsAccount(single_data)
                        existing = self._find_merge_target(loaded_accounts, single_acct)
                        if existing is None:
                            loaded_accounts.append(single_acct)
                            print(f"[CodeArtsPool] Auto-merged single Web account to pool: {single_acct.label}")
                        else:
                            # 旧逻辑：同账号直接跳过。问题在于网页 OAuth 重新授权
                            # 只写 legacy 文件，重启后池里会一直留着失效的旧 token。
                            # 这里只在 legacy 的 expires_at 更新时才采用新凭据，
                            # 避免用更旧的 legacy 文件覆盖池里较新的会话。
                            new_exp = self._expiry_datetime(single_acct)
                            old_exp = self._expiry_datetime(existing)
                            if new_exp is not None and (old_exp is None or new_exp > old_exp):
                                self._apply_credentials(existing, single_acct)
                                print(f"[CodeArtsPool] Adopted newer legacy credentials "
                                      f"for {existing.label}")
                except Exception as exc:
                    print(f"[CodeArtsPool] Failed to auto-merge single account: {exc}")

            self.accounts = loaded_accounts

        self.apply_proxy_slots()
        self.sync_desktop_account()
        if self.accounts:
            self.save()

    def sync_legacy_account(self) -> Optional[CodeArtsAccount]:
        """把旧版单账号文件（网页 OAuth 授权写入处）同步进多账号池。

        ``load()`` 对已存在的同账号只做「跳过」，重新授权后池里会继续留着
        失效的 refresh token。这个入口专门在新授权成功时调用：用新凭据覆盖
        同账号的凭据字段、清除 credential_error，同时保留 enabled/proxySlot
        等用户配置。
        """
        if not os.path.isfile(self.legacy_single_file):
            return None
        try:
            with open(self.legacy_single_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            print(f"[CodeArtsPool] Failed to read legacy account for sync: {exc}")
            return None
        if not isinstance(data, dict) or not (
            data.get("accessKey") or data.get("access_key")
        ):
            return None

        fresh = CodeArtsAccount(data)
        target = None
        with self._lock:
            target = self._find_merge_target(self.accounts, fresh)
            if target is not None:
                self._apply_credentials(target, fresh)
                print(f"[CodeArtsPool] synced new web login into pool account {target.label}")
            else:
                self.accounts.append(fresh)
                target = fresh
                print(f"[CodeArtsPool] added new web login to pool account {fresh.label}")
        self.save()
        return target

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
    def set_primary(self, account_id: str) -> bool:
        """将指定账号移至首选（第一顺位）并重置调度指针。"""
        with self._lock:
            target = None
            for a in self.accounts:
                if a.id == account_id:
                    target = a
                    break
            if not target:
                return False
            self.accounts.remove(target)
            self.accounts.insert(0, target)
            self._cursor = 0
        self.save()
        return True
    def set_all(self, enabled: bool) -> int:
        """批量启用或停用所有账号。"""
        count = 0
        with self._lock:
            for a in self.accounts:
                a.enabled = enabled
                count += 1
        self.save()
        return count

    def sync_desktop_account(self, force: bool = False) -> Optional[CodeArtsAccount]:
        """从本机桌面端 CodeArts Agent 提取会话，并同步/合并至多账号池。"""
        try:
            from codearts import codearts_session
            ds = codearts_session.load_desktop_session()
            if not ds or not ds.access_key:
                return None

            with self._lock:
                # 同名账号可能同时有 desktop 与 web_oauth 两条：桌面端同步只能
                # 落在 desktop 条目或新建 desktop 条目，不能把 IDE 会话写进网页
                # OAuth 条目（两边的 refresh token 会互相顶掉）。
                candidates = [a for a in self.accounts
                              if (ds.domain_id and a.domain_id == ds.domain_id)
                              or (ds.account_label and a.label == ds.account_label)
                              or (a.access_key and a.access_key == ds.access_key)]
                candidates.sort(key=lambda a: 0 if a.source == "desktop" else 1)
                matched: Optional[CodeArtsAccount] = candidates[0] if candidates else None

                if matched and matched.source == "desktop":
                    matched.access_key = ds.access_key
                    matched.secret_key = ds.secret_key
                    matched.security_token = ds.security_token
                    matched.expires_at = ds.expires_at
                    matched.refresh_token = ds.refresh_token
                    if ds.account_label and not matched.label:
                        matched.label = ds.account_label
                    if ds.domain_id and not matched.domain_id:
                        matched.domain_id = ds.domain_id
                    res = matched
                elif matched and force:
                    # 仅在显式 force 时允许把桌面凭据写进非 desktop 条目。
                    matched.access_key = ds.access_key
                    matched.secret_key = ds.secret_key
                    matched.security_token = ds.security_token
                    matched.expires_at = ds.expires_at
                    matched.refresh_token = ds.refresh_token
                    res = matched
                else:
                    acct_id = ds.domain_id or f"desktop_{ds.account_label}"
                    res = CodeArtsAccount({
                        "id": acct_id,
                        "label": ds.account_label or "desktop",
                        "accessKey": ds.access_key,
                        "secretKey": ds.secret_key,
                        "securityToken": ds.security_token,
                        "expiresAt": ds.expires_at,
                        "refreshToken": ds.refresh_token,
                        "domainId": ds.domain_id,
                        "source": "desktop",
                        "enabled": True,
                    })
                    self.accounts.append(res)
                    print(f"[CodeArtsPool] Synced desktop account into pool: {res.label}")

            self.apply_proxy_slots()
            self.save()
            return res
        except Exception as exc:
            # e.g. SessionMissing on non-Windows or not installed
            return None


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

    def pick_account(self, exclude_ids: Optional[Any] = None) -> Tuple[Optional[CodeArtsAccount], Optional[CodeArtsSession]]:
        """从可用账号中选取最合适的一个（最小在飞并发 + 环形轮询）。

        若选中的账号 STS 已过期或濒临过期（剩余 < 300s），且具备 refreshToken
        与 loginContext，则自动走该账号专属的 proxy 执行 STS 刷新。刷新失败
        不再原地返回这个坏账号，而是标记并换下一个候选。
        """
        attempted = set(exclude_ids or ())
        failed_acct = None
        last_exc = None
        blocked = None
        while True:
            now = time.time()
            with self._lock:
                available = [a for a in self.accounts
                             if a.is_available(now) and a.id not in attempted]
                if not available:
                    # 账号还在「凭据失效」隔离期内（比如刚重启、从文件读回了
                    # credentialErrorUntil）时也不会进 available；把原因带上，
                    # 否则上层又会把「凭据失效」误报成并发满。
                    blocked = next((a for a in self.accounts
                                    if a.enabled and a.is_credential_error(now)), None)
                    break
                # 旧的排序键 (cursor - index) % n 在 cursor=1 时永远让 index 0
                # 最小，失败账号会一直霸占第一位；改成从 cursor 开始的环形顺序。
                start = self._cursor % len(self.accounts)
                available.sort(key=lambda a: (a.in_flight,
                                              (self.accounts.index(a) - start) % len(self.accounts)))
                picked = available[0]
                self._cursor = (self.accounts.index(picked) + 1) % len(self.accounts)
            try:
                picked.ensure_fresh_sts(save=self.save)
            except Exception as exc:
                picked.mark_credential_error(CREDENTIAL_ERROR_COOLDOWN, exc)
                attempted.add(picked.id)
                failed_acct, last_exc = picked, exc
                print(f"[CodeArtsPool] Failed auto-refreshing STS for account {picked.label}: {exc}")
                continue
            picked.clear_credential_error()
            return picked, picked.to_session()
        if failed_acct is not None:
            self._record_credential_error(failed_acct, last_exc)
        elif blocked is not None:
            self._record_credential_error(
                blocked, blocked.credential_error_detail or "STS refresh rejected")
        return None, None

    def acquire_account(self, exclude_ids: Optional[Any] = None
                        ) -> Tuple[Optional[CodeArtsAccount], Optional[CodeArtsSession]]:
        """选中并**原子占用**一个并发槽位。

        与 :meth:`pick_account` 的区别：选择与 ``in_flight += 1`` 在同一把锁里，
        两个线程不会同时通过 ``in_flight < 3`` 的检查再把计数顶到 4。调用方必须
        在请求结束后 ``release_slot()``（或用 ``reserved_slot()`` 上下文）。

        续期失败时释放槽位、标记该账号凭据失效并继续换下一个候选 —— 否则池里
        第一个账号的 refresh token 一旦失效，整个池会被它拖死（线上 48 次重试
        全打在同一个账号上）。
        """
        attempted = set(exclude_ids or ())
        failed_acct = None
        last_exc = None
        blocked = None
        while True:
            now = time.time()
            with self._lock:
                available = [a for a in self.accounts
                             if a.is_available(now) and a.id not in attempted]
                if not available:
                    blocked = next((a for a in self.accounts
                                    if a.enabled and a.is_credential_error(now)), None)
                    break
                start = self._cursor % len(self.accounts)
                available.sort(key=lambda a: (a.in_flight,
                                              (self.accounts.index(a) - start) % len(self.accounts)))
                picked = available[0]
                self._cursor = (self.accounts.index(picked) + 1) % len(self.accounts)
                picked.reserve_slot()
            try:
                picked.ensure_fresh_sts(save=self.save)
            except Exception as exc:
                picked.release_slot()
                picked.mark_credential_error(CREDENTIAL_ERROR_COOLDOWN, exc)
                attempted.add(picked.id)
                failed_acct, last_exc = picked, exc
                print(f"[CodeArtsPool] Failed preparing account {picked.label}: {exc}")
                continue
            picked.clear_credential_error()
            return picked, picked.to_session()
        if failed_acct is not None:
            self._record_credential_error(failed_acct, last_exc)
        elif blocked is not None:
            self._record_credential_error(
                blocked, blocked.credential_error_detail or "STS refresh rejected")
        return None, None

    def _record_credential_error(self, account: CodeArtsAccount, exc: Any) -> None:
        with self._lock:
            self._last_credential_error = {
                "at": time.time(),
                "accountId": getattr(account, "id", ""),
                "label": getattr(account, "label", ""),
                "message": str(exc or "STS refresh failed")[:500],
            }

    def credential_error_snapshot(self) -> Optional[Dict[str, Any]]:
        """最近一次取号时的凭据刷新失败，供网关返回 credentials_error。"""
        with self._lock:
            err = getattr(self, "_last_credential_error", None)
            return dict(err) if err else None

    def start_background_renewal(self, tick_seconds: float = 60.0) -> bool:
        """启动 STS 后台续期线程（daemon）。

        原先只有请求进来时才顺带检查续期，长时间空闲后凭据会静默过期；这里按
        ``safelyRenewTokenInterval`` 收敛出的窗口主动续期。每个 tick 只处理已进入
        续期窗口的启用账号，``ensure_fresh_sts`` 内部还有刷新锁与双重检查，
        不会频繁轮转单次使用的 refresh token。
        """
        if getattr(self, "_renew_thread", None) is not None and self._renew_thread.is_alive():
            return False
        if not hasattr(self, "_renew_stop") or self._renew_stop is None:
            self._renew_stop = threading.Event()
        self._renew_stop.clear()

        def _loop():
            while not self._renew_stop.wait(tick_seconds):
                try:
                    with self._lock:
                        targets = [a for a in self.accounts
                                   if a.enabled and a.refresh_token and not a.is_credential_error()]
                    for acct in targets:
                        if not acct.needs_renew(acct.renew_threshold()):
                            continue
                        try:
                            acct.ensure_fresh_sts(save=self.save, threshold=acct.renew_threshold())
                        except Exception as exc:
                            acct.mark_credential_error(CREDENTIAL_ERROR_COOLDOWN, exc)
                except Exception as exc:
                    print("[CodeArtsPool] renewal loop error: %s" % exc)

        thread = threading.Thread(target=_loop, name="codearts-sts-renew", daemon=True)
        self._renew_thread = thread
        thread.start()
        print("[CodeArtsPool] STS renewal thread started (tick=%ss, renew_window<=%ss)"
              % (int(tick_seconds),
                 int(max((a.renew_threshold() for a in self.accounts), default=300.0))), flush=True)
        return True

    def stop_background_renewal(self) -> None:
        event = getattr(self, "_renew_stop", None)
        if event is not None:
            event.set()
        thread = getattr(self, "_renew_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._renew_thread = None

    def wait_seconds_until_free(self) -> float:
        """所有账号被冷却/并发占满时，估算还要等多久（用于 429 的提示）。"""
        now = time.time()
        waits = []
        with self._lock:
            for a in self.accounts:
                if not a.enabled:
                    continue
                if a.restricted_until > now:
                    waits.append(a.restricted_until - now)
                elif a.cooldown_until > now:
                    waits.append(a.cooldown_until - now)
                elif a.in_flight >= MAX_CONCURRENCY_PER_ACCOUNT:
                    waits.append(0.0)
        return min(waits) if waits else 0.0

    def public_view(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [a.masked() for a in self.accounts]

    def checkin_account(self, account_id: str) -> Dict[str, Any]:
        """对指定账号执行签到。"""
        acct = self.get(account_id)
        if not acct:
            return {"ok": False, "error": f"Account {account_id} not found"}
        res = acct.checkin()
        self.save()
        return {"ok": res.get("ok", False), "account": acct.masked(), "result": res}

    def checkin_all(self) -> Dict[str, Any]:
        """对池中所有已启用且有效的账号执行签到。"""
        results = []
        total_points = 0.0
        success_count = 0
        with self._lock:
            accounts_to_check = [a for a in self.accounts if a.enabled]
        if not accounts_to_check:
            # 兜底：若池中为空，尝试从默认 session 签到
            try:
                from codearts import codearts_session as cas
                from codearts import codearts_client as cac
                sess = cas.load_session()
                r = cac.claim_all_eligible_welfare(sess)
                amt = float(r.get("total_amount") or 0.0)
                return {
                    "ok": r.get("ok", False),
                    "success_count": 1 if r.get("ok") else 0,
                    "total_accounts": 1,
                    "total_points": amt,
                    "details": [{
                        "id": getattr(sess, "account_label", "default"),
                        "label": getattr(sess, "account_label", "default"),
                        "ok": r.get("ok", False),
                        "already_claimed": r.get("already_claimed", False),
                        "total_amount": amt,
                        "message": r.get("message", ""),
                        "result": r
                    }]
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "success_count": 0,
                    "total_accounts": 0,
                    "total_points": 0.0,
                    "error": f"暂无可用的 CodeArts 账号: {exc}",
                    "details": []
                }

        for idx, acct in enumerate(accounts_to_check):
            if idx > 0:
                time.sleep(1.0)
            try:
                r = acct.checkin()
                points = float(r.get("total_amount") or 0.0)
                total_points += points
                if r.get("ok"):
                    success_count += 1
                results.append({
                    "id": acct.id,
                    "label": acct.label,
                    "ok": r.get("ok", False),
                    "already_claimed": r.get("already_claimed", False),
                    "total_amount": points,
                    "message": r.get("message", ""),
                    "result": r
                })
            except Exception as exc:
                results.append({
                    "id": acct.id,
                    "label": acct.label,
                    "ok": False,
                    "already_claimed": False,
                    "total_amount": 0.0,
                    "message": str(exc),
                    "error": str(exc)
                })
        self.save()
        return {
            "ok": True,
            "success_count": success_count,
            "total_accounts": len(accounts_to_check),
            "total_points": total_points,
            "details": results
        }

    def fetch_all_credits(self, force: bool = False) -> Dict[str, Any]:
        """聚合查询所有可用账号的积分并计算全池总积分余额。"""
        import concurrent.futures

        def _fetch_one(acct: CodeArtsAccount):
            try:
                c = acct.fetch_credits(force=force)
                return acct, c, None
            except Exception as e:
                return acct, acct.credits or {}, str(e)

        active_accts = [a for a in self.accounts if a.enabled and a.has_sts and not a.is_expired()]
        if not active_accts and self.accounts:
            active_accts = [self.accounts[0]]

        if active_accts:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(active_accts), 5)) as pool:
                futures = [pool.submit(_fetch_one, a) for a in active_accts]
                for f in concurrent.futures.as_completed(futures):
                    pass

        total_remain = 0.0
        total_amount = 0.0
        total_used = 0.0
        accounts_credits = []
        for a in self.accounts:
            c = a.credits or {}
            c_remain = float(c.get("remain", 0.0) or 0.0)
            c_total = float(c.get("total", 0.0) or 0.0)
            c_used = float(c.get("used", 0.0) or 0.0)
            if a.enabled:
                total_remain += c_remain
                total_amount += c_total
                total_used += c_used
            accounts_credits.append({
                "id": a.id,
                "label": a.label,
                "enabled": a.enabled,
                "source": a.source,
                "remain": round(c_remain, 2),
                "total": round(c_total, 2),
                "used": round(c_used, 2),
                "package_name": c.get("package_name", "体验版"),
                "end_date": c.get("end_date", ""),
            })

        self.save()
        pct_remain = round((total_remain / total_amount) * 100, 1) if total_amount > 0 else 0.0
        return {
            "ok": True,
            "total_remain": round(total_remain, 2),
            "total_amount": round(total_amount, 2),
            "total_used": round(total_used, 2),
            "percent_remain": pct_remain,
            "accounts_count": len(self.accounts),
            "active_accounts_count": len([a for a in self.accounts if a.enabled]),
            "accounts": accounts_credits,
        }
