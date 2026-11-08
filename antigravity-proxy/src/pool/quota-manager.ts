import { request, ProxyAgent } from 'undici';
import { Account, AccountQuota, AppConfig } from '../types.js';
import { AccountPool } from './account-pool.js';

/**
 * 从 loadCodeAssist 响应解析订阅等级。
 *
 * 实测（2026-11）结构：
 * - 个人号 currentTier.id 恒为 "free-tier"、name 为 "Antigravity"；
 * - 付费订阅在 paidTier：id="g1-pro-tier"、name="Google AI Pro"，
 *   免费号则是 "Antigravity Starter Quota"（或不存在 paidTier）。
 *
 * 返回值 null 表示本次响应无法给出明确结论（字段缺失/未知命名），
 * 调用方必须保留账号原有 tier，绝不能默认按 free 处理并停用账号。
 */
export function resolveTierFromCodeAssist(tierData: any): 'free' | 'pro' | 'ultra' | null {
  if (!tierData || typeof tierData !== 'object') return null;

  const currentTier = tierData.currentTier || {};
  const paidTier = tierData.paidTier || {};

  const currentId = String(currentTier.id || '').toLowerCase();
  const currentName = String(currentTier.name || '').toLowerCase();
  const paidId = String(paidTier.id || '').toLowerCase();
  const paidName = String(paidTier.name || '').toLowerCase();
  const hasAiCredit = Array.isArray(paidTier.availableCredits) &&
    paidTier.availableCredits.some((c: any) => c?.creditType === 'GOOGLE_ONE_AI');

  // paidTier 存在时优先采信（id 与 name 双通道，兼容上游改字段）
  if (paidId || paidName) {
    const paidProbe = `${paidId} ${paidName}`;
    if (paidProbe.includes('ultra')) return 'ultra';
    if (paidProbe.includes('pro') || hasAiCredit) return 'pro';
    if (paidId.includes('free') || paidName.includes('starter') || paidName.includes('free')) return 'free';
    return null;
  }

  // 无 paidTier 时退回 currentTier（企业/特殊租户可能直接给出标准付费档）
  const currentProbe = `${currentId} ${currentName}`;
  if (currentProbe.includes('ultra')) return 'ultra';
  if (currentProbe.includes('pro')) return 'pro';
  if (currentId.includes('free') || currentName.includes('starter') || currentName.includes('free')) return 'free';
  return null;
}

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
          // 判定规则见 resolveTierFromCodeAssist：
          // - 个人号 currentTier.id 恒为 "free-tier"，真正的订阅等级在 paidTier（id + name）；
          // - 返回 null 表示本次响应无法判定，保留原 tier，避免把 PRO 误判为 free。
          const tier = resolveTierFromCodeAssist(tierData);

          if (tier) {
            account.tier = tier;
            this.pool.updateAccount(account.id, { tier });

            // 免费号输入上限远低于 PRO（实测 free 上限 < 972K，PRO = 1048576），大上下文会话必 400：
            // 仅保留 pro/ultra 参与调度，确认 free 才自动停用（保留凭据，随时可在管理面板重新启用）。
            if (tier === 'free' && account.status === 'active') {
              this.pool.updateAccount(account.id, {
                status: 'disabled',
                lastError: 'free 账号已自动停用：输入上限过低',
                autoDisabledForFree: true
              });
              console.log(`[QuotaManager] ${account.email} 为 free 账号（输入上限过低），已自动停用。`);
            }

            // 自愈：此前因 tier 未知/误判被自动停用的账号，复检确认是 pro/ultra 后自动恢复，
            // 否则停用账号永远拿不到调度机会，形成不可逆死锁。
            // 用 autoDisabledForFree 标记区分“自动停用”与“用户手动停用”，不误启用后者。
            if (tier !== 'free' && account.status === 'disabled' && account.autoDisabledForFree === true) {
              this.pool.updateAccount(account.id, { status: 'active', lastError: undefined, autoDisabledForFree: undefined });
              console.log(`[QuotaManager] ${account.email} 复检确认为 ${tier} 账号，已自动恢复启用。`);
            }
          }
        }
      } catch { /* tier 是增强信息，拉取失败不打断额度流程 */ }

      // 模型池能力探测：新池用 Opus/Sonnet 5.5，旧池仍用 4.6/4-6（6 小时内不重复拉取）。
      await this.syncAvailableModels(account);

      return quota;
    } catch (err: any) {
      console.error(`[QuotaManager] Error fetching quota for ${account.email}:`, err.message);
      return null;
    }
  }

  /**
   * 拉取账号在上游真正可用的模型清单（v1internal:fetchAvailableModels）。
   *
   * 实测（2026-11）账号按模型池灰度分化：
   * - 旧池：19 个模型，Opus 只有 claude-opus-4-6-thinking；
   * - 新池：23 个模型，Opus 4.6 已下线，只有 claude-opus-5-5-*。
   * 清单用于执行器按账号选择实际上游模型；拉取失败不影响额度流程。
   */
  public async syncAvailableModels(account: Account): Promise<void> {
    const now = Date.now();
    const CACHE_MS = 6 * 60 * 60 * 1000;
    if (account.availableModelsAt && now - account.availableModelsAt < CACHE_MS) return;
    if (!account.accessToken || now >= account.accessTokenExpiresAt) return;

    const effectiveProxy = account.proxyUrl || this.config?.defaultProxyUrl || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
    const dispatcher = effectiveProxy ? new ProxyAgent({ uri: effectiveProxy, connect: { timeout: 30000 } }) : undefined;

    try {
      const res = await request('https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels', {
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

      if (res.statusCode !== 200) {
        await res.body.text();
        return;
      }

      const data = (await res.body.json()) as any;
      const modelIds = Object.keys(data?.models || {});
      if (!modelIds.length) return;

      const modelPool: 'legacy' | 'new' = modelIds.some(id => id.includes('opus-5-5')) ? 'new' : 'legacy';
      account.availableModels = modelIds;
      account.availableModelsAt = now;
      account.modelPool = modelPool;
      this.pool.updateAccount(account.id, {
        availableModels: modelIds,
        availableModelsAt: now,
        modelPool
      });
      console.log(`[QuotaManager] ${account.email} 模型池=${modelPool} (${modelIds.length} 个模型)`);
    } catch { /* 能力探测是增强信息，失败不影响额度流程 */ }
  }

  /**
   * 刷新账号池内所有活跃账号的额度
   */
  public async fetchAllQuotas(): Promise<void> {
    const accounts = this.pool.getAllAccounts();
    for (const acc of accounts) {
      // dead 账号凭据已明确失效，不再消耗上游请求；
      // disabled 必须继续复检，否则被误停的账号永远无法重新识别为 pro（死锁）。
      if (acc.status === 'dead') continue;
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
      // 旧逻辑（已停用）：启动时把“当前已知为 free”且仍 active 的账号立即停用。
      // 该兜底会在账号等级尚未真实探测（导入时 tier 缺省为 free）时抢先误杀 PRO 账号，
      // 且停用后不再复检，形成永久死锁。现在统一改为：只有 loadCodeAssist 明确返回 free 才停用。
      // for (const acc of this.pool.getAllAccounts()) {
      //   if (acc.tier === 'free' && acc.status === 'active') {
      //     this.pool.updateAccount(acc.id, { status: 'disabled', lastError: 'free 账号已自动停用：输入上限过低' });
      //     console.log(`[QuotaManager] ${acc.email} 为 free 账号（输入上限过低），已自动停用。`);
      //   }
      // }
      this.fetchAllQuotas().catch(() => {});
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
