import { request, ProxyAgent } from 'undici';
import { Account, AccountQuota, AppConfig } from '../types.js';
import { AccountPool } from './account-pool.js';

export class QuotaManager {
  private pollTimer: NodeJS.Timeout | null = null;

  constructor(
    private pool: AccountPool,
    private config?: AppConfig
  ) {}

  /**
   * 从 Google 官方上游 (v1internal:retrieveUserQuotaSummary) 拉取账号实时额度
   */
  public async fetchQuotaForAccount(account: Account): Promise<AccountQuota | null> {
    if (!account.accessToken) return null;

    try {
      const effectiveProxy = account.proxyUrl || this.config?.defaultProxyUrl || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
      const dispatcher = effectiveProxy ? new ProxyAgent({
        uri: effectiveProxy,
        connect: { timeout: 30000 }
      }) : undefined;
    let res: any;

    for (let attempt = 1; attempt <= 2; attempt++) {
      try {
        res = await request('https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary', {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${account.accessToken}`,
            'Content-Type': 'application/json',
            'User-Agent': 'antigravity/2.14.0',
            'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
          },
          body: JSON.stringify({ project: 'aicode-consumers' }),
          headersTimeout: 30000,
          bodyTimeout: 30000,
          dispatcher
        });
        break;
      } catch (err: any) {
        if (attempt < 2) {
          await new Promise(r => setTimeout(r, 1500));
          continue;
        }
        console.warn(`[QuotaManager] Network error fetching quota for ${account.email} (transient socket drop, will retry next cycle):`, err.message);
        return null;
      }
    }

      if (res.statusCode !== 200) {
        const errText = await res.body.text();
        console.warn(`[QuotaManager] Failed to fetch quota for ${account.email} (HTTP ${res.statusCode}): ${errText.substring(0, 100)}`);
        try {
          const errJson = JSON.parse(errText);
          const validationDetail = errJson.error?.details?.find((d: any) => d.reason === 'VALIDATION_REQUIRED' || d.metadata?.validation_url);
          const validationUrl = validationDetail?.metadata?.validation_url || (errJson.error?.message?.includes('Verify your account') ? errJson.error?.details?.[0]?.metadata?.validation_url : undefined);

          if (validationUrl) {
            account.validationUrl = validationUrl;
            account.lastError = '需要完成 Google 账号验证';
            this.pool.updateAccount(account.id, { validationUrl, lastError: account.lastError });
          } else {
            account.lastError = `HTTP ${res.statusCode}: ${errJson.error?.message || errText.substring(0, 60)}`;
            this.pool.updateAccount(account.id, { lastError: account.lastError });
          }
        } catch {
          account.lastError = `HTTP ${res.statusCode}`;
          this.pool.updateAccount(account.id, { lastError: account.lastError });
        }
        return null;
      }

      const data = (await res.body.json()) as any;
      const groups = data.groups || [];

      let geminiWeekly = 1.0;
      let gemini5h = 1.0;
      let geminiResetTime = '';
      let geminiWeeklyResetTime = '';
      let gemini5hDesc = '';
      let geminiWeeklyDesc = '';

      let claudeWeekly = 1.0;
      let claude5h = 1.0;
      let claudeResetTime = '';
      let claudeWeeklyResetTime = '';
      let claude5hDesc = '';
      let claudeWeeklyDesc = '';

      for (const group of groups) {
        const dName = (group.displayName || '').toLowerCase();
        const buckets = group.buckets || [];

        if (dName.includes('gemini')) {
          for (const b of buckets) {
            if (b.window === 'weekly' || b.bucketId === 'gemini-weekly') {
              geminiWeekly = typeof b.remainingFraction === 'number' ? b.remainingFraction : 1.0;
              if (b.resetTime) geminiWeeklyResetTime = b.resetTime;
              if (b.description) geminiWeeklyDesc = b.description;
            }
            if (b.window === '5h' || b.bucketId === 'gemini-5h') {
              gemini5h = typeof b.remainingFraction === 'number' ? b.remainingFraction : 1.0;
              if (b.resetTime) geminiResetTime = b.resetTime;
              if (b.description) gemini5hDesc = b.description;
            }
          }
        } else if (dName.includes('claude') || dName.includes('gpt')) {
          for (const b of buckets) {
            if (b.window === 'weekly' || b.bucketId === '3p-weekly') {
              claudeWeekly = typeof b.remainingFraction === 'number' ? b.remainingFraction : 1.0;
              if (b.resetTime) claudeWeeklyResetTime = b.resetTime;
              if (b.description) claudeWeeklyDesc = b.description;
            }
            if (b.window === '5h' || b.bucketId === '3p-5h') {
              claude5h = typeof b.remainingFraction === 'number' ? b.remainingFraction : 1.0;
              if (b.resetTime) claudeResetTime = b.resetTime;
              if (b.description) claude5hDesc = b.description;
            }
          }
        }
      }

      const quota: AccountQuota = {
        geminiWeeklyFraction: Math.round(geminiWeekly * 10000) / 10000,
        gemini5hFraction: Math.round(gemini5h * 10000) / 10000,
        geminiResetTime: geminiResetTime || undefined,
        geminiWeeklyResetTime: geminiWeeklyResetTime || undefined,
        gemini5hDesc: gemini5hDesc || undefined,
        geminiWeeklyDesc: geminiWeeklyDesc || undefined,
        claudeWeeklyFraction: Math.round(claudeWeekly * 10000) / 10000,
        claude5hFraction: Math.round(claude5h * 10000) / 10000,
        claudeResetTime: claudeResetTime || undefined,
        claudeWeeklyResetTime: claudeWeeklyResetTime || undefined,
        claude5hDesc: claude5hDesc || undefined,
        claudeWeeklyDesc: claudeWeeklyDesc || undefined,
        lastCheckedAt: Date.now()
      };

      account.quota = quota;
      account.validationUrl = undefined;
      account.lastError = undefined;
      this.pool.updateAccount(account.id, { quota, validationUrl: undefined, lastError: undefined });

      // 同步拉取账号订阅等级 (v1internal:loadCodeAssist -> currentTier.id)
      // free-tier / pro-tier / ultra-tier 等；失败不影响额度结果。
      try {
        const tierRes = await request('https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist', {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${account.accessToken}`,
            'Content-Type': 'application/json',
            'User-Agent': 'antigravity/2.14.0',
            'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
          },
          body: JSON.stringify({}),
          headersTimeout: 30000,
          bodyTimeout: 30000,
          dispatcher
        });
        if (tierRes.statusCode === 200) {
          const tierData = (await tierRes.body.json()) as any;
          // 判定规则（实测 loadCodeAssist 返回结构）：
          // - 所有个人账号 currentTier.id 都返回 "free-tier"（Code Assist 层视角），
          //   真正的订阅等级在 paidTier.id: "g1-pro-tier"(Google AI Pro) / g1-ultra-tier(Ultra)。
          // - 付费订阅还带 paidTier.availableCredits[].creditType = "GOOGLE_ONE_AI"。
          // - currentTier.id 直接含 pro/ultra 的场景保留兼容（企业 standard-tier 等）。
          const currentId = String(tierData?.currentTier?.id || '').toLowerCase();
          const paidId = String(tierData?.paidTier?.id || '').toLowerCase();
          const hasAiCredit = Array.isArray(tierData?.paidTier?.availableCredits) &&
            tierData.paidTier.availableCredits.some((c: any) => c?.creditType === 'GOOGLE_ONE_AI');

          let tier: 'free' | 'pro' | 'ultra' = 'free';
          const probe = paidId || currentId;
          if (probe.includes('ultra')) tier = 'ultra';
          else if (probe.includes('pro') || hasAiCredit) tier = 'pro';
          else if (currentId.includes('ultra')) tier = 'ultra';
          else if (currentId.includes('pro')) tier = 'pro';

          account.tier = tier;
          this.pool.updateAccount(account.id, { tier });

          // 免费号输入上限远低于 PRO（实测 free 上限 < 972K，PRO = 1048576），大上下文会话必 400：
          // 仅保留 pro/ultra 参与调度，free 自动停用（保留凭据，随时可在管理面板重新启用）。
          if (tier === 'free' && account.status === 'active') {
            this.pool.updateAccount(account.id, { status: 'disabled', lastError: 'free 账号已自动停用：输入上限过低' });
            console.log(`[QuotaManager] ${account.email} 为 free 账号（输入上限过低），已自动停用。`);
          }
        }
      } catch { /* tier 是增强信息，拉取失败不打断额度流程 */ }

      return quota;
    } catch (err: any) {
      console.error(`[QuotaManager] Error fetching quota for ${account.email}:`, err.message);
      return null;
    }
  }

  /**
   * 刷新账号池内所有活跃账号的额度
   */
  public async fetchAllQuotas(): Promise<void> {
    const accounts = this.pool.getAllAccounts();
    for (const acc of accounts) {
      if (acc.status === 'dead' || acc.status === 'disabled') continue;
      await this.fetchQuotaForAccount(acc);
      // 适度防抖
      await new Promise(r => setTimeout(r, 600));
    }
  }

  /**
   * 启动后台额度自动轮询（默认每 3 分钟更新一次）
   */
  public startBackgroundWorker(intervalMs = 180000): void {
    if (this.pollTimer) return;
    console.log('[QuotaManager] Starting background quota monitor worker...');

    // 延迟 5 秒后首次运行
    setTimeout(() => {
      this.fetchAllQuotas().catch(() => {});
      // 启动同步：把当前已知为 free 且仍 active 的账号立即停用，
      // 免费号输入上限远低于 PRO（实测 free 上限 < 972K，PRO = 1048576），大上下文会话必 400。
      for (const acc of this.pool.getAllAccounts()) {
        if (acc.tier === 'free' && acc.status === 'active') {
          this.pool.updateAccount(acc.id, { status: 'disabled', lastError: 'free 账号已自动停用：输入上限过低' });
          console.log(`[QuotaManager] ${acc.email} 为 free 账号（输入上限过低），已自动停用。`);
        }
      }
      this.pool.saveAccounts();
    }, 5000);

    this.pollTimer = setInterval(() => {
      this.fetchAllQuotas().catch(() => {});
    }, intervalMs);
  }

  public stop(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }
}
