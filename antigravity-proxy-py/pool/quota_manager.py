import os
import time
import asyncio
from typing import Optional, Dict, Any, List
import httpx

from .account_pool import AccountPool, current_time_ms

QUOTA_SUMMARY_URL = 'https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary'
LOAD_CODE_ASSIST_URL = 'https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist'


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
                current_id = str(tier_data.get('currentTier', {}).get('id') or '').lower()
                paid_id = str(tier_data.get('paidTier', {}).get('id') or '').lower()
                available_credits = tier_data.get('paidTier', {}).get('availableCredits', [])
                has_ai_credit = any(c.get('creditType') == 'GOOGLE_ONE_AI' for c in available_credits if isinstance(c, dict))

                tier = 'free'
                probe = paid_id or current_id
                if 'ultra' in probe:
                    tier = 'ultra'
                elif 'pro' in probe or has_ai_credit:
                    tier = 'pro'
                elif 'ultra' in current_id:
                    tier = 'ultra'
                elif 'pro' in current_id:
                    tier = 'pro'

                account['tier'] = tier
                self.pool.update_account(account['id'], {'tier': tier})

                # 免费号输入上限远低于 PRO，自动停用
                if tier == 'free' and account.get('status') == 'active':
                    self.pool.update_account(account['id'], {
                        'status': 'disabled',
                        'lastError': 'free 账号已自动停用：输入上限过低'
                    })
                    print(f"[QuotaManager] {account.get('email')} 为 free 账号（输入上限过低），已自动停用。")
        except Exception:
            pass

        return quota

    async def fetch_all_quotas(self) -> None:
        accounts = self.pool.get_all_accounts()
        for acc in accounts:
            if acc.get('status') in ('dead', 'disabled'):
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
            # 启动时检查：把当前已知为 free 且仍 active 的账号立即停用
            for acc in self.pool.get_all_accounts():
                if acc.get('tier') == 'free' and acc.get('status') == 'active':
                    self.pool.update_account(acc['id'], {
                        'status': 'disabled',
                        'lastError': 'free 账号已自动停用：输入上限过低'
                    })
                    print(f"[QuotaManager] {acc.get('email')} 为 free 账号（输入上限过低），已自动停用。")
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
