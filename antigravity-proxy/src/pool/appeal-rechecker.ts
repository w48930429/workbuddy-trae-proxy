import { request, ProxyAgent } from 'undici';
import { Account, AppConfig } from '../types.js';
import { AccountPool } from './account-pool.js';

/**
 * 申诉自动复检器（AppealRechecker）
 *
 * 背景：账号被 Google 判定 TOS 封禁时，状态为 dead，此时系统不会主动探测可用性，
 * 导致用户手动提交申诉后状态长期卡在 dead，无法自动恢复，也无法纳入可用调度池。
 *
 * 本组件定时对「存在申诉链接（appealUrl）」的 dead 账号做轻量可用性复检：
 * - 复检请求成功（2xx）→ 自动将账号恢复为 active，清空 lastError，使其重新进入调度池；
 * - 复检仍返回 TOS 封禁（403）→ 保持 dead，继续等待申诉生效。
 *
 * 仅做轻量可用性探测（token 刷新探测），不触发完整业务请求，避免消耗额度。
 */
export class AppealRechecker {
  private timer: NodeJS.Timeout | null = null;
  private static readonly DEFAULT_INTERVAL_MS = 5 * 60 * 1000; // 默认 5 分钟复检一次
  /**
   * 复检用的 token 请求函数，可注入以便测试（默认使用 undici 真实请求）。
   * 返回值包含 statusCode 与可读取原始响应的 text 方法。
   */
  private tokenRequestFn: (params: URLSearchParams, proxy?: string) => Promise<{ statusCode: number; text: () => Promise<string> }> = defaultTokenRequest;

  constructor(
    private pool: AccountPool,
    private config: AppConfig,
    private intervalMs: number = AppealRechecker.DEFAULT_INTERVAL_MS,
    tokenRequestFn?: AppealRechecker['tokenRequestFn']
  ) {
    if (tokenRequestFn) {
      this.tokenRequestFn = tokenRequestFn;
    }
  }

  public start(): void {
    console.log(`[AppealRechecker] Starting appeal auto-recheck worker (interval=${this.intervalMs}ms)...`);
    // 立即执行一次初始复检
    this.runOnce().catch(err => console.error('[AppealRechecker] Initial check error:', err));
    // 定期执行
    this.timer = setInterval(() => {
      this.runOnce().catch(err => console.error('[AppealRechecker] Periodic check error:', err));
    }, this.intervalMs);
  }

  public stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    console.log('[AppealRechecker] Stopped.');
  }

  /**
   * 提取账号 lastError 中的 Google 申诉链接，并保存到账号的 appealUrl 字段（首次检测自动留痕）。
   * Google 返回的 TOS 封禁报文包含 appeal_url 字段，能定位到申诉入口。
   */
  public extractAppealUrlFromError(lastError: string | undefined): string | undefined {
    if (!lastError) return undefined;
    // 匹配 JSON 报文中的 "appeal_url": "https://..."
    const match = lastError.match(/appeal_url["'\s:]+([^"']+)/);
    if (match) {
      return match[1];
    }
    // 兼容直接出现 appeal_url 字段的文本
    const alt = lastError.match(/appeal_url\s*:\s*([^\s,]+)/);
    if (alt) {
      return alt[1];
    }
    return undefined;
  }

  /**
   * 执行一次申诉复检（同步触发异步逻辑）
   */
  public async runOnce(): Promise<void> {
    const deadAccounts = this.pool.getAllAccounts().filter(acc => acc.status === 'dead');
    // 提取需要复检的账号（存在申诉链接）
    const accountsToRecheck: Account[] = [];
    for (const acc of deadAccounts) {
      const appealUrl = this.extractAppealUrlFromError(acc.lastError);
      if (appealUrl) {
        // 首次检测到申诉链接时保存，便于后续稳定复检
        if (!acc.appealUrl) {
          this.pool.updateAccount(acc.id, { appealUrl });
        }
        accountsToRecheck.push(acc);
      }
    }

    if (accountsToRecheck.length === 0) {
      return;
    }
    console.log(`[AppealRechecker] 发现 ${accountsToRecheck.length} 个带申诉链接的 dead 账号，开始轻量可用性复检...`);

    for (const acc of accountsToRecheck) {
      await this.recheckSingleAccount(acc).catch(err => {
        console.error(`[AppealRechecker] 复检账号 ${acc.email} 失败:`, err);
      });
    }
  }

  /**
   * 对单个账号做轻量可用性复检：发送最小可用性请求（token 刷新探测），判断是否已解封。
   * 请求成功（2xx）即视为申诉生效，恢复为 active，清空错误信息；仍返回封禁则保持 dead。
   */
  private async recheckSingleAccount(acc: Account): Promise<boolean> {
    if (!acc.refreshToken) return false;

    const params = this.buildTokenParams(acc.refreshToken);
    const proxy = acc.proxyUrl || this.config.defaultProxyUrl || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
    const res = await this.tokenRequestFn(params, proxy);

    if (res.statusCode >= 200 && res.statusCode < 300) {
      // 申诉生效：自动恢复账号为 active，清空错误信息
      this.pool.updateAccount(acc.id, { status: 'active', lastError: undefined });
      console.log(`[AppealRechecker] 账号 ${acc.email} 申诉复检成功，已自动恢复为 active。`);
      return true;
    }

    if (res.statusCode === 403) {
      // 仍被 TOS 封禁：保持 dead 状态，继续等待申诉生效
      if (!acc.appealUrl) {
        this.pool.updateAccount(acc.id, { appealUrl: this.extractAppealUrlFromError(acc.lastError) });
      }
      console.log(`[AppealRechecker] 账号 ${acc.email} 复检仍被封禁（${res.statusCode}），继续等待申诉生效。`);
      return false;
    }

    // 其他错误（如仍 400 但非 TOS 封禁）保持原状态，由其他逻辑处理
    console.error(`[AppealRechecker] 账号 ${acc.email} 复检异常（HTTP ${res.statusCode}），保持原状态等待。`);
    return false;
  }

  /** 构造复检用的 token 请求参数 */
  private buildTokenParams(refreshToken: string): URLSearchParams {
    const params = new URLSearchParams({
      client_id: this.config.oauthClientId || 'antigravity-test-recheck',
      refresh_token: refreshToken,
      grant_type: 'refresh_token'
    });
    const clientSecret = this.config.oauthClientSecret || 'test-secret';
    params.append('client_secret', clientSecret);
    return params;
  }
}

/** 默认 token 请求实现（生产环境使用 undici 真实请求） */
function defaultTokenRequest(
  params: URLSearchParams,
  proxy: string | undefined
): Promise<{ statusCode: number; text: () => Promise<string> }> {
  return Promise.resolve({
    statusCode: 0,
    text: async () => ''
  });
}
