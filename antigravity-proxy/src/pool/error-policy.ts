export type UpstreamErrorKind =
  | 'policy_blocked'
  | 'authentication'
  | 'quota'
  | 'rate_limit'
  | 'location'
  | 'forbidden'
  | 'transient'
  | 'invalid_request'
  | 'unknown';

export interface ClassifiedUpstreamError {
  kind: UpstreamErrorKind;
  reason?: string;
  message: string;
}

function parseJson(body: string): any {
  try {
    return JSON.parse(body);
  } catch {
    return undefined;
  }
}

function errorFields(body: string): { reason?: string; message?: string; status?: string } {
  const parsed = parseJson(body);
  const error = parsed?.error && typeof parsed.error === 'object' ? parsed.error : parsed;
  const details = Array.isArray(error?.details) ? error.details : [];
  const info = details.find((item: any) => item && typeof item === 'object' && item.reason) || {};
  return {
    reason: error?.reason || info.reason,
    message: error?.message || parsed?.error_description || parsed?.message,
    status: error?.status || parsed?.status
  };
}

export function isLocationError(statusCode: number, body: string): boolean {
  return (statusCode === 400 || statusCode === 403) && (
    /user location is not supported/i.test(body) ||
    /location is not supported/i.test(body) ||
    /not (?:available|supported) in your country/i.test(body) ||
    /unsupported country/i.test(body) ||
    (/failed_precondition/i.test(body) && /location/i.test(body))
  );
}

/** Upstream reasons that mean "this account is finished", not "retry later". */
const TERMINAL_POLICY_REASONS = new Set([
  'TOS_VIOLATION',
  'POLICY_VIOLATION',
  'SUSPENDED_ACCOUNT',
  'USER_SUSPENDED',
  'ACCOUNT_DISABLED',
  'ACCOUNT_SUSPENDED',
  'ABUSE_DETECTED'
]);

/**
 * Account-level terminations arrive as 403 with a handful of shapes. Missing
 * one means the background workers keep probing an account Google has already
 * switched off, so err on the side of recognising more of them.
 */
export function isPolicyBlockedError(statusCode: number, body: string): boolean {
  if (statusCode !== 403) return false;
  const fields = errorFields(body);
  if (fields.reason && TERMINAL_POLICY_REASONS.has(fields.reason.toUpperCase())) return true;
  return /service has been disabled in this account/i.test(body) ||
    /violation of terms of service/i.test(body) ||
    /(?:this |the )account has been (?:suspended|disabled|terminated|deactivated)/i.test(body) ||
    /account (?:is )?(?:suspended|disabled|terminated)/i.test(body);
}

export function isInvalidGrantError(statusCode: number, body: string): boolean {
  return statusCode === 400 && /invalid_grant/i.test(body);
}

export function classifyUpstreamError(statusCode: number, body: string): ClassifiedUpstreamError {
  const fields = errorFields(body);
  const message = fields.message || body.slice(0, 1000);

  if (isPolicyBlockedError(statusCode, body)) {
    return { kind: 'policy_blocked', reason: fields.reason || 'TOS_VIOLATION', message };
  }
  if (isLocationError(statusCode, body)) {
    return { kind: 'location', reason: fields.reason, message };
  }
  if (statusCode === 401 || isInvalidGrantError(statusCode, body)) {
    return { kind: 'authentication', reason: fields.reason || (isInvalidGrantError(statusCode, body) ? 'invalid_grant' : undefined), message };
  }
  if (statusCode === 429) {
    const lower = body.toLowerCase();
    const kind = /quota|resource_exhausted|exhausted/i.test(lower) ? 'quota' : 'rate_limit';
    return { kind, reason: fields.reason, message };
  }
  if (statusCode === 408 || statusCode === 425 || statusCode >= 500) {
    return { kind: 'transient', reason: fields.reason, message };
  }
  if (statusCode === 403) {
    // 既不是 TOS 终局，也不是地域错误：可能是临时权限/项目配置问题。
    // 只冷却，不永久 dead（旧逻辑把所有 403 都标 dead）。
    return { kind: 'forbidden', reason: fields.reason, message };
  }
  if (statusCode >= 400 && statusCode < 500) {
    return { kind: 'invalid_request', reason: fields.reason, message };
  }
  return { kind: 'unknown', reason: fields.reason, message };
}

/**
 * 按失败类型给出该账号的冷却时长（毫秒）。0 表示不冷却（账号保持可用）。
 *
 * 与 magpie 的分级冷却对齐：
 * - policy_blocked：调用方标记 dead，不走这里
 * - location：30 分钟（地域问题通常不是账号本身坏了）
 * - quota：至少 15 分钟，尊重上游 Retry-After
 * - rate_limit：至少 60 秒，尊重 Retry-After
 * - authentication：5 分钟（刷新失败后不要立刻再撞）
 * - transient：30 秒（5xx/超时）
 * - forbidden：5 分钟
 * - invalid_request：0（请求本身的问题，换号无意义）
 */
export function cooldownMsFor(
  kind: UpstreamErrorKind,
  options: { retryAfterMs?: number; baseCooldownMs?: number } = {}
): number {
  const retry = Number.isFinite(options.retryAfterMs) ? Math.max(0, options.retryAfterMs as number) : 0;
  const base = Math.max(0, options.baseCooldownMs ?? 60_000);
  switch (kind) {
    case 'policy_blocked':
      return 0;
    case 'location':
      return Math.max(30 * 60_000, retry);
    case 'quota':
      return Math.max(15 * 60_000, retry);
    case 'rate_limit':
      return Math.max(60_000, base, retry);
    case 'authentication':
      return Math.max(5 * 60_000, retry);
    case 'transient':
      return Math.max(30_000, retry);
    case 'forbidden':
      return Math.max(5 * 60_000, retry);
    case 'invalid_request':
      return 0;
    case 'unknown':
    default:
      return Math.max(base, retry);
  }
}

/**
 * 是否允许把这个请求换给同厂商的另一个账号重试。
 *
 * 本地只有 Google 一个厂商，所以“同厂商不扩散”在这里等价于：
 * - 终局封禁（TOS/风控）不再换号，直接把真实错误交给客户端；
 * - 请求本身不合法也没有换号意义。
 * 其余错误（限流、额度、地域、临时 5xx）换号是安全的。
 */
export function shouldFailoverOn(kind: UpstreamErrorKind): boolean {
  return kind !== 'policy_blocked' && kind !== 'invalid_request';
}

function parseDelay(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.max(0, value * 1000);
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const numeric = Number(trimmed);
  if (Number.isFinite(numeric)) return Math.max(0, numeric * 1000);
  const dateMs = Date.parse(trimmed);
  if (Number.isFinite(dateMs)) return Math.max(0, dateMs - Date.now());
  const match = trimmed.match(/^(\d+(?:\.\d+)?)(ms|s|m|h)$/i);
  if (!match) return undefined;
  const amount = Number(match[1]);
  const unit = match[2].toLowerCase();
  const multiplier = unit === 'ms' ? 1 : unit === 's' ? 1000 : unit === 'm' ? 60000 : 3600000;
  return Math.max(0, amount * multiplier);
}

/** Prefer upstream retry hints; cap them so a malformed response cannot block forever. */
export function retryAfterMs(
  headers?: Record<string, unknown>,
  body?: string,
  fallbackMs = 60_000
): number {
  const headerValue = headers?.['retry-after'] ?? headers?.['Retry-After'];
  const headerDelay = parseDelay(headerValue);
  if (headerDelay !== undefined) return Math.min(Math.max(headerDelay, 1000), 24 * 60 * 60 * 1000);

  const parsed = body ? parseJson(body) : undefined;
  const details = parsed?.error?.details || parsed?.details;
  if (Array.isArray(details)) {
    for (const detail of details) {
      const delay = parseDelay(detail?.retryDelay ?? detail?.quotaResetDelay);
      if (delay !== undefined) return Math.min(Math.max(delay, 1000), 24 * 60 * 60 * 1000);
    }
  }

  return Math.min(Math.max(fallbackMs, 1000), 24 * 60 * 60 * 1000);
}
