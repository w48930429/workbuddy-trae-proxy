import { AccountPool } from '../src/pool/account-pool.js';
import { AppealRechecker } from '../src/pool/appeal-rechecker.js';
import { AppConfig } from '../src/types.js';

function createTestConfig(): AppConfig {
  return {
    port: 3099,
    host: '127.0.0.1',
    apiMasterKey: 'test-sk-key',
    adminPassword: 'test-admin-password',
    upstreamUrl: 'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse',
    defaultMaxConcurrency: 3,
    cooldownSeconds: 30,
    tokenRefreshIntervalMs: 60000,
    oauthClientId: 'test-oauth-id',
    oauthClientSecret: 'test-oauth-secret',
    defaultProxyUrl: ''
  };
}

/** 构造注入用的假请求函数：根据返回状态模拟复检结果 */
function createMockTokenRequest(simulatedStatus: number) {
  return async (params: URLSearchParams, proxy?: string) => {
    return {
      statusCode: simulatedStatus,
      text: async () => ''
    };
  };
}

async function runAppealRecheckerTests() {
  console.log('🧪 Starting AppealRechecker tests...\n');

  const config = createTestConfig();
  const pool = new AccountPool(config.routingStrategy || 'zero-waste');

  let passed = 0;
  let failed = 0;
  function assert(condition: boolean, message: string) {
    if (condition) {
      passed++;
      console.log('  ✅ ' + message);
    } else {
      failed++;
      console.error('  ❌ ' + message);
    }
  }

  // 准备一个 TOS 封禁的 dead 账号，带申诉链接
  const deadAcc = {
    id: 'acc_test_tos',
    email: 'test-tos-account@gmail.com',
    refreshToken: 'test-refresh-token',
    accessToken: 'test-access-token',
    accessTokenExpiresAt: Date.now() + 3600000,
    status: 'dead',
    cooldownUntil: 0,
    activeConcurrency: 0,
    maxConcurrency: 5,
    totalRequests: 0,
    failedRequests: 0,
    proxyUrl: undefined,
    lastUsedAt: 0,
    createdAt: Date.now(),
    lastError: '[HTTP 403] {"error": {"code": 403, "message": "This service has been disabled...", "status": "PERMISSION_DENIED", "details": [{"appeal_url": "https://forms.gle/test-appeal-link"}]}}',
    quota: {
      geminiWeeklyFraction: 0.9,
      gemini5hFraction: 0.9,
      claudeWeeklyFraction: 0.9,
      claude5hFraction: 0.9
    }
  };
  pool.addAccount({
    email: deadAcc.email,
    refreshToken: deadAcc.refreshToken,
    accessToken: deadAcc.accessToken,
    expiresInSeconds: 3600,
    proxyUrl: undefined,
    maxConcurrency: 5,
    tier: 'pro'
  });
  const accountId = pool.getAllAccounts()[0].id;
  pool.updateAccount(accountId, { status: 'dead', lastError: deadAcc.lastError, quota: deadAcc.quota });

  // Test 1: 提取申诉链接
  {
    const rechecker = new AppealRechecker(pool, config, 1000);
    const extracted = rechecker.extractAppealUrlFromError(deadAcc.lastError);
    console.log(`Test 1: 从 lastError 提取申诉链接 -> ${extracted}`);
    assert(extracted === 'https://forms.gle/test-appeal-link', '申诉链接提取正确');
  }

  // Test 2: 首次检测自动保存申诉链接
  {
    const rechecker2 = new AppealRechecker(pool, config, 1000, createMockTokenRequest(200));
    await rechecker2.runOnce();
    const after = pool.getAccount(accountId);
    console.log(`Test 2: 首次检测自动保存申诉链接 -> appealUrl=${after?.appealUrl}`);
    assert(after?.appealUrl === 'https://forms.gle/test-appeal-link', '首次检测自动保存申诉链接');
  }

  // Test 3: 复检成功（模拟申诉生效，返回 200）→ 自动恢复为 active
  {
    const rechecker3 = new AppealRechecker(pool, config, 1000, createMockTokenRequest(200));
    await rechecker3.runOnce();
    const after = pool.getAccount(accountId);
    console.log(`Test 3: 复检成功恢复状态 -> status=${after?.status}, lastError=${after?.lastError}`);
    assert(after?.status === 'active', '复检成功自动恢复为 active');
    assert(after?.lastError === undefined, '复检成功清空 lastError');
  }

  // Test 4: 复检仍被封禁（模拟返回 403）→ 保持 dead
  {
    const rechecker4 = new AppealRechecker(pool, config, 1000, createMockTokenRequest(403));
    const deadFor403 = { ...deadAcc, id: 'acc_test_tos_403', email: 'test-tos-403-account@gmail.com' };
    pool.addAccount({
      email: deadFor403.email,
      refreshToken: deadFor403.refreshToken,
      accessToken: deadFor403.accessToken,
      expiresInSeconds: 3600,
      tier: 'pro'
    });
    const dead403Id = pool.getAllAccounts()[0].id;
    pool.updateAccount(dead403Id, { status: 'dead', lastError: deadAcc.lastError, quota: deadAcc.quota });
    await rechecker4.runOnce();
    const after = pool.getAccount(dead403Id);
    console.log(`Test 4: 复检仍被封禁状态 -> status=${after?.status}, appealUrl=${after?.appealUrl}`);
    assert(after?.status === 'dead', '复检仍被封禁保持 dead');
    assert(after?.appealUrl === 'https://forms.gle/test-appeal-link', '复检仍被封禁时保留申诉链接');
  }

  // Test 5: lastError 无申诉链接的 dead 账号不被复检
  {
    const noAppealAcc = { ...deadAcc, id: 'acc_test_no_appeal', email: 'test-no-appeal-account@gmail.com' };
    pool.addAccount({
      email: noAppealAcc.email,
      refreshToken: noAppealAcc.refreshToken,
      accessToken: noAppealAcc.accessToken,
      expiresInSeconds: 3600,
      tier: 'pro'
    });
    const noAppealId = pool.getAllAccounts()[0].id;
    pool.updateAccount(noAppealId, { status: 'dead', lastError: '[HTTP 403] {invalid message no appeal_url}' });
    const rechecker5 = new AppealRechecker(pool, config, 1000, createMockTokenRequest(200));
    await rechecker5.runOnce();
    const after = pool.getAccount(noAppealId);
    console.log(`Test 5: 无申诉链接账号不被复检 -> status=${after?.status}`);
    assert(after?.status === 'dead', '无申诉链接的 dead 账号不被复检');
  }

  // Test 6: 正常 active 账号不受影响
  {
    const activeAcc = pool.addAccount({
      email: 'test-active-account@gmail.com',
      refreshToken: 'test-active-refresh',
      accessToken: 'test-active-access',
      expiresInSeconds: 3600,
      tier: 'pro'
    });
    const rechecker6 = new AppealRechecker(pool, config, 1000, createMockTokenRequest(403));
    await rechecker6.runOnce();
    assert(pool.getAccount(activeAcc.id)?.status === 'active', 'active 账号不受复检影响');
  }

  console.log(`\n结果: ${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
}

runAppealRecheckerTests().catch(err => {
  console.error('❌ AppealRechecker tests failed:', err);
  process.exit(1);
});
