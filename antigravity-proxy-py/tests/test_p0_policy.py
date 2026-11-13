import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pool import account_pool as ap
from pool.error_policy import (
    classify_upstream_error,
    cooldown_ms_for,
    retry_after_ms,
    should_failover_on,
)


class ErrorPolicyTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_upstream_error(403, '{"error":{"reason":"TOS_VIOLATION"}}')['kind'], 'policy_blocked')
        self.assertEqual(classify_upstream_error(403, 'User location is not supported for the API use.')['kind'], 'location')
        self.assertEqual(classify_upstream_error(403, '{"error":{"message":"permission denied"}}')['kind'], 'forbidden')
        self.assertEqual(classify_upstream_error(429, '{"error":{"message":"Resource has been exhausted (quota)"}}')['kind'], 'quota')
        self.assertEqual(classify_upstream_error(429, 'rate limited')['kind'], 'rate_limit')
        self.assertEqual(classify_upstream_error(401, 'unauthorized')['kind'], 'authentication')
        self.assertEqual(classify_upstream_error(500, 'boom')['kind'], 'transient')
        self.assertEqual(classify_upstream_error(400, 'invalid argument')['kind'], 'invalid_request')

    def test_failover_and_cooldown(self):
        self.assertFalse(should_failover_on('policy_blocked'))
        self.assertFalse(should_failover_on('invalid_request'))
        self.assertTrue(should_failover_on('rate_limit'))
        self.assertTrue(should_failover_on('quota'))
        self.assertTrue(should_failover_on('forbidden'))

        self.assertEqual(cooldown_ms_for('rate_limit', None, 60000), 60000)
        self.assertEqual(cooldown_ms_for('rate_limit', 120000, 60000), 120000)
        self.assertEqual(cooldown_ms_for('quota'), 15 * 60 * 1000)
        self.assertEqual(cooldown_ms_for('location'), 30 * 60 * 1000)
        self.assertEqual(cooldown_ms_for('forbidden'), 5 * 60 * 1000)
        self.assertEqual(cooldown_ms_for('transient'), 30 * 1000)
        self.assertEqual(cooldown_ms_for('invalid_request'), 0)

    def test_retry_after(self):
        self.assertEqual(retry_after_ms({'retry-after': '30'}), 30000)
        self.assertEqual(retry_after_ms(None, '{"error":{"details":[{"retryDelay":"2s"}]}}'), 2000)


class AccountPoolTest(unittest.TestCase):
    def test_rpm_affinity_and_graded_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap.DATA_DIR = tmp
            ap.ACCOUNTS_FILE = os.path.join(tmp, 'accounts.json')
            ap.AFFINITY_FILE = os.path.join(tmp, 'affinity.json')
            accounts = [
                {
                    'id': 'a1', 'email': 'a1@example.com', 'refreshToken': 'r1', 'accessToken': 't1',
                    'accessTokenExpiresAt': ap.current_time_ms() + 3600000, 'status': 'active',
                    'cooldownUntil': 0, 'activeConcurrency': 0, 'maxConcurrency': 5,
                    'totalRequests': 0, 'failedRequests': 0, 'lastUsedAt': 0, 'createdAt': ap.current_time_ms(),
                },
                {
                    'id': 'a2', 'email': 'a2@example.com', 'refreshToken': 'r2', 'accessToken': 't2',
                    'accessTokenExpiresAt': ap.current_time_ms() + 3600000, 'status': 'active',
                    'cooldownUntil': 0, 'activeConcurrency': 0, 'maxConcurrency': 5,
                    'totalRequests': 0, 'failedRequests': 0, 'lastUsedAt': 0, 'createdAt': ap.current_time_ms(),
                },
            ]
            with open(ap.ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(accounts, f)

            pool = ap.AccountPool('zero-waste', {'rpmLimit': 1, 'baseCooldownMs': 60000})
            first = pool.acquire_account(set(), 'claude', 'sess-1')
            self.assertIsNotNone(first)
            self.assertEqual(pool.session_bindings['sess-1']['accountId'], first['id'])
            self.assertTrue(os.path.exists(ap.AFFINITY_FILE))
            with open(ap.AFFINITY_FILE, 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f)['sess-1']['accountId'], first['id'])

            pool.release_account(first['id'])
            second = pool.acquire_account(set(), 'claude', 'sess-2')
            self.assertEqual(second['id'], 'a2', 'rpmLimit=1 should skip a1 within 60s')
            pool.release_account(second['id'])

            # 会话粘滞重启恢复（此时 sess-1 仍绑定 a1；后面的封禁测试会清掉绑定）
            pool2 = ap.AccountPool('zero-waste', {})
            self.assertEqual(pool2.session_bindings['sess-1']['accountId'], first['id'])

            a1 = pool.get_account('a1')
            pool.release_account('a1', {'code': 403, 'message': 'violation of terms of service'}, 'claude',
                                 {'kind': 'policy_blocked', 'message': 'violation of terms of service'})
            self.assertEqual(a1['status'], 'disabled')

            a2 = pool.get_account('a2')
            before = ap.current_time_ms()
            pool.release_account('a2', {'code': 429, 'message': 'rate limited'}, 'claude',
                                 {'kind': 'rate_limit', 'message': 'rate limited'}, 90000)
            self.assertEqual(a2['status'], 'cooldown')
            self.assertGreaterEqual(a2['cooldownUntil'] - before, 85000)



if __name__ == '__main__':
    unittest.main(verbosity=2)