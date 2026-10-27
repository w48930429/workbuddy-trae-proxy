import os
import time
import asyncio
from typing import Optional, Dict, Any
import httpx

from .account_pool import AccountPool, current_time_ms

GOOGLE_TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'


def _oauth_credentials() -> tuple[str, str]:
    """Google OAuth credentials come from the environment only.

    No compiled-in fallback: a default secret in source is a leaked secret the
    moment the repository is public, and a fallback also hides a missing
    deployment variable until token refresh silently fails.
    """
    return (os.environ.get('OAUTH_CLIENT_ID', ''),
            os.environ.get('OAUTH_CLIENT_SECRET', ''))


class TokenRefresher:
    def __init__(self, pool: AccountPool, config: Dict[str, Any]):
        self.pool = pool
        self.config = config
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        print('[TokenRefresher] Starting background token refresh worker...')
        self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            print('[TokenRefresher] Stopped background worker.')

    async def _run_loop(self) -> None:
        # 初始检查
        try:
            await self.refresh_eligible_accounts()
        except Exception as e:
            print(f'[TokenRefresher] Initial check error: {e}')

        interval_sec = max(10, self.config.get('tokenRefreshIntervalMs', 180000) / 1000)
        while self._running:
            try:
                await asyncio.sleep(interval_sec)
                await self.refresh_eligible_accounts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f'[TokenRefresher] Periodic check error: {e}')

    async def refresh_eligible_accounts(self) -> None:
        now = current_time_ms()
        ten_minutes = 10 * 60 * 1000

        for acc in self.pool.get_all_accounts():
            if acc.get('status') in ('dead', 'disabled'):
                continue
            if not acc.get('refreshToken'):
                continue

            needs_refresh = (
                not acc.get('accessToken') or
                (acc.get('accessTokenExpiresAt', 0) - now < ten_minutes)
            )
            if needs_refresh:
                await self.refresh_single_account(acc)

    async def refresh_single_account(self, acc: Dict[str, Any]) -> bool:
        client_id, client_secret = _oauth_credentials()
        if not client_id or not client_secret:
            print('[TokenRefresher] OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET are not set; '
                  'skipping token refresh. Export both environment variables and restart.')
            return False

        data = {
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': acc['refreshToken'],
            'grant_type': 'refresh_token'
        }

        effective_proxy = (
            acc.get('proxyUrl') or
            self.config.get('defaultProxyUrl') or
            os.environ.get('HTTPS_PROXY') or
            os.environ.get('HTTP_PROXY')
        )

        for attempt in range(1, 3):
            try:
                if attempt == 1:
                    print(f"[TokenRefresher] Refreshing token for account: {acc.get('email')} ({acc.get('id')})...")

                client_kwargs: Dict[str, Any] = {
                    'timeout': httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
                }
                if effective_proxy:
                    client_kwargs['proxy'] = effective_proxy

                async with httpx.AsyncClient(**client_kwargs) as client:
                    res = await client.post(
                        GOOGLE_TOKEN_ENDPOINT,
                        data=data,
                        headers={'Content-Type': 'application/x-www-form-urlencoded'}
                    )

                if res.status_code == 200:
                    token_data = res.json()
                    new_token = token_data.get('access_token')
                    expires_in = token_data.get('expires_in', 3600)

                    acc['accessToken'] = new_token
                    acc['accessTokenExpiresAt'] = current_time_ms() + expires_in * 1000
                    if acc.get('status') == 'dead':
                        acc['status'] = 'active'
                    acc.pop('lastError', None)
                    self.pool.save_accounts()
                    print(f"[TokenRefresher] Successfully refreshed token for {acc.get('email')}. Expires in {expires_in}s.")
                    return True
                else:
                    err_text = res.text
                    print(f"[TokenRefresher] Failed to refresh token for {acc.get('email')}, HTTP {res.status_code}: {err_text}")
                    acc['lastError'] = f"OAuth Refresh Failed ({res.status_code}): {err_text[:200]}"
                    if res.status_code in (400, 401):
                        acc['status'] = 'dead'
                    self.pool.save_accounts()
                    return False

            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                print(f"[TokenRefresher] Network error refreshing token for {acc.get('email')} (will retry next cycle): {e}")
                acc['lastError'] = f"OAuth Network Error: {str(e)[:200]}"
                self.pool.save_accounts()
                return False

        return False
