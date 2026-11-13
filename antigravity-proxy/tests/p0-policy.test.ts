import fs from 'fs';
import os from 'os';
import path from 'path';
import assert from 'assert';
import {
  classifyUpstreamError,
  cooldownMsFor,
  retryAfterMs,
  shouldFailoverOn
} from '../src/pool/error-policy.js';

function testPolicy() {
  assert.equal(classifyUpstreamError(403, '{"error":{"reason":"TOS_VIOLATION"}}').kind, 'policy_blocked');
  assert.equal(classifyUpstreamError(403, 'User location is not supported for the API use.').kind, 'location');
  assert.equal(classifyUpstreamError(403, '{"error":{"message":"permission denied"}}').kind, 'forbidden');
  assert.equal(classifyUpstreamError(429, '{"error":{"message":"Resource has been exhausted (quota)"}}').kind, 'quota');
  assert.equal(classifyUpstreamError(429, 'rate limited').kind, 'rate_limit');
  assert.equal(classifyUpstreamError(401, 'unauthorized').kind, 'authentication');
  assert.equal(classifyUpstreamError(500, 'boom').kind, 'transient');
  assert.equal(classifyUpstreamError(400, 'invalid argument').kind, 'invalid_request');

  assert.equal(shouldFailoverOn('policy_blocked'), false);
  assert.equal(shouldFailoverOn('invalid_request'), false);
  assert.equal(shouldFailoverOn('rate_limit'), true);
  assert.equal(shouldFailoverOn('quota'), true);
  assert.equal(shouldFailoverOn('forbidden'), true);

  assert.equal(cooldownMsFor('rate_limit', { baseCooldownMs: 60000 }), 60000);
  assert.equal(cooldownMsFor('rate_limit', { retryAfterMs: 120000, baseCooldownMs: 60000 }), 120000);
  assert.equal(cooldownMsFor('quota'), 15 * 60_000);
  assert.equal(cooldownMsFor('location'), 30 * 60_000);
  assert.equal(cooldownMsFor('forbidden'), 5 * 60_000);
  assert.equal(cooldownMsFor('transient'), 30_000);
  assert.equal(cooldownMsFor('invalid_request'), 0);

  assert.equal(retryAfterMs({ 'retry-after': '30' }), 30_000);
  assert.equal(
    retryAfterMs(undefined, '{"error":{"details":[{"retryDelay":"2s"}]}}'),
    2_000
  );
  console.log('✅ error-policy: all assertions passed');
}

async function testPool() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ag-pool-'));
  process.chdir(tmp);
  fs.mkdirSync(path.join(tmp, 'data'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'data', 'accounts.json'), JSON.stringify([
    {
      id: 'a1', email: 'a1@example.com', refreshToken: 'r1', accessToken: 't1',
      accessTokenExpiresAt: Date.now() + 3600_000, status: 'active',
      cooldownUntil: 0, activeConcurrency: 0, maxConcurrency: 5,
      totalRequests: 0, failedRequests: 0, lastUsedAt: 0, createdAt: Date.now()
    },
    {
      id: 'a2', email: 'a2@example.com', refreshToken: 'r2', accessToken: 't2',
      accessTokenExpiresAt: Date.now() + 3600_000, status: 'active',
      cooldownUntil: 0, activeConcurrency: 0, maxConcurrency: 5,
      totalRequests: 0, failedRequests: 0, lastUsedAt: 0, createdAt: Date.now()
    }
  ]), 'utf-8');

  // 动态导入，确保模块里的 DATA_DIR 使用刚切换的 cwd。
  const { AccountPool } = await import('../src/pool/account-pool.js');
  const pool = new AccountPool('zero-waste', { rpmLimit: 1, baseCooldownMs: 60000 });

  const first = pool.acquireAccount(new Set(), 'claude', 'sess-1');
  assert.ok(first, 'first acquire should succeed');
  assert.equal((pool as any).sessionBindings.get('sess-1')?.accountId, first!.id);
  const affinityPath = path.join(tmp, 'data', 'affinity.json');
  await new Promise(r => setTimeout(r, 700));
  assert.ok(fs.existsSync(affinityPath), 'affinity.json should be written');
  const saved = JSON.parse(fs.readFileSync(affinityPath, 'utf-8'));
  assert.equal(saved['sess-1'].accountId, first!.id);

  pool.releaseAccount(first!.id);
  const second = pool.acquireAccount(new Set(), 'claude', 'sess-2');
  assert.equal(second?.id, 'a2', 'rpmLimit=1 should skip a1 within 60s and pick the other account');
  if (second) pool.releaseAccount(second.id);

  // 分级冷却：policy_blocked → dead
  const a1 = pool.getAccount('a1')!;
  pool.releaseAccount('a1', { code: 403, message: 'violation of terms of service' }, 'claude', {
    kind: 'policy_blocked', message: 'violation of terms of service'
  });
  assert.equal(a1.status, 'dead');

  const a2 = pool.getAccount('a2')!;
  const before = Date.now();
  pool.releaseAccount('a2', { code: 429, message: 'rate limited' }, 'claude', {
    kind: 'rate_limit', message: 'rate limited'
  }, 90_000);
  assert.equal(a2.status, 'cooldown');
  assert.ok(a2.cooldownUntil - before >= 85_000, 'rate_limit cooldown should respect retry-after');

  // 会话粘滞重启恢复
  const pool2 = new AccountPool('zero-waste', {});
  assert.equal((pool2 as any).sessionBindings.get('sess-1')?.accountId, first!.id);

  console.log('✅ account-pool: rpm / affinity / graded cooldown assertions passed');
}

testPolicy();
(async () => {
  await testPool();
})().then(() => {
  process.exit(0);
}).catch(err => {
  console.error(err);
  process.exit(1);
});