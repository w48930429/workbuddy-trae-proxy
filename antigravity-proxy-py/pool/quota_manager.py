import os
import time
import asyncio
from typing import Optional, Dict, Any, List
import httpx

from .account_pool import AccountPool, current_time_ms

QUOTA_SUMMARY_URL = 'https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary'
LOAD_CODE_ASSIST_URL = 'https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist'


def resolve_tier_from_code_assist(tier_data: Dict[str, Any]) -> Optional[str]:
    """从 loadCodeAssist 响应解析订阅等级。

    实测（2026-11）结构：
    - 个人号 currentTier.id 恒为 "free-tier"、name 为 "Antigravity"；
    - 付费订阅在 paidTier：id="g1-pro-tier"、name="Google AI Pro"，
      免费号为 "Antigravity Starter Quota"（或不存在 paidTier）。

    返回 None 表示本次响应无法给出明确结论（字段缺失/未知命名），
    调用方必须保留账号原有 tier，绝不能默认按 free 处理并停用账号。
    """
    if not isinstance(tier_data, dict):
        return None

    current_tier = tier_data.get('currentTier') or {}
    paid_tier = tier_data.get('paidTier') or {}

    current_id = str(current_tier.get('id') or '').lower()
    current_name = str(current_tier.get('name') or '').lower()
    paid_id = str(paid_tier.get('id') or '').lower()
    paid_name = str(paid_tier.get('name') or '').lower()
    available_credits = paid_tier.get('availableCredits') or []
    has_ai_credit = any(
        isinstance(c, dict) and c.get('creditType') == 'GOOGLE_ONE_AI'
        for c in available_credits
    )

    # paidTier 存在时优先采信（id 与 name 双通道，兼容上游改字段）
    if paid_id or paid_name:
        paid_probe = f'{paid_id} {paid_name}'
        if 'ultra' in paid_probe:
            return 'ultra'
        if 'pro' in paid_probe or has_ai_credit:
            return 'pro'
        if 'free' in paid_id or 'starter' in paid_name or 'free' in paid_name:
            return 'free'
        return None

    # 无 paidTier 时退回 currentTier（企业/特殊租户可能直接给出标准付费档）
    current_probe = f'{current_id} {current_name}'
    if 'ultra' in current_probe:
        return 'ultra'
    if 'pro' in current_probe:
        return 'pro'
    if 'free' in current_id or 'starter' in current_name or 'free' in current_name:
        return 'free'
    return None


class QuotaManager:
    def __init__(self, pool: AccountPool, config: Optional[Dict[str, Any]] = None):
        self.pool = pool
        self.config = config or {}
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def fetch_quota_for_account(self, account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        access_token = account.get('accessToken')
        if not access_token:
            return None

        effective_proxy = (
            account.get('proxyUrl') or
            self.config.get('defaultProxyUrl') or
            os.environ.get('HTTPS_PROXY') or
            os.environ.get('HTTP_PROXY')
        )

        client_kwargs: Dict[str, Any] = {
            'timeout': httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
        }
        if effective_proxy:
            client_kwargs['proxy'] = effective_proxy

        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'User-Agent': 'antigravity/2.14.0',
            'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
        }

        res = None
        for attempt in range(1, 3):
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    res = await client.post(
                        QUOTA_SUMMARY_URL,
                        headers=headers,
                        json={'project': 'aicode-consumers'}
                    )
                break
            except Exception as err:
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                print(f"[QuotaManager] Network error fetching quota for {account.get('email')} (transient socket drop, retry next cycle): {err}")
                return None

        if res is None:
            return None

        if res.status_code != 200:
            err_text = res.text
            print(f"[QuotaManager] Failed to fetch quota for {account.get('email')} (HTTP {res.status_code}): {err_text[:100]}")
            try:
                err_json = res.json()
                validation_url = None
                details = err_json.get('error', {}).get('details', [])
                for d in details:
                    if d.get('reason') == 'VALIDATION_REQUIRED' or (d.get('metadata') and d['metadata'].get('validation_url')):
                        validation_url = d.get('metadata', {}).get('validation_url')
                        break
                if not validation_url and 'Verify your account' in err_json.get('error', {}).get('message', ''):
                    if details and details[0].get('metadata', {}).get('validation_url'):
                        validation_url = details[0]['metadata']['validation_url']

                if validation_url:
                    self.pool.update_account(account['id'], {
                        'validationUrl': validation_url,
                        'lastError': '需要完成 Google 账号验证'
                    })
                else:
                    msg = err_json.get('error', {}).get('message') or err_text[:60]
                    self.pool.update_account(account['id'], {
                        'lastError': f"HTTP {res.status_code}: {msg}"
                    })
            except Exception:
                self.pool.update_account(account['id'], {
                    'lastError': f"HTTP {res.status_code}"
                })
            return None

        try:
            data = res.json()
        except Exception:
            return None

        groups = data.get('groups', [])

        gemini_weekly = 1.0
        gemini_5h = 1.0
        gemini_reset_time = None
        gemini_weekly_reset_time = None
        gemini_5h_desc = None
        gemini_weekly_desc = None

        claude_weekly = 1.0
        claude_5h = 1.0
        claude_reset_time = None
        claude_weekly_reset_time = None
        claude_5h_desc = None
        claude_weekly_desc = None

        for group in groups:
            d_name = (group.get('displayName') or '').lower()
            buckets = group.get('buckets', [])

            if 'gemini' in d_name:
                for b in buckets:
                    if b.get('window') == 'weekly' or b.get('bucketId') == 'gemini-weekly':
                        gemini_weekly = b.get('remainingFraction') if isinstance(b.get('remainingFraction'), (int, float)) else 1.0
                        if b.get('resetTime'):
                            gemini_weekly_reset_time = b['resetTime']
                        if b.get('description'):
                            gemini_weekly_desc = b['description']
                    if b.get('window') == '5h' or b.get('bucketId') == 'gemini-5h':
                        gemini_5h = b.get('remainingFraction') if isinstance(b.get('remainingFraction'), (int, float)) else 1.0
                        if b.get('resetTime'):
                            gemini_reset_time = b['resetTime']
                        if b.get('description'):
                            gemini_5h_desc = b['description']
            elif 'claude' in d_name or 'gpt' in d_name:
                for b in buckets:
                    if b.get('window') == 'weekly' or b.get('bucketId') == '3p-weekly':
                        claude_weekly = b.get('remainingFraction') if isinstance(b.get('remainingFraction'), (int, float)) else 1.0
                        if b.get('resetTime'):
                            claude_weekly_reset_time = b['resetTime']
                        if b.get('description'):
                            claude_weekly_desc = b['description']
                    if b.get('window') == '5h' or b.get('bucketId') == '3p-5h':
                        claude_5h = b.get('remainingFraction') if isinstance(b.get('remainingFraction'), (int, float)) else 1.0
                        if b.get('resetTime'):
                            claude_reset_time = b['resetTime']
                        if b.get('description'):
                            claude_5h_desc = b['description']

        quota = {
            'geminiWeeklyFraction': round(gemini_weekly, 4),
            'gemini5hFraction': round(gemini_5h, 4),
            'geminiResetTime': gemini_reset_time,
            'geminiWeeklyResetTime': gemini_weekly_reset_time,
            'gemini5hDesc': gemini_5h_desc,
            'geminiWeeklyDesc': gemini_weekly_desc,
            'claudeWeeklyFraction': round(claude_weekly, 4),
            'claude5hFraction': round(claude_5h, 4),
            'claudeResetTime': claude_reset_time,
            'claudeWeeklyResetTime': claude_weekly_reset_time,
            'claude5hDesc': claude_5h_desc,
            'claudeWeeklyDesc': claude_weekly_desc,
            'lastCheckedAt': current_time_ms()
        }

        account['quota'] = quota
        account.pop('validationUrl', None)
        account.pop('lastError', None)
        self.pool.update_account(account['id'], {
            'quota': quota,
            'validationUrl': None,
            'lastError': None
        })

        # 同步拉取账号订阅等级 (v1internal:loadCodeAssist)
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                tier_res = await client.post(
                    LOAD_CODE_ASSIST_URL,
                    headers=headers,
                    json={}
                )
            if tier_res.status_code == 200:
                tier_data = tier_res.json()
                # 判定规则见 resolve_tier_from_code_assist：
                # 返回 None 表示本次响应无法判定，保留原 tier，避免把 PRO 误判为 free。
                tier = resolve_tier_from_code_assist(tier_data)

                if tier:
                    account['tier'] = tier
                    self.pool.update_account(account['id'], {'tier': tier})

                    # 免费号输入上限远低于 PRO，确认 free 才自动停用
                    if tier == 'free' and account.get('status') == 'active':
                        self.pool.update_account(account['id'], {
                            'status': 'disabled',
                            'lastError': 'free 账号已自动停用：输入上限过低',
                            'autoDisabledForFree': True
                        })
                        print(f"[QuotaManager] {account.get('email')} 为 free 账号（输入上限过低），已自动停用。")

                    # 自愈：此前因 tier 未知/误判被自动停用的账号，复检确认是 pro/ultra 后自动恢复，
                    # 否则停用账号永远拿不到调度机会，形成不可逆死锁。
                    # 用 autoDisabledForFree 标记区分“自动停用”与“用户手动停用”，不误启用后者。
                    if (tier != 'free' and account.get('status') == 'disabled' and
                            account.get('autoDisabledForFree') is True):
                        self.pool.update_account(account['id'], {
                            'status': 'active',
                            'lastError': None,
                            'autoDisabledForFree': None
                        })
                        print(f"[QuotaManager] {account.get('email')} 复检确认为 {tier} 账号，已自动恢复启用。")
        except Exception:
            pass

        return quota

    async def fetch_all_quotas(self) -> None:
        accounts = self.pool.get_all_accounts()
        for acc in accounts:
            # dead 账号凭据已明确失效，不再消耗上游请求；
            # disabled 必须继续复检，否则被误停的账号永远无法重新识别为 pro（死锁）。
            if acc.get('status') == 'dead':
                continue
            await self.fetch_quota_for_account(acc)
            await asyncio.sleep(0.6)

    def start_background_worker(self, interval_ms: int = 180000) -> None:
        if self._running:
            return
        self._running = True
        print('[QuotaManager] Starting background quota monitor worker...')
        self._task = asyncio.create_task(self._run_loop(interval_ms))

    async def _run_loop(self, interval_ms: int) -> None:
        # 初始延迟 5 秒后首次运行
        await asyncio.sleep(5)
        try:
            # 旧逻辑（已停用）：启动时把“当前已知为 free”且仍 active 的账号立即停用。
            # 该兜底会在账号等级尚未真实探测（导入时 tier 缺省为 free）时抢先误杀 PRO 账号，
            # 且停用后不再复检，形成永久死锁。现在统一改为：只有 loadCodeAssist 明确返回 free 才停用。
            # for acc in self.pool.get_all_accounts():
            #     if acc.get('tier') == 'free' and acc.get('status') == 'active':
            #         self.pool.update_account(acc['id'], {
            #             'status': 'disabled',
            #             'lastError': 'free 账号已自动停用：输入上限过低'
            #         })
            #         print(f"[QuotaManager] {acc.get('email')} 为 free 账号（输入上限过低），已自动停用。")
            await self.fetch_all_quotas()
        except Exception as e:
            print(f"[QuotaManager] Initial fetch error: {e}")

        interval_sec = max(10, interval_ms / 1000)
        while self._running:
            try:
                await asyncio.sleep(interval_sec)
                await self.fetch_all_quotas()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[QuotaManager] Periodic quota check error: {e}")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            print('[QuotaManager] Stopped background worker.')
