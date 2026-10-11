import { request, ProxyAgent } from 'undici';
import { Account, AppConfig, GoogleCloudCodePayload } from '../types.js';
import { AccountPool } from './account-pool.js';
import { TokenRefresher } from './refresher.js';

import { QuotaManager } from './quota-manager.js';

export interface UpstreamExecutionResult {
  statusCode: number;
  headers: Record<string, any>;
  bodyStream: any;
  accountUsed: Account;
}

export interface UpstreamTokenCountResult {
  totalTokens: number;
  accountUsed: Account;
}

/** An upstream response that must be returned to the client, never retried on another account. */
export class UpstreamRequestError extends Error {
  constructor(
    public readonly statusCode: number,
    public readonly upstreamBody: string
  ) {
    super(`Google Upstream returned HTTP ${statusCode}: ${upstreamBody}`);
    this.name = 'UpstreamRequestError';
  }
}

export class ContextOverflowError extends UpstreamRequestError {
  constructor(statusCode: number, upstreamBody: string) {
    super(statusCode, upstreamBody);
    this.name = 'ContextOverflowError';
  }
}

function isContextOverflow(statusCode: number, body: string): boolean {
  return statusCode === 400 && (
    /input token count exceeds the maximum number of tokens/i.test(body) ||
    /maximum number of tokens allowed/i.test(body) ||
    /CONTEXT_LENGTH_EXCEEDED/i.test(body) ||
    /prompt is too long/i.test(body)
  );
}

/**
 * 跨家族静默改道开关（当前关闭）。
 *
 * 开启时的行为：目标模型家族（gemini / claude）全部账号不可用（额度耗尽、冷却中、
 * 无候选）而另一个家族尚有额度时，本执行器会把请求体里的模型**改写成另一家族的固定
 * 模型**（gemini → claude-sonnet-4-6，claude → gemini-3.8-flash-high），用另一家族的
 * 账号代答，响应照常返回且**仍回显客户端请求的模型名**，客户端无从察觉。
 *
 * 关闭原因（2026-10-11 实证）：
 *   1. 会静默吞掉另一家族的额度 —— 请求 gemini 实际扣的是 Claude 额度；
 *   2. 两个家族的分词、单次输入上限、工具调用与推理字段都不同，输出特性会悄悄改变；
 *   3. 排查被带偏 —— 实测 663,163 tokens 的 gemini 请求返回 200（由 claude 答出），
 *      而同一时刻日志记录的却是"该请求在 gemini 上上下文超限"，只看响应无法分辨。
 *
 * 关闭后的行为：目标家族无可用账号时，按真实原因报错 ——
 *   上下文超限 → 400（提示客户端压缩上下文）；额度耗尽 → 503（附最近回满倒计时）。
 * 需要恢复该行为时把此常量改为 true 即可（分支代码原样保留）。
 */
const CROSS_FAMILY_FALLBACK_ENABLED = false;

export class RequestExecutor {
  constructor(
    private pool: AccountPool,
    private refresher: TokenRefresher,
    private config: AppConfig,
    private quotaManager?: QuotaManager
  ) {}

  /** Uses Google's countTokens endpoint so Claude Code can run its own compaction logic. */
  public async countTokens(
    payload: GoogleCloudCodePayload,
    maxRetries = 3,
    sessionKey?: string
  ): Promise<UpstreamTokenCountResult> {
    const triedAccountIds = new Set<string>();
    const model = (payload.model || '').toLowerCase();
    const family: 'claude' | 'gemini' = (model.includes('claude') || model.includes('opus') || model.includes('sonnet') || model.includes('gpt') || model.includes('oss'))
      ? 'claude'
      : 'gemini';
    const countUrl = this.config.upstreamUrl
      .replace(/streamGenerateContent/i, 'countTokens')
      .replace(/[?&]alt=sse\b/i, '')
      .replace(/[?&]$/, '');

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      const account = this.pool.acquireAccount(triedAccountIds, family, sessionKey);
      if (!account) {
        // 计数失败不阻断生成：上层已对 countTokens 异常做了降级处理。
        // 但给出更精确的提示（该家族额度是否真的全灭）。
        if (this.pool.isFamilyExhausted(family)) {
          const altFamily = family === 'gemini' ? 'claude' : 'gemini';
          const altOk = !this.pool.isFamilyExhausted(altFamily);
          throw new Error(`No available ${family} account for token counting. ${family} 额度已全灭${altOk ? `，但 ${altFamily} 仍有额度（生成请求会自动切到 ${altFamily} 模型）` : ''}。`);
        }
        throw new Error(`No available ${family} account for token counting.`);
      }
      triedAccountIds.add(account.id);

      if (!account.accessToken || Date.now() >= account.accessTokenExpiresAt) {
        const ok = await this.refresher.refreshSingleAccount(account);
        if (!ok) {
          this.pool.releaseAccount(account.id, { code: 401, message: 'Token refresh failed during countTokens' }, family);
          continue;
        }
      }

      try {
        const effectiveProxy = account.proxyUrl || this.config.defaultProxyUrl || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
        const dispatcher = effectiveProxy ? new ProxyAgent({ uri: effectiveProxy, connect: { timeout: 60000 } }) : undefined;
        // CloudCode deployments have used both envelopes over time. Count the
        // exact generation payload first, then the bare request envelope as a
        // compatibility fallback. Returning the larger successful result avoids
        // silently under-counting wrappers/tools that one endpoint ignores.
        const requestBodies = [JSON.stringify(payload), JSON.stringify({ request: payload.request })];
        let countedTokens: number | undefined;
        let lastFailure = '';
        for (const requestBody of requestBodies) {
          const res = await request(countUrl, {
            method: 'POST',
            headers: {
              'Authorization': `Bearer ${account.accessToken}`,
              'Content-Type': 'application/json',
              'User-Agent': 'antigravity/2.14.0',
              'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
            },
            body: requestBody,
            headersTimeout: 120000,
            bodyTimeout: 120000,
            dispatcher
          });
          const body = await res.body.text();
          if (res.statusCode >= 200 && res.statusCode < 300) {
            const parsed = JSON.parse(body);
            const totalTokens = Number(
              parsed.totalTokens ?? parsed.total_tokens ?? parsed.promptTokenCount ?? parsed.usageMetadata?.promptTokenCount
            );
            if (Number.isFinite(totalTokens)) {
              countedTokens = Math.max(countedTokens ?? 0, totalTokens);
              continue;
            }
            lastFailure = `countTokens response did not include totalTokens: ${body.slice(0, 300)}`;
            continue;
          }
          lastFailure = `countTokens upstream HTTP ${res.statusCode}: ${body}`;
          if (res.statusCode === 401 || res.statusCode === 403) {
            this.pool.releaseAccount(account.id);
            throw new UpstreamRequestError(res.statusCode, body);
          }
        }
        if (countedTokens !== undefined) {
          this.pool.releaseAccount(account.id);
          return { totalTokens: countedTokens, accountUsed: account };
        }
        this.pool.releaseAccount(account.id);
        throw new Error(lastFailure || 'countTokens request failed');
      } catch (err: any) {
        if (err instanceof UpstreamRequestError) throw err;
        this.pool.releaseAccount(account.id, { code: 500, message: `countTokens network error: ${err.message}` }, family);
        if (attempt === maxRetries) throw err;
      }
    }
    throw new Error('Failed to count tokens after retries.');
  }

  /**
   * 携带重试与熔断的执行器 (支持模型族群额度感知与智能无感换号)
   */
  public async executeWithRetry(
    payload: GoogleCloudCodePayload,
    maxRetries = 3,
    sessionKey?: string
  ): Promise<UpstreamExecutionResult> {
    const triedAccountIds = new Set<string>();
    // 记录本轮出现过的"上下文超限"错误。全池候选都因上下文超限而失败时，必须把
    // 这个真实原因回报给客户端（400 + 让客户端压缩上下文），而不是伪装成"额度耗尽"
    // 的 503 —— 那正是"面板显示还有额度、请求却 503"这种自相矛盾现象的来源。
    let lastOverflowError: ContextOverflowError | null = null;
    // 上游 503（MODEL_CAPACITY_EXHAUSTED）的真实报文：三个账号都被挡下时如实回报，
    // 而不是给出笼统的 "Failed to fulfill request after 3 attempts..."。
    let lastCapacityError: UpstreamRequestError | null = null;
    const model = (payload.model || '').toLowerCase();
    const family: 'claude' | 'gemini' = (model.includes('claude') || model.includes('opus') || model.includes('sonnet') || model.includes('gpt') || model.includes('oss'))
      ? 'claude'
      : 'gemini';

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      const account = this.pool.acquireAccount(triedAccountIds, family, sessionKey);

      if (!account) {
        // 跨家族自动切换：请求的模型家族已全灭，但另一个家族还有额度时，
        // 自动把请求改道到另一个家族的模型，而不是直接报错。
        // 详见 CROSS_FAMILY_FALLBACK_ENABLED 处注释：当前已关闭该行为。
        const altFamily: 'claude' | 'gemini' = family === 'gemini' ? 'claude' : 'gemini';
        const altHealthy = !this.pool.isFamilyExhausted(altFamily);
        const hasAltModel = altFamily === 'claude' ? true : true; // 两个家族都有可用底座
        if (CROSS_FAMILY_FALLBACK_ENABLED && altHealthy && hasAltModel) {
          const fallbackModel = altFamily === 'claude' ? 'claude-sonnet-4-6' : 'gemini-3.8-flash-high';
          console.warn(`[Executor] All ${family} quotas exhausted, auto-switching to ${altFamily} model: ${fallbackModel}`);
          payload.model = fallbackModel;
          // 用新家族重新选择账号（clear tried 集合，因为是另一个家族的调度）
          triedAccountIds.clear();
          const altAccount = this.pool.acquireAccount(triedAccountIds, altFamily, sessionKey);
          if (altAccount) {
            triedAccountIds.add(altAccount.id);
            if (!altAccount.accessToken || Date.now() >= altAccount.accessTokenExpiresAt) {
              const ok = await this.refresher.refreshSingleAccount(altAccount);
              if (!ok) {
                this.pool.releaseAccount(altAccount.id, { code: 401, message: 'Token refresh failed during cross-family fallback' }, altFamily);
                throw new Error(`Cross-family fallback failed: token refresh error`);
              }
            }
            const account = altAccount;
            try {
              const effectiveProxy = altAccount.proxyUrl || this.config.defaultProxyUrl || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
              const dispatcher = effectiveProxy ? new ProxyAgent({ uri: effectiveProxy, connect: { timeout: 60000 } }) : undefined;
              const res = await request(this.config.upstreamUrl, {
                method: 'POST',
                headers: {
                  'Authorization': `Bearer ${altAccount.accessToken}`,
                  'Content-Type': 'application/json',
                  'User-Agent': 'antigravity/2.14.0',
                  'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
                },
                body: JSON.stringify(payload),
                headersTimeout: 600000,
                bodyTimeout: 600000,
                dispatcher
              });
              if (res.statusCode >= 200 && res.statusCode < 300) {
                return { statusCode: res.statusCode, headers: res.headers, bodyStream: res.body, accountUsed: altAccount };
              }
              const errBody = await res.body.text();
              console.warn(`[Executor] 跨家族兜底 ${altFamily} 请求失败 HTTP ${res.statusCode}: ${errBody.slice(0, 180)}`);
              if (isContextOverflow(res.statusCode, errBody)) {
                lastOverflowError = new ContextOverflowError(res.statusCode, errBody);
              }
              this.pool.releaseAccount(altAccount.id, { code: res.statusCode, message: errBody }, altFamily);
            } catch (err: any) {
              this.pool.releaseAccount(altAccount.id, { code: 500, message: `Cross-family fallback network error: ${err.message}` }, altFamily);
            }
          }
        }
        // 真实原因是上下文超限（全部候选账号 + 跨家族兜底都是"prompt 太长"）时，
        // 回报 400 让客户端压缩上下文，绝不能伪装成"额度耗尽"的 503。
        if (lastOverflowError) {
          console.warn('[Executor] 全部候选账号均因上下文超限失败，回报 400 让客户端压缩上下文。');
          throw lastOverflowError;
        }
        if (family === 'claude') {
          let nearestDiff = Infinity;
          for (const acc of this.pool.getAllAccounts()) {
            if (acc.quota?.claudeResetTime) {
              const diff = new Date(acc.quota.claudeResetTime).getTime() - Date.now();
              if (diff > 0 && diff < nearestDiff) {
                nearestDiff = diff;
              }
            }
          }
          const timeHint = nearestDiff !== Infinity
            ? ` (最近回满倒计时: ${Math.floor(nearestDiff / 3600000)}小时${Math.floor((nearestDiff % 3600000) / 60000)}分)`
            : '';
          throw new Error(`当前账号池所有账号的 Claude/GPT 5小时额度均已耗尽${timeHint}。Gemini 额度依然充足可用，建议在客户端切换为 gemini-3.8-flash 等模型，或等待额度自动回满。`);
        } else if (family === 'gemini') {
          let nearestDiff = Infinity;
          for (const acc of this.pool.getAllAccounts()) {
            if (acc.quota?.geminiResetTime) {
              const diff = new Date(acc.quota.geminiResetTime).getTime() - Date.now();
              if (diff > 0 && diff < nearestDiff) {
                nearestDiff = diff;
              }
            }
          }
          const timeHint = nearestDiff !== Infinity
            ? ` (最近回满倒计时: ${Math.floor(nearestDiff / 3600000)}小时${Math.floor((nearestDiff % 3600000) / 60000)}分)`
            : '';
          throw new Error(`当前账号池所有账号的 Gemini 5小时额度均已耗尽${timeHint}。Claude / GPT 额度依然充足可用，建议在客户端切换为 claude-3-7-sonnet 等模型，或等待额度自动回满。`);
        }
        throw new Error('All accounts in the pool are busy, in cooldown, or exhausted.');
      }

      triedAccountIds.add(account.id);

      // 检查 Token 是否需要刷新
      if (!account.accessToken || Date.now() >= account.accessTokenExpiresAt) {
        const ok = await this.refresher.refreshSingleAccount(account);
        if (!ok) {
          this.pool.releaseAccount(account.id, { code: 401, message: 'Initial token refresh failed' }, family);
          continue; // 换下一个号重试
        }
      }

      try {
        const effectiveProxy = account.proxyUrl || this.config.defaultProxyUrl || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
        const dispatcher = effectiveProxy ? new ProxyAgent({
          uri: effectiveProxy,
          connect: {
            timeout: 60000 // 放宽底层 TCP/TLS 握手及代理建连超时至 60 秒，完美承载几兆的大 Prompt 上下文
          }
        }) : undefined;
        const upstreamHeaders: Record<string, string> = {
          'Authorization': `Bearer ${account.accessToken}`,
          'Content-Type': 'application/json',
          'User-Agent': 'antigravity/2.14.0',
          'x-goog-api-client': 'gl-node/22.7.0 grpc-web/1.0.0'
        };

        const res = await request(this.config.upstreamUrl, {
          method: 'POST',
          headers: upstreamHeaders,
          body: JSON.stringify(payload),
          headersTimeout: 600000,
          bodyTimeout: 600000,
          dispatcher
        });

        // 成功状态
        if (res.statusCode >= 200 && res.statusCode < 300) {
          return {
            statusCode: res.statusCode,
            headers: res.headers,
            bodyStream: res.body,
            accountUsed: account
          };
        }

        // 处理 429 限频 / 额度耗尽
        if (res.statusCode === 429) {
          const errBody = await res.body.text();
          console.warn(`[Executor] 账号 ${account.email} 触发 429 限频/额度枯竭 (第 ${attempt}/${maxRetries} 次尝试)，正在无感自动换号...:`, errBody.substring(0, 120));
          this.pool.releaseAccount(account.id, { code: 429, message: errBody }, family);
          if (this.quotaManager) {
            this.quotaManager.fetchQuotaForAccount(account).catch(() => {});
          }
          // 自动进入下一轮循环，换取下一个可用账号
          continue;
        }

        // 处理 401 Token 失效
        if (res.statusCode === 401) {
          const errBody = await res.body.text();
          console.warn(`[Executor] Account ${account.email} hit 401 Unauthorized:`, errBody);
          // 尝试立即刷新 Token 一次
          const refreshOk = await this.refresher.refreshSingleAccount(account);
          if (refreshOk) {
            // 刷新成功，重试当前账号
            triedAccountIds.delete(account.id);
          } else {
            this.pool.releaseAccount(account.id, { code: 401, message: 'Token refresh failed' });
          }
          continue;
        }

        // 处理 403 封禁或无权限
        if (res.statusCode === 403) {
          const errBody = await res.body.text();
          console.error(`[Executor] Account ${account.email} hit 403 Forbidden:`, errBody);
          this.pool.releaseAccount(account.id, { code: 403, message: errBody });
          continue;
        }

        // 处理 503 容量不足智能降级转移
        if (res.statusCode === 503) {
          const errBody = await res.body.text();
          console.warn(`[Executor] Google Upstream 503:`, errBody);
          // 503 是上游"本次真的没容量"，不是账号问题。换号可能有效（不同账号走不同后端），
          // 但若三个账号都被 503 挡下，必须把这个真实原因回报客户端 —— 否则只剩一句
          // "Failed to fulfill request after 3 attempts with rotating accounts"，
          // 让人误以为本地调度坏了（2026-10-11 实测：claude-opus-4-6-thinking 上游返回
          // MODEL_CAPACITY_EXHAUSTED / "No capacity available for model ..."）。
          lastCapacityError = new UpstreamRequestError(res.statusCode, errBody);
          if (errBody.includes('MODEL_CAPACITY_EXHAUSTED') && payload.model?.includes('gpt-oss')) {
            console.warn(`[Executor] GPT-OSS capacity exhausted, automatically falling back to gemini-3.8-flash-high...`);
            payload.model = 'gemini-3.8-flash-high';
            triedAccountIds.delete(account.id);
            this.pool.releaseAccount(account.id);
            continue;
          }
          this.pool.releaseAccount(account.id, { code: 503, message: errBody });
          continue;
        }

        // 400 分两种：上下文超限（换号可能有用——不同账号/tier 的 per-request 输入上限不同）
        // 和其它请求不合法（换号无意义）。上下文超限时先换号重试，全部候选都超限才报给客户端。
        const errBody = await res.body.text();
        this.pool.releaseAccount(account.id);
        if (isContextOverflow(res.statusCode, errBody)) {
          lastOverflowError = new ContextOverflowError(res.statusCode, errBody);
          if (attempt < maxRetries) {
            console.warn(`[Executor] 账号 ${account.email} 返回上下文超限（${payload.model}），尝试换下一个账号（不同账号/tier 输入上限不同）...`);
            continue;
          }
          // 已无未试过的候选账号：跨家族按需降档也没意义（两个家族输入上限同量级），报给客户端让其压缩
          throw lastOverflowError;
        }
        throw new UpstreamRequestError(res.statusCode, errBody);

      } catch (err: any) {
        // 协议错误已完成账号释放，不允许被 catch 当成网络异常后再换号。
        if (err instanceof UpstreamRequestError) {
          throw err;
        }
        // 网络层异常（如 Connect Timeout、Socket 掉线等），自动换号继续尝试，不立即中断
        console.error(`[Executor] Network or connection error for account ${account.email} (attempt ${attempt}/${maxRetries}):`, err.message);
        this.pool.releaseAccount(account.id, { code: 500, message: `Network error: ${err.message}` });
        if (attempt === maxRetries) {
          throw err;
        }
      }
    }

    // 三个账号都试过仍失败：若原因是"上下文超限"或"上游没容量"，把上游原话回报给客户端，
    // 不要用笼统的 rotating-accounts 文案掩盖（那会让人以为本地调度坏了）。
    if (lastOverflowError) throw lastOverflowError;
    if (lastCapacityError) throw lastCapacityError;
    throw new Error(`Failed to fulfill request after ${maxRetries} attempts with rotating accounts.`);
  }
}
