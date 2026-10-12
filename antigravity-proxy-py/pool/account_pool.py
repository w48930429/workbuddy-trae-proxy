import os
import json
import time
import uuid
import secrets
from datetime import datetime
from typing import Optional, Dict, Any, List, Set


DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data'))
ACCOUNTS_FILE = os.path.join(DATA_DIR, 'accounts.json')


def current_time_ms() -> int:
    return int(time.time() * 1000)


def parse_iso_time_ms(iso_str: Optional[str]) -> int:
    if not iso_str:
        return 0
    try:
        clean = iso_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


class AccountPool:
    def __init__(self, initial_strategy: str = 'zero-waste'):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.routing_strategy: str = initial_strategy
        self.start_time: int = current_time_ms()
        self.total_requests_served: int = 0
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_cached_tokens: int = 0
        self.total_cache_creation_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_tokens: int = 0
        self.has_token_usage: bool = False
        self.session_bindings: Dict[str, Dict[str, Any]] = {}  # sessionKey -> {accountId, lastUsedAt}

        os.makedirs(DATA_DIR, exist_ok=True)
        self.load_accounts()

    def load_accounts(self) -> None:
        if not os.path.exists(ACCOUNTS_FILE):
            self.accounts = {}
            return

        try:
            with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            raw_list = data if isinstance(data, list) else data.get('accounts', [])
            self.accounts = {}
            for item in raw_list:
                acc_id = item.get('id') or f"acc_{secrets.token_hex(4)}"
                item['id'] = acc_id
                item.setdefault('activeConcurrency', 0)
                item.setdefault('maxConcurrency', 5)
                item.setdefault('totalRequests', 0)
                item.setdefault('failedRequests', 0)
                item.setdefault('status', 'active')
                item.setdefault('cooldownUntil', 0)
                item.setdefault('lastUsedAt', 0)
                item.setdefault('createdAt', current_time_ms())
                self.accounts[acc_id] = item
        except Exception as e:
            print(f"[AccountPool] Failed to load accounts.json: {e}")

    def save_accounts(self) -> None:
        try:
            with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self.accounts.values()), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[AccountPool] Failed to save accounts.json: {e}")

    def get_all_accounts(self) -> List[Dict[str, Any]]:
        return list(self.accounts.values())

    def get_account(self, acc_id: str) -> Optional[Dict[str, Any]]:
        return self.accounts.get(acc_id)

    def is_family_exhausted(self, family: str) -> bool:
        """
        判断指定模型家族在账号池里是否已全灭 (所有 active 账号该家族 5h+weekly 额度均耗尽且未到重置时间)。
        无任何额度数据的账号视为未知，不参与“全灭”判定。
        """
        now = current_time_ms()
        actives = [a for a in self.accounts.values() if a.get('status') == 'active' and a.get('quota')]
        if not actives:
            return False

        for a in actives:
            q = a['quota']
            if family == 'claude':
                weekly_out = (q.get('claudeWeeklyFraction', 1.0) <= 0.005 and
                              parse_iso_time_ms(q.get('claudeWeeklyResetTime')) > now)
                five_hour_out = (q.get('claude5hFraction', 1.0) <= 0.01 and
                                 parse_iso_time_ms(q.get('claudeResetTime')) > now)
                if not (weekly_out or five_hour_out):
                    return False
            else:
                weekly_out = (q.get('geminiWeeklyFraction', 1.0) <= 0.005 and
                              parse_iso_time_ms(q.get('geminiWeeklyResetTime')) > now)
                five_hour_out = (q.get('gemini5hFraction', 1.0) <= 0.01 and
                                 parse_iso_time_ms(q.get('geminiResetTime')) > now)
                if not (weekly_out or five_hour_out):
                    return False
        return True

    def add_account(self, params: Dict[str, Any]) -> Dict[str, Any]:
        acc_id = f"acc_{secrets.token_hex(4)}"
        now = current_time_ms()
        expires_in = params.get('expiresInSeconds', 3600)
        expires_at = now + expires_in * 1000 if params.get('accessToken') else 0

        new_acc = {
            'id': acc_id,
            'email': params['email'],
            'refreshToken': params['refreshToken'],
            'accessToken': params.get('accessToken', ''),
            'accessTokenExpiresAt': expires_at,
            'status': 'active',
            'cooldownUntil': 0,
            'activeConcurrency': 0,
            'maxConcurrency': params.get('maxConcurrency', 5),
            'totalRequests': 0,
            'failedRequests': 0,
            'proxyUrl': params.get('proxyUrl'),
            'tier': params.get('tier'),
            'lastUsedAt': 0,
            'createdAt': now
        }
        self.accounts[acc_id] = new_acc
        self.save_accounts()
        return new_acc

    def update_account(self, acc_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        acc = self.accounts.get(acc_id)
        if not acc:
            return None
        acc.update(updates)
        self.save_accounts()
        return acc

    def remove_account(self, acc_id: str) -> bool:
        if acc_id in self.accounts:
            del self.accounts[acc_id]
            self.save_accounts()
            return True
        return False

    def acquire_account(
        self,
        exclude_ids: Set[str],
        required_family: Optional[str] = None,
        session_key: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        now = current_time_ms()

        # 1. 检查冷却与状态恢复
        for acc in self.accounts.values():
            if acc.get('status') == 'cooldown' and now >= acc.get('cooldownUntil', 0):
                acc['status'] = 'active'
                acc['cooldownUntil'] = 0

        # 2. 筛选候选账号
        candidates: List[Dict[str, Any]] = []
        for acc in self.accounts.values():
            if acc['id'] in exclude_ids:
                continue
            if acc.get('status') != 'active':
                continue
            if acc.get('activeConcurrency', 0) >= acc.get('maxConcurrency', 5):
                continue

            # 额度感知过滤
            if required_family and acc.get('quota'):
                q = acc['quota']
                if required_family == 'claude':
                    is_weekly_exhausted = q.get('claudeWeeklyFraction', 1.0) <= 0.005
                    is_5h_exhausted = q.get('claude5hFraction', 1.0) <= 0.01
                    reset_time = parse_iso_time_ms(q.get('claudeWeeklyResetTime'))
                    reset_5h_time = parse_iso_time_ms(q.get('claudeResetTime'))
                    if is_weekly_exhausted and reset_time > now:
                        continue
                    if is_5h_exhausted and reset_5h_time > now:
                        continue
                elif required_family == 'gemini':
                    is_weekly_exhausted = q.get('geminiWeeklyFraction', 1.0) <= 0.005
                    is_5h_exhausted = q.get('gemini5hFraction', 1.0) <= 0.01
                    reset_time = parse_iso_time_ms(q.get('geminiWeeklyResetTime'))
                    reset_5h_time = parse_iso_time_ms(q.get('geminiResetTime'))
                    if is_weekly_exhausted and reset_time > now:
                        continue
                    if is_5h_exhausted and reset_5h_time > now:
                        continue

            candidates.append(acc)

        # 兜底策略: 若无候选，且账号尚未拉取过额度 (!quota)，允许尝试一次
        if not candidates and required_family:
            for acc in self.accounts.values():
                if acc['id'] not in exclude_ids and acc.get('status') == 'active':
                    if acc.get('activeConcurrency', 0) < acc.get('maxConcurrency', 5):
                        if not acc.get('quota'):
                            candidates.append(acc)

        if not candidates:
            return None

        # 3. 策略路由选择
        chosen: Optional[Dict[str, Any]] = None

        if self.routing_strategy == 'zero-waste':
            # 阶段 1: 会话粘性优先 (Session Affinity) -> 上游 Prompt Cache 命中
            if session_key and session_key in self.session_bindings:
                bound = self.session_bindings[session_key]
                # 30 分钟过期
                if now - bound.get('lastUsedAt', 0) < 30 * 60 * 1000:
                    for cand in candidates:
                        if cand['id'] == bound.get('accountId'):
                            chosen = cand
                            break

            # 阶段 2: 综合最优选择 (并发优先 -> Tier 偏好 -> 额度剩余度)
            if not chosen:
                def tier_score(tier: Optional[str]) -> int:
                    if tier == 'ultra':
                        return 2
                    if tier == 'pro':
                        return 1
                    return 0

                def quota_fraction(cand: Dict[str, Any]) -> float:
                    q = cand.get('quota')
                    if not q:
                        return 1.0
                    if required_family == 'claude':
                        return q.get('claude5hFraction', 1.0)
                    if required_family == 'gemini':
                        return q.get('gemini5hFraction', 1.0)
                    return 1.0

                candidates.sort(key=lambda a: (
                    a.get('activeConcurrency', 0),        # 越少并发越优先
                    -tier_score(a.get('tier')),           # tier 越高越好
                    -quota_fraction(a),                   # 额度剩余越多越好
                    a.get('lastUsedAt', 0)                # 轮换防饥饿
                ))
                chosen = candidates[0]

            if session_key:
                self.session_bindings[session_key] = {'accountId': chosen['id'], 'lastUsedAt': now}

        elif self.routing_strategy == 'sequential-drain':
            # 流水线顺序耗尽模式：打满一个再切下一个
            candidates.sort(key=lambda a: (
                0 if a.get('activeConcurrency', 0) > 0 else 1,
                a.get('createdAt', 0)
            ))
            chosen = candidates[0]

        else:
            # 传统均衡轮询
            candidates.sort(key=lambda a: (
                a.get('activeConcurrency', 0),
                a.get('lastUsedAt', 0)
            ))
            chosen = candidates[0]

        chosen['activeConcurrency'] = chosen.get('activeConcurrency', 0) + 1
        chosen['lastUsedAt'] = now
        chosen['totalRequests'] = chosen.get('totalRequests', 0) + 1
        self.total_requests_served += 1
        return chosen

    def release_account(
        self,
        acc_id: str,
        error: Optional[Dict[str, Any]] = None,
        family: Optional[str] = None
    ) -> None:
        acc = self.accounts.get(acc_id)
        if not acc:
            return

        acc['activeConcurrency'] = max(0, acc.get('activeConcurrency', 0) - 1)

        if error:
            acc['failedRequests'] = acc.get('failedRequests', 0) + 1
            code = error.get('code', 500)
            msg = str(error.get('message', ''))
            now = current_time_ms()

            if code == 429:
                # 冷却 60 秒
                acc['status'] = 'cooldown'
                acc['cooldownUntil'] = now + 60 * 1000
                acc['lastError'] = f"429 Rate Limited: {msg[:100]}"
                print(f"[AccountPool] Account {acc['email']} entered 60s cooldown due to 429")
            elif code == 401:
                acc['lastError'] = f"401 Unauthorized: {msg[:100]}"
            elif code == 403:
                acc['status'] = 'disabled'
                acc['lastError'] = f"403 Forbidden: {msg[:100]}"
                print(f"[AccountPool] Account {acc['email']} marked as disabled due to 403")
        else:
            acc.pop('lastError', None)

    def get_routing_strategy(self) -> str:
        return self.routing_strategy

    def set_routing_strategy(self, strategy: str) -> None:
        if strategy in ('zero-waste', 'sequential-drain', 'round-robin'):
            self.routing_strategy = strategy

    def record_token_usage(self, usage: Dict[str, Any]) -> None:
        if not usage or not isinstance(usage, dict):
            return

        def read_num(val, fallback=0) -> int:
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
            return fallback

        prompt = read_num(usage.get('promptTokenCount'), read_num(usage.get('prompt_tokens')))
        completion = read_num(usage.get('candidatesTokenCount'), read_num(usage.get('completion_tokens')))
        total = read_num(usage.get('totalTokenCount'), read_num(usage.get('total_tokens'), prompt + completion))
        cache_read = read_num(usage.get('cachedContentTokenCount'),
                              read_num(usage.get('cacheReadInputTokenCount'),
                                       read_num(usage.get('cache_read_input_tokens'))))
        cache_creation = read_num(usage.get('cacheCreationInputTokenCount'),
                                  read_num(usage.get('cache_creation_input_tokens')))

        self.total_prompt_tokens += prompt
        self.total_completion_tokens += completion
        self.total_cached_tokens += cache_read
        self.total_cache_read_tokens += cache_read
        self.total_cache_creation_tokens += cache_creation
        self.total_tokens += total
        self.has_token_usage = True

    def get_stats(self) -> Dict[str, Any]:
        accs = list(self.accounts.values())
        active = sum(1 for a in accs if a.get('status') == 'active')
        cooldown = sum(1 for a in accs if a.get('status') == 'cooldown')
        dead = sum(1 for a in accs if a.get('status') == 'dead')
        disabled = sum(1 for a in accs if a.get('status') == 'disabled')
        active_conc = sum(a.get('activeConcurrency', 0) for a in accs)

        return {
            'totalAccounts': len(accs),
            'activeAccounts': active,
            'cooldownAccounts': cooldown,
            'deadAccounts': dead,
            'disabledAccounts': disabled,
            'totalActiveConcurrency': active_conc,
            'totalRequestsServed': self.total_requests_served,
            'uptimeSeconds': int((current_time_ms() - self.start_time) / 1000),
            'routingStrategy': self.routing_strategy,
            'totalPromptTokens': self.total_prompt_tokens,
            'totalCompletionTokens': self.total_completion_tokens,
            'totalCachedTokens': self.total_cached_tokens,
            'totalCacheCreationTokens': self.total_cache_creation_tokens,
            'totalCacheReadTokens': self.total_cache_read_tokens,
            'totalTokens': self.total_tokens,
            'hasTokenUsage': self.has_token_usage
        }
