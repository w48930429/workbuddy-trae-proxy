import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import { Account, PoolStats, AccountStatus, AccountTier, RoutingStrategy } from '../types.js';
import { accountSupportsModelFamily, ThinkingEffort } from '../gateway/models.js';
import { cooldownMsFor, ClassifiedUpstreamError } from './error-policy.js';

const DATA_DIR = path.resolve(process.cwd(), 'data');
const ACCOUNTS_FILE = path.resolve(DATA_DIR, 'accounts.json');
const AFFINITY_FILE = path.resolve(DATA_DIR, 'affinity.json');

/** 单账号 RPM 窗口长度（滚动窗口）。 */
const RPM_WINDOW_MS = 60_000;

export interface AccountPoolOptions {
  /** 单账号每分钟请求上限，0/未设置表示不限。 */
  rpmLimit?: number;
  /** 是否要求每个账号必须有独立 proxyUrl。 */
  requireAccountProxy?: boolean;
  /** 全局默认出口代理（用于统计共用出口告警）。 */
  defaultProxyUrl?: string;
  /** 429 等错误的基准冷却时长。 */
  baseCooldownMs?: number;
}

export class AccountPool {
  private accounts: Map<string, Account> = new Map();
  private roundRobinCursor = 0;
  private startTime = Date.now();
  private totalRequestsServed = 0;
  private totalPromptTokens = 0;
  private totalCompletionTokens = 0;
  private totalCachedTokens = 0;
  private totalCacheCreationTokens = 0;
  private totalCacheReadTokens = 0;
  private totalTokens = 0;
  private hasTokenUsage = false;
  private isInternalSaving = false;
  private reloadDebounceTimer: NodeJS.Timeout | null = null;
  private routingStrategy: RoutingStrategy = 'zero-waste';
  private sessionBindings: Map<string, { accountId: string; lastUsedAt: number }> = new Map();
  private sessionCleanTimer: NodeJS.Timeout | null = null;
  private affinitySaveTimer: NodeJS.Timeout | null = null;
  // 每账号的滚动 60 秒请求时间戳，用于 RPM 限流（只在内存，不落盘）。
  private requestWindows: Map<string, number[]> = new Map();
  private rpmLimit = 0;
  private requireAccountProxy = false;
  private defaultProxyUrl?: string;
  private baseCooldownMs = 60_000;

  constructor(initialStrategy: RoutingStrategy = 'zero-waste', options: AccountPoolOptions = {}) {
    this.routingStrategy = initialStrategy;
    this.rpmLimit = Math.max(0, options.rpmLimit || 0);
    this.requireAccountProxy = options.requireAccountProxy === true;
    this.defaultProxyUrl = options.defaultProxyUrl;
    this.baseCooldownMs = Math.max(1_000, options.baseCooldownMs || 60_000);
    this.ensureDataDir();
    this.loadAccounts();
    this.loadAffinity();
    this.logProxyIsolation();
    this.setupFileWatcher();
    this.setupSessionCleaner();
  }

  private setupSessionCleaner(): void {
    this.sessionCleanTimer = setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const [key, val] of this.sessionBindings.entries()) {
        if (now - val.lastUsedAt > 30 * 60 * 1000) {
          this.sessionBindings.delete(key);
          changed = true;
        }
      }
      if (changed) this.scheduleAffinitySave();
    }, 10 * 60 * 1000);
  }

  private ensureDataDir(): void {
    if (!fs.existsSync(DATA_DIR)) {
      fs.mkdirSync(DATA_DIR, { recursive: true });
    }
  }

  private setupFileWatcher(): void {
    if (!fs.existsSync(ACCOUNTS_FILE)) {
      this.ensureDataDir();
      fs.writeFileSync(ACCOUNTS_FILE, '[]', 'utf-8');
    }

    try {
      fs.watch(ACCOUNTS_FILE, (eventType) => {
        if (this.isInternalSaving) return;
        if (this.reloadDebounceTimer) clearTimeout(this.reloadDebounceTimer);
        this.reloadDebounceTimer = setTimeout(() => {
          console.log('[AccountPool] Detected external change to accounts.json, reloading from disk...');
          this.loadAccounts();
        }, 300);
      });
    } catch (e: any) {
      console.error('[AccountPool] Error setting up accounts.json watcher:', e.message);
    }
  }

  public loadAccounts(): void {
    if (fs.existsSync(ACCOUNTS_FILE)) {
      try {
        const raw = fs.readFileSync(ACCOUNTS_FILE, 'utf-8');
        const list: Account[] = JSON.parse(raw);
        this.accounts.clear();
        for (const acc of list) {
          // 重置瞬态状态
          acc.activeConcurrency = 0;
          // 旧逻辑（已停用）：缺少/非法 tier 一律强制视为 free。
          // 这会把尚未探测等级的新导入账号误判为免费号，进而触发 QuotaManager 的 free 自动停用。
          // if (!acc.tier || !['free', 'pro', 'ultra'].includes(acc.tier)) {
          //   acc.tier = 'free';
          // }
          // 新逻辑：未知等级保持 undefined，等 loadCodeAssist 明确返回后再落库。
          if (acc.tier && !['free', 'pro', 'ultra'].includes(acc.tier)) {
            delete (acc as any).tier;
          }
          // 清理已过期的"账号不支持模型"临时黑名单，避免持久化文件越积越大
          if (acc.unsupportedModels) {
            const now = Date.now();
            for (const [model, until] of Object.entries(acc.unsupportedModels)) {
              if (!until || until <= now) delete acc.unsupportedModels[model];
            }
            if (Object.keys(acc.unsupportedModels).length === 0) delete acc.unsupportedModels;
          }
          this.accounts.set(acc.id, acc);
        }
        console.log(`[AccountPool] Loaded ${this.accounts.size} account(s) from storage.`);
      } catch (e) {
        console.error('[AccountPool] Failed to load accounts.json:', e);
      }
    } else {
      this.saveAccounts();
    }
  }

  /**
   * 会话粘滞绑定落盘，重启后仍把同一会话固定在同一账号。
   * 旧实现只保存在内存，网关重启后所有会话都会重新漂移，增加上游看到的
   * “同一对话跨账号/跨出口”的特征。
   */
  private loadAffinity(): void {
    try {
      if (!fs.existsSync(AFFINITY_FILE)) return;
      const raw = JSON.parse(fs.readFileSync(AFFINITY_FILE, 'utf-8'));
      if (!raw || typeof raw !== 'object') return;
      for (const [key, val] of Object.entries(raw as Record<string, any>)) {
        const accountId = String(val?.accountId || '');
        const lastUsedAt = Number(val?.lastUsedAt || 0);
        if (!key || !accountId || !this.accounts.has(accountId)) continue;
        this.sessionBindings.set(key, { accountId, lastUsedAt: Number.isFinite(lastUsedAt) ? lastUsedAt : 0 });
      }
      console.log(`[AccountPool] Loaded ${this.sessionBindings.size} session binding(s) from affinity.json.`);
    } catch (e: any) {
      console.warn(`[AccountPool] Failed to load affinity.json (ignored): ${e.message}`);
    }
  }

  private scheduleAffinitySave(): void {
    if (this.affinitySaveTimer) return;
    this.affinitySaveTimer = setTimeout(() => {
      this.affinitySaveTimer = null;
      this.saveAffinity();
    }, 500);
  }

  private saveAffinity(): void {
    try {
      const out: Record<string, { accountId: string; lastUsedAt: number }> = {};
      for (const [key, val] of this.sessionBindings.entries()) {
        if (this.accounts.has(val.accountId)) out[key] = val;
      }
      fs.writeFileSync(AFFINITY_FILE, JSON.stringify(out, null, 2), 'utf-8');
    } catch (e: any) {
      console.warn(`[AccountPool] Failed to save affinity.json: ${e.message}`);
    }
  }

  /** 单账号滚动 60 秒窗口是否还有额度。0 表示不启用 RPM 限制。 */
  private rpmOk(acc: Account, now: number): boolean {
    if (this.rpmLimit <= 0) return true;
    const stamps = (this.requestWindows.get(acc.id) || []).filter(t => now - t < RPM_WINDOW_MS);
    this.requestWindows.set(acc.id, stamps);
    return stamps.length < this.rpmLimit;
  }

  private noteRequest(accId: string, now: number): void {
    if (this.rpmLimit <= 0) return;
    const stamps = (this.requestWindows.get(accId) || []).filter(t => now - t < RPM_WINDOW_MS);
    stamps.push(now);
    this.requestWindows.set(accId, stamps);
  }

  /**
   * 出口隔离可见性：多账号共用同一个代理时给出告警。
   * 不阻断调度（避免现有未配代理的账号全部不可用），但把风险显式写进日志。
   */
  private logProxyIsolation(): void {
    const groups = new Map<string, string[]>();
    for (const acc of this.accounts.values()) {
      const key = (acc.proxyUrl || this.defaultProxyUrl || 'direct').trim();
      const list = groups.get(key) || [];
      list.push(acc.email || acc.id);
      groups.set(key, list);
    }
    for (const [key, emails] of groups.entries()) {
      if (emails.length <= 1) continue;
      const masked = key.replace(/\/\/[^@/]+@/, '//***@');
      console.warn(`[AccountPool] ${emails.length} 个账号共用出口 ${masked}: ${emails.join(', ')}（建议为每个账号配置独立 proxyUrl）`);
    }
    if (this.requireAccountProxy) {
      const noProxy = Array.from(this.accounts.values()).filter(a => !a.proxyUrl).length;
      if (noProxy > 0) {
        console.warn(`[AccountPool] requireAccountProxy=true，${noProxy} 个账号没有独立 proxyUrl，将不参与调度。`);
      }
    }
  }

  public saveAccounts(): void {
    try {
      this.isInternalSaving = true;
      const list = Array.from(this.accounts.values()).map(acc => ({
        ...acc,
        activeConcurrency: 0 // 持久化时不保留内存中的并发计数
      }));
      fs.writeFileSync(ACCOUNTS_FILE, JSON.stringify(list, null, 2), 'utf-8');
    } catch (e) {
      console.error('[AccountPool] Failed to save accounts.json:', e);
    } finally {
      setTimeout(() => { this.isInternalSaving = false; }, 500);
    }
  }

  public getAllAccounts(): Account[] {
    return Array.from(this.accounts.values());
  }

  /**
   * 判断指定模型家族在账号池里是否已全灭（所有 active 账号该家族 5h+weekly 额度均耗尽且未到重置时间）。
   * 无任何额度数据的账号视为未知，不参与"全灭"判定（避免误判）。
   */
  public isFamilyExhausted(family: 'claude' | 'gemini'): boolean {
    const now = Date.now();
    const actives = Array.from(this.accounts.values()).filter(a => a.status === 'active' && a.quota);
    if (actives.length === 0) return false; // 没有额度数据，谈不上"全灭"
    return actives.every(a => {
      const q = a.quota!;
      if (family === 'claude') {
        const weeklyOut = q.claudeWeeklyFraction <= 0.005 &&
          (!q.claudeWeeklyResetTime || new Date(q.claudeWeeklyResetTime).getTime() > now);
        const fiveHourOut = q.claude5hFraction <= 0.01 &&
          (!q.claudeResetTime || new Date(q.claudeResetTime).getTime() > now);
        return weeklyOut || fiveHourOut;
      }
      const weeklyOut = q.geminiWeeklyFraction <= 0.005 &&
        (!q.geminiWeeklyResetTime || new Date(q.geminiWeeklyResetTime).getTime() > now);
      const fiveHourOut = q.gemini5hFraction <= 0.01 &&
        (!q.geminiResetTime || new Date(q.geminiResetTime).getTime() > now);
      return weeklyOut || fiveHourOut;
    });
  }

  public getAccount(id: string): Account | undefined {
    return this.accounts.get(id);
  }

  public addAccount(params: {
    email: string;
    refreshToken: string;
    accessToken?: string;
    expiresInSeconds?: number;
    proxyUrl?: string;
    maxConcurrency?: number;
    tier?: AccountTier;
  }): Account {
    const id = `acc_${crypto.randomBytes(4).toString('hex')}`;
    const now = Date.now();
    const newAcc: Account = {
      id,
      email: params.email,
      refreshToken: params.refreshToken,
      accessToken: params.accessToken || '',
      accessTokenExpiresAt: params.accessToken ? now + (params.expiresInSeconds || 3600) * 1000 : 0,
      status: 'active',
      cooldownUntil: 0,
      activeConcurrency: 0,
      maxConcurrency: params.maxConcurrency || 5,
      totalRequests: 0,
      failedRequests: 0,
      proxyUrl: params.proxyUrl?.trim() || undefined,
      lastUsedAt: 0,
      createdAt: now,
      // 未知等级不再默认成 free（会被误判并自动停用），留空等待真实探测。
      tier: (params.tier && ['free', 'pro', 'ultra'].includes(params.tier)) ? params.tier : undefined
    };

    this.accounts.set(id, newAcc);
    this.saveAccounts();
    return newAcc;
  }

  public updateAccount(id: string, updates: Partial<Account>): Account | null {
    const acc = this.accounts.get(id);
    if (!acc) return null;

    Object.assign(acc, updates);
    if (updates.proxyUrl === undefined && 'proxyUrl' in updates) {
      delete (acc as any).proxyUrl;
    }
    this.saveAccounts();
    if ('proxyUrl' in updates) this.logProxyIsolation();
    return acc;
  }

  public removeAccount(id: string): boolean {
    const deleted = this.accounts.delete(id);
    if (deleted) {
      this.clearSessionBindings(id);
      this.saveAccounts();
    }
    return deleted;
  }

  public getRoutingStrategy(): RoutingStrategy {
    return this.routingStrategy;
  }

  public setRoutingStrategy(strategy: RoutingStrategy): void {
    console.log(`[AccountPool] Switching routing strategy from ${this.routingStrategy} to ${strategy}`);
    this.routingStrategy = strategy;
  }

  /**
   * 获取下一个可用的账号（支持零浪费智能调度、会话粘性防 Cache 穿透、临期抢救、流水线耗尽与故障转移）
   */
  public acquireAccount(
    excludeIds: Set<string> = new Set(),
    requiredFamily?: 'claude' | 'gemini',
    sessionKey?: string,
    inputModel?: string,
    effort: ThinkingEffort = 'high'
  ): Account | null {
    const now = Date.now();
    const candidates: Account[] = [];

    for (const acc of this.accounts.values()) {
      if (excludeIds.has(acc.id)) continue;

      // 检查是否解除冷却
      if (acc.status === 'cooldown' && now >= acc.cooldownUntil) {
        acc.status = 'active';
        acc.cooldownUntil = 0;
        console.log(`[AccountPool] Account ${acc.email} (${acc.id}) cooldown expired, returned to active.`);
      }

      // 筛选健康且未满并发的账号
      if (acc.status === 'active' && acc.activeConcurrency < acc.maxConcurrency) {
        // 出口隔离：要求独立代理时，没有 proxyUrl 的账号不参与调度。
        if (this.requireAccountProxy && !acc.proxyUrl) continue;
        // 单账号 RPM 限制：60 秒窗口内达到上限就跳过，避免同一个账号被短时间打爆。
        if (!this.rpmOk(acc, now)) continue;
        // 模型池能力过滤：账号清单已知且完全不支持该模型族时跳过。
        // 清单未知（新导入/尚未探测）一律放行，避免误锁账号。
        if (inputModel && !accountSupportsModelFamily(inputModel, effort, acc.availableModels)) {
          continue;
        }
        // 如果账号具备额度监控信息，检查对应模型组的额度是否已耗尽
        if (acc.quota && requiredFamily) {
          if (requiredFamily === 'claude') {
            const isWeeklyExhausted = (acc.quota.claudeWeeklyFraction <= 0.005);
            const is5hExhausted = (acc.quota.claude5hFraction <= 0.01);
            const resetTime = acc.quota.claudeWeeklyResetTime ? new Date(acc.quota.claudeWeeklyResetTime).getTime() : 0;
            const reset5hTime = acc.quota.claudeResetTime ? new Date(acc.quota.claudeResetTime).getTime() : 0;

            if (isWeeklyExhausted && resetTime > now) {
              continue;
            }
            if (is5hExhausted && reset5hTime > now) {
              continue;
            }
          } else if (requiredFamily === 'gemini') {
            const isWeeklyExhausted = (acc.quota.geminiWeeklyFraction <= 0.005);
            const is5hExhausted = (acc.quota.gemini5hFraction <= 0.01);
            const resetTime = acc.quota.geminiWeeklyResetTime ? new Date(acc.quota.geminiWeeklyResetTime).getTime() : 0;
            const reset5hTime = acc.quota.geminiResetTime ? new Date(acc.quota.geminiResetTime).getTime() : 0;

            if (isWeeklyExhausted && resetTime > now) {
              continue;
            }
            if (is5hExhausted && reset5hTime > now) {
              continue;
            }
          }
        }

        candidates.push(acc);
      }
    }

    // 兜底策略：只有在账号尚未拉取过额度信息 (!acc.quota) 时才允许兜底尝试，
    // 如果已知该家族额度已用尽 (如 claude5hFraction <= 0.01 且未到重置时间)，坚决不兜底送去撞 Google 429！
    if (candidates.length === 0 && requiredFamily) {
      for (const acc of this.accounts.values()) {
        if (!excludeIds.has(acc.id) && acc.status === 'active' && acc.activeConcurrency < acc.maxConcurrency) {
          if (this.requireAccountProxy && !acc.proxyUrl) continue;
          if (!this.rpmOk(acc, now)) continue;
          if (!acc.quota) {
            candidates.push(acc);
          }
        }
      }
    }

    if (candidates.length === 0) {
      return null;
    }

    let chosen: Account;

    if (this.routingStrategy === 'zero-waste') {
      // ==================== 策略 1: 零浪费智能融合调度 (Zero-Waste Hybrid) ====================

      // 阶段 1: 会话粘性优先 (Session Affinity) -> 保证同一会话命中 Google 上游 Prompt Cache，省下 80%~90% 上下文 Token
      let sessionStickyAccount: Account | null = null;
      if (sessionKey && this.sessionBindings.has(sessionKey)) {
        const bound = this.sessionBindings.get(sessionKey)!;
        const candidateAcc = candidates.find(c => c.id === bound.accountId);
        if (candidateAcc) {
          // 检查该粘性账号是否健康且配额没有到达警戒线 (< 3%)
          const isLowQuota = (requiredFamily === 'claude' && candidateAcc.quota && candidateAcc.quota.claude5hFraction < 0.03) ||
                             (requiredFamily === 'gemini' && candidateAcc.quota && candidateAcc.quota.gemini5hFraction < 0.03);
          // Cache affinity is valuable, but it must yield to a materially more
          // urgent reset. This prevents one recently-used account from being
          // pinned for 15 minutes while another account's weekly/5h quota is
          // about to expire and be discarded.
          const family5h = requiredFamily === 'claude' ? candidateAcc.quota?.claudeResetTime : candidateAcc.quota?.geminiResetTime;
          const familyWeekly = requiredFamily === 'claude' ? candidateAcc.quota?.claudeWeeklyResetTime : candidateAcc.quota?.geminiWeeklyResetTime;
          const bound5h = family5h ? new Date(family5h).getTime() - now : Infinity;
          const boundWeekly = familyWeekly ? new Date(familyWeekly).getTime() - now : Infinity;
          const hasMoreUrgentCandidate = candidates.some(candidate => {
            if (candidate.id === candidateAcc.id || !candidate.quota) return false;
            const reset5h = requiredFamily === 'claude' ? candidate.quota.claudeResetTime : candidate.quota.geminiResetTime;
            const resetWeekly = requiredFamily === 'claude' ? candidate.quota.claudeWeeklyResetTime : candidate.quota.geminiWeeklyResetTime;
            const candidate5h = reset5h ? new Date(reset5h).getTime() - now : Infinity;
            const candidateWeekly = resetWeekly ? new Date(resetWeekly).getTime() - now : Infinity;
            const fraction = requiredFamily === 'claude' ? candidate.quota.claude5hFraction : candidate.quota.gemini5hFraction;
            const imminent5h = candidate5h > 0 && candidate5h <= 45 * 60 * 1000 && fraction > 0.08;
            const weeklyUrgency = candidateWeekly > 0 && candidateWeekly + 24 * 60 * 60 * 1000 < boundWeekly;
            const fiveHourUrgency = candidate5h > 0 && candidate5h + 5 * 60 * 1000 < bound5h;
            return imminent5h || weeklyUrgency || fiveHourUrgency;
          });
          if (!isLowQuota && !hasMoreUrgentCandidate) {
            sessionStickyAccount = candidateAcc;
          }
        }
      }

      if (sessionStickyAccount) {
        chosen = sessionStickyAccount;
        this.sessionBindings.set(sessionKey!, { accountId: chosen.id, lastUsedAt: now });
        this.scheduleAffinitySave();
      } else {
        // 阶段 2: 临期抢救优先 (Time-to-Reset Expiration Harvesting)
        // 如果有账号在 45 分钟内即将重置 5 小时额度，且目前剩余额度 > 8%，必须优先用它，防止重置清零浪费！
        const imminentCandidates = candidates.filter(acc => {
          if (!acc.quota) return false;
          const resetTimeStr = (requiredFamily === 'claude' ? acc.quota.claudeResetTime : acc.quota.geminiResetTime);
          const fraction = (requiredFamily === 'claude' ? acc.quota.claude5hFraction : acc.quota.gemini5hFraction) ?? 1;
          if (!resetTimeStr || fraction <= 0.08) return false;
          const diffMs = new Date(resetTimeStr).getTime() - now;
          return diffMs > 0 && diffMs <= 45 * 60 * 1000; // 45分钟内即将重置
        });

        if (imminentCandidates.length > 0) {
          // 临近重置者中，剩余时间最近且额度最多的优先
          imminentCandidates.sort((a, b) => {
            const timeA = new Date(requiredFamily === 'claude' ? a.quota!.claudeResetTime! : a.quota!.geminiResetTime!).getTime();
            const timeB = new Date(requiredFamily === 'claude' ? b.quota!.claudeResetTime! : b.quota!.geminiResetTime!).getTime();
            if (Math.abs(timeA - timeB) > 5 * 60 * 1000) return timeA - timeB; // 时间差大于5分钟，先救最近的
            const fracA = (requiredFamily === 'claude' ? a.quota!.claude5hFraction : a.quota!.gemini5hFraction) ?? 0;
            const fracB = (requiredFamily === 'claude' ? b.quota!.claude5hFraction : b.quota!.gemini5hFraction) ?? 0;
            return fracB - fracA; // 额度多的先用
          });
          chosen = imminentCandidates[0];
          console.log(`[AccountPool] 🎯 [Zero-Waste 临期抢救] 优先调度账号 ${chosen.email} (5h额度即将重置，抢救剩余额度)`);
        } else {
          // 阶段 3: 流水线深度耗尽 (Sequential Pipeline)
          // 优先集中消耗当前的主力活跃账号（最近 15 分钟内被活跃调用的账号），直到其 5h 额度用透再切下一个，让前序账号拥有完整的 5 小时回满时间！
          candidates.sort((a, b) => {
            // 首先排除周额度极度危险的账号（< 3%）
            const aWeekly = (requiredFamily === 'claude' ? a.quota?.claudeWeeklyFraction : a.quota?.geminiWeeklyFraction) ?? 1;
            const bWeekly = (requiredFamily === 'claude' ? b.quota?.claudeWeeklyFraction : b.quota?.geminiWeeklyFraction) ?? 1;
            if (aWeekly <= 0.03 && bWeekly > 0.03) return 1;
            if (bWeekly <= 0.03 && aWeekly > 0.03) return -1;

            // 时间紧迫度优先于“15 分钟主力号”：先消耗更接近周重置的账号，
            // 防止周期结束时剩余额度被刷新浪费；周重置接近时再比较 5h 重置。
            const getResetDelay = (account: Account, weekly: boolean): number => {
              if (!account.quota) return Infinity;
              const resetTime = weekly
                ? (requiredFamily === 'claude' ? account.quota.claudeWeeklyResetTime : account.quota.geminiWeeklyResetTime)
                : (requiredFamily === 'claude' ? account.quota.claudeResetTime : account.quota.geminiResetTime);
              if (!resetTime) return Infinity;
              const timestamp = new Date(resetTime).getTime();
              const delay = timestamp - now;
              return Number.isFinite(delay) && delay > 0 ? delay : Infinity;
            };

            if (requiredFamily) {
              const aWeeklyReset = getResetDelay(a, true);
              const bWeeklyReset = getResetDelay(b, true);
              const weeklyResetGap = Math.abs(aWeeklyReset - bWeeklyReset);
              if (weeklyResetGap > 24 * 60 * 60 * 1000) {
                return aWeeklyReset - bWeeklyReset;
              }

              const a5hReset = getResetDelay(a, false);
              const b5hReset = getResetDelay(b, false);
              if (a5hReset !== b5hReset) {
                return a5hReset - b5hReset;
              }
            }

            // 多个同类候选时，先按并发数均衡。主力号优先不能压过
            // least-connections，否则第二个并发请求会继续锁在同一账号。
            if (a.activeConcurrency !== b.activeConcurrency) {
              return a.activeConcurrency - b.activeConcurrency;
            }

            // 并发相同且额度状态相近时，再保留主力号的缓存粘性。
            const aIsActiveDrain = (now - a.lastUsedAt < 15 * 60 * 1000) && ((requiredFamily === 'claude' ? a.quota?.claude5hFraction : a.quota?.gemini5hFraction) ?? 1) > 0.05;
            const bIsActiveDrain = (now - b.lastUsedAt < 15 * 60 * 1000) && ((requiredFamily === 'claude' ? b.quota?.claude5hFraction : b.quota?.gemini5hFraction) ?? 1) > 0.05;
            if (aIsActiveDrain && !bIsActiveDrain) return -1;
            if (bIsActiveDrain && !aIsActiveDrain) return 1;

            // 5h 额度充裕的优先接棒
            const a5h = (requiredFamily === 'claude' ? a.quota?.claude5hFraction : a.quota?.gemini5hFraction) ?? 1;
            const b5h = (requiredFamily === 'claude' ? b.quota?.claude5hFraction : b.quota?.gemini5hFraction) ?? 1;
            return b5h - a5h;
          });
          chosen = candidates[0];
        }

        // 建立新的会话粘性
        if (sessionKey) {
          this.sessionBindings.set(sessionKey, { accountId: chosen.id, lastUsedAt: now });
          this.scheduleAffinitySave();
        }
      }

    } else if (this.routingStrategy === 'sequential-drain') {
      // ==================== 策略 2: 流水线顺序耗尽模式 (Sequential Drain) ====================
      // 严格按照账号列表顺序，打满一个再切下一个
      candidates.sort((a, b) => {
        if (a.activeConcurrency === 0 && b.activeConcurrency > 0) return -1;
        if (b.activeConcurrency === 0 && a.activeConcurrency > 0) return 1;
        return a.createdAt - b.createdAt;
      });
      chosen = candidates[0];

    } else {
      // ==================== 策略 3: 传统均衡轮询模式 (Round-Robin) ====================
      candidates.sort((a, b) => {
        if (a.activeConcurrency !== b.activeConcurrency) {
          return a.activeConcurrency - b.activeConcurrency;
        }
        return a.lastUsedAt - b.lastUsedAt;
      });
      chosen = candidates[0];
    }

    chosen.activeConcurrency++;
    chosen.lastUsedAt = now;
    chosen.totalRequests++;
    this.totalRequestsServed++;
    this.noteRequest(chosen.id, now);

    return chosen;
  }

  /**
   * 释放借出的账号，并处理错误与状态更新
   */
  /** 记录某账号对某上游模型的临时不支持状态（404 或 200 下线提示）。 */
  public markModelUnsupported(accId: string, model: string, ttlMs = 30 * 60 * 1000): void {
    const acc = this.accounts.get(accId);
    if (!acc || !model) return;
    if (!acc.unsupportedModels) acc.unsupportedModels = {};
    acc.unsupportedModels[model] = Date.now() + ttlMs;
    this.saveAccounts();
  }

  public isModelUnsupported(accId: string, model: string): boolean {
    const acc = this.accounts.get(accId);
    if (!acc || !model || !acc.unsupportedModels) return false;
    const until = acc.unsupportedModels[model];
    if (!until) return false;
    if (Date.now() >= until) {
      delete acc.unsupportedModels[model];
      return false;
    }
    return true;
  }

  public releaseAccount(
    accId: string,
    error?: { code: number; message: string },
    family?: 'claude' | 'gemini',
    classified?: ClassifiedUpstreamError,
    retryAfterMs?: number
  ): void {
    const acc = this.accounts.get(accId);
    if (!acc) return;

    acc.activeConcurrency = Math.max(0, acc.activeConcurrency - 1);

    if (error) {
      acc.failedRequests++;
      acc.lastError = `[HTTP ${error.code}] ${error.message}`;

      if (classified) {
        // 新逻辑：按失败类型分级冷却（magpie 的 failure/rest 策略）。
        this.applyErrorPolicy(acc, classified, family, retryAfterMs);
      } else if (error.code === 429) {
        // 旧逻辑（保留）：没有分类信息时按 HTTP code 处理。
        // 判断是否属于特定模型族的额度用尽 (如 RESOURCE_EXHAUSTED / Quota exceeded / limit)
        const errMsg = error.message.toLowerCase();
        const isQuotaExhausted = errMsg.includes('resource_exhausted') ||
                                 errMsg.includes('quota') ||
                                 errMsg.includes('exhausted') ||
                                 errMsg.includes('limit');

        if (isQuotaExhausted && family === 'claude') {
          // 仅 Claude / 3p 额度耗尽：精准置零 Claude 5h 额度，账号全局状态依然保持 active，Gemini 请求不受任何影响！
          if (acc.quota) {
            acc.quota.claude5hFraction = 0.0;
          }
          console.warn(`[CircuitBreaker] 账号 ${acc.email} Claude/GPT 5h 额度耗尽，已标记 Claude 耗尽，账号保持 Active 供 Gemini 继续使用。`);
        } else if (isQuotaExhausted && family === 'gemini') {
          if (acc.quota) {
            acc.quota.gemini5hFraction = 0.0;
          }
          console.warn(`[CircuitBreaker] 账号 ${acc.email} Gemini 5h 额度耗尽，已标记 Gemini 耗尽，账号保持 Active 供 Claude 继续使用。`);
        } else {
          // 通用 RPM 频率限制（非特定模型额度用尽），账号进入常规冷却
          acc.status = 'cooldown';
          acc.cooldownUntil = Date.now() + this.baseCooldownMs;
          console.warn(`[CircuitBreaker] 账号 ${acc.email} 触发通用 429 限频，进入冷却 ${Math.round(this.baseCooldownMs / 1000)} 秒。`);
        }

        this.saveAccounts();
        this.clearSessionBindings(accId);
      } else if (error.code === 401) {
        // 凭证过期，需刷新
        console.warn(`[CircuitBreaker] Account ${acc.email} returned 401, token will be refreshed.`);
      } else if (error.code === 403) {
        // 旧逻辑：所有 403 一律 dead。新逻辑在有 classify 时区分 TOS / 地域 / 临时权限。
        acc.status = 'dead';
        console.error(`[CircuitBreaker] Account ${acc.email} marked as DEAD due to 403 Forbidden.`);
        this.clearSessionBindings(accId);
        this.saveAccounts();
      }
    }
  }

  /**
   * 按分类结果给账号设置冷却/终局状态。
   *
   * 与 magpie 对齐：
   * - policy_blocked（TOS/封禁）→ dead，且调用方不应把同一请求换给同厂商其他账号
   * - quota（某家族额度耗尽）→ 保留旧行为，只把该家族 5h 额度置零，账号继续服务另一家族
   * - location → 30 分钟冷却
   * - rate_limit → 60 秒起，尊重 Retry-After
   * - authentication → 5 分钟冷却
   * - transient → 30 秒冷却
   * - forbidden → 5 分钟冷却（不再一律 dead）
   * - invalid_request → 不冷却、不清会话绑定（请求本身的问题）
   */
  private applyErrorPolicy(
    acc: Account,
    classified: ClassifiedUpstreamError,
    family?: 'claude' | 'gemini',
    retryAfterMs?: number
  ): void {
    const kind = classified.kind;

    if (kind === 'policy_blocked') {
      acc.status = 'dead';
      console.error(`[CircuitBreaker] Account ${acc.email} marked DEAD (${classified.reason || 'TOS_VIOLATION'}): ${classified.message.slice(0, 160)}`);
      this.clearSessionBindings(acc.id);
      this.saveAccounts();
      return;
    }

    if (kind === 'quota' && family) {
      if (acc.quota) {
        if (family === 'claude') acc.quota.claude5hFraction = 0.0;
        else acc.quota.gemini5hFraction = 0.0;
      }
      console.warn(`[CircuitBreaker] 账号 ${acc.email} ${family} 5h 额度耗尽，账号保持 Active 供另一家族继续使用。`);
      this.saveAccounts();
      return;
    }

    const ms = cooldownMsFor(kind, { retryAfterMs, baseCooldownMs: this.baseCooldownMs });
    if (ms > 0) {
      acc.status = 'cooldown';
      acc.cooldownUntil = Date.now() + ms;
      console.warn(`[CircuitBreaker] 账号 ${acc.email} ${kind} → 冷却 ${Math.round(ms / 1000)}s`);
    }

    if (kind !== 'invalid_request') {
      this.clearSessionBindings(acc.id);
    }
    this.saveAccounts();
  }

  private clearSessionBindings(accountId: string): void {
    let changed = false;
    for (const [key, binding] of this.sessionBindings.entries()) {
      if (binding.accountId === accountId) {
        this.sessionBindings.delete(key);
        changed = true;
      }
    }
    if (changed) this.scheduleAffinitySave();
  }

  public getStats(): PoolStats {
    const now = Date.now();
    let active = 0;
    let cooldown = 0;
    let dead = 0;
    let currentConcurrency = 0;

    for (const acc of this.accounts.values()) {
      if (acc.status === 'cooldown' && now >= acc.cooldownUntil) {
        acc.status = 'active';
      }
      if (acc.status === 'active') active++;
      else if (acc.status === 'cooldown') cooldown++;
      else if (acc.status === 'dead') dead++;

      currentConcurrency += acc.activeConcurrency;
    }

    return {
      totalAccounts: this.accounts.size,
      activeAccounts: active,
      cooldownAccounts: cooldown,
      deadAccounts: dead,
      currentTotalConcurrency: currentConcurrency,
      totalRequestsServed: this.totalRequestsServed,
      totalPromptTokens: this.totalPromptTokens,
      totalCompletionTokens: this.totalCompletionTokens,
      totalCachedTokens: this.totalCachedTokens,
      totalCacheCreationTokens: this.totalCacheCreationTokens,
      totalCacheReadTokens: this.totalCacheReadTokens,
      totalTokens: this.totalTokens,
      hasTokenUsage: this.hasTokenUsage,
      uptimeSeconds: Math.floor((now - this.startTime) / 1000),
      routingStrategy: this.routingStrategy
    };
  }

  public recordTokenUsage(usage: {
    promptTokenCount?: number;
    candidatesTokenCount?: number;
    totalTokenCount?: number;
    cachedContentTokenCount?: number;
    cacheReadInputTokenCount?: number;
    cacheCreationInputTokenCount?: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
  }): void {
    const readNumber = (...values: unknown[]) => {
      const value = values.map(Number).find(Number.isFinite);
      return value ?? 0;
    };
    const prompt = readNumber(usage.promptTokenCount);
    const completion = readNumber(usage.candidatesTokenCount);
    const total = readNumber(usage.totalTokenCount) || prompt + completion;
    const cacheRead = readNumber(usage.cachedContentTokenCount, usage.cacheReadInputTokenCount, usage.cache_read_input_tokens);
    const cacheCreation = readNumber(usage.cacheCreationInputTokenCount, usage.cache_creation_input_tokens);
    const hasAnyUsage = [usage.promptTokenCount, usage.candidatesTokenCount, usage.totalTokenCount, usage.cachedContentTokenCount, usage.cacheReadInputTokenCount, usage.cacheCreationInputTokenCount, usage.cache_read_input_tokens, usage.cache_creation_input_tokens].some(value => Number.isFinite(Number(value)));
    if (!hasAnyUsage) return;
    this.totalPromptTokens += prompt;
    this.totalCompletionTokens += completion;
    this.totalCachedTokens += cacheRead;
    this.totalCacheReadTokens += cacheRead;
    this.totalCacheCreationTokens += cacheCreation;
    this.totalTokens += total;
    this.hasTokenUsage = true;
  }
}
