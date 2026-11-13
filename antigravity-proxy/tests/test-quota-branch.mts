import { AccountPool } from '../src/pool/account-pool.js';
import { RequestExecutor, UpstreamRequestError } from '../src/pool/breaker.js';
import { TokenRefresher } from '../src/pool/refresher.js';
import { AppConfig } from '../src/types.js';

let passed = 0;
let failed = 0;
function assert(cond: boolean, msg: string) {
  if (cond) { passed++; console.log('  ✅ ' + msg); }
  else { failed++; console.error('  ❌ ' + msg); }
}

const config: AppConfig = {
  port: 3099, host: '127.0.0.1', apiMasterKey: 'k', adminPassword: 'p',
  upstreamUrl: 'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse',
  defaultMaxConcurrency: 3, cooldownSeconds: 30, tokenRefreshIntervalMs: 60000
};

const payload = () => ({
  project: 'aicode-consumers',
  model: 'gemini-3.8-flash-high',
  clientModel: 'gemini-3.8-flash',
  effort: 'high' as const,
  request: { contents: [{ role: 'user' as const, parts: [{ text: 'hi' }] }] }
});

async function run() {
  const pool = new AccountPool('zero-waste');
  const executor = new RequestExecutor(pool, new TokenRefresher(pool, config), config);

  const accounts = pool.getAllAccounts();
  console.log('pool accounts:', accounts.length);

  // 场景 1：额度充足（>1%），但所有账号并发满载 → 必须报 429，绝不能报额度耗尽
  for (const acc of accounts) {
    acc.status = 'active';
    acc.activeConcurrency = acc.maxConcurrency; // 占满并发
    acc.quota = {
      geminiWeeklyFraction: 0.9, gemini5hFraction: 0.9,
      claudeWeeklyFraction: 0.9, claude5hFraction: 0.9
    } as never;
  }
  try {
    await executor.executeWithRetry(payload() as never, 1, 's1');
    assert(false, '并发满载时应抛错，不应成功执行');
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    assert(!msg.includes('5小时额度均已耗尽'), '并发满载不得误报「5小时额度均已耗尽」');
    assert(err instanceof UpstreamRequestError && err.statusCode === 429, '并发满载返回 429');
    assert(msg.includes('账号池繁忙'), '并发满载提示账号池繁忙');
  }

  // 场景 2：真实额度耗尽（<=1%）→ 才报额度耗尽
  for (const acc of accounts) {
    acc.status = 'active';
    acc.activeConcurrency = 0;
    acc.quota = {
      geminiWeeklyFraction: 0.9, gemini5hFraction: 0.0,
      geminiResetTime: new Date(Date.now() + 3600_000).toISOString(),
      claudeWeeklyFraction: 0.9, claude5hFraction: 0.9
    } as never;
  }
  try {
    await executor.executeWithRetry(payload() as never, 1, 's2');
    assert(false, '额度耗尽时应抛错');
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    assert(msg.includes('5小时额度均已耗尽'), '真实额度耗尽才报「5小时额度均已耗尽」');
    assert(msg.includes('回满倒计时'), '真实耗尽时附带回满倒计时');
  }

  console.log(`\n结果: ${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
}

run().catch(err => { console.error(err); process.exit(1); });