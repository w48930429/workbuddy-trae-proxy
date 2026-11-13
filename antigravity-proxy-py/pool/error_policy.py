"""上游失败分类与分级冷却（与 Node 版 error-policy.ts 对等）。

移植自 magpie 的 failure/failureOf + restAfter 思路：
- 先做纯函数分类，再决定账号冷却时长、是否允许换号、是否终局；
- 不再把所有 429 都按 60 秒冷却，也不再把所有 403 都永久停用。
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional


TERMINAL_POLICY_REASONS = {
    'TOS_VIOLATION',
    'POLICY_VIOLATION',
    'SUSPENDED_ACCOUNT',
    'USER_SUSPENDED',
    'ACCOUNT_DISABLED',
    'ACCOUNT_SUSPENDED',
    'ABUSE_DETECTED',
}


def _error_fields(body: str) -> Dict[str, Optional[str]]:
    parsed: Any = None
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = None
    error = parsed.get('error') if isinstance(parsed, dict) and isinstance(parsed.get('error'), dict) else parsed
    details = error.get('details') if isinstance(error, dict) and isinstance(error.get('details'), list) else []
    info = next((d for d in details if isinstance(d, dict) and d.get('reason')), {})
    return {
        'reason': (error or {}).get('reason') or info.get('reason') if isinstance(error, dict) else info.get('reason'),
        'message': (error or {}).get('message') if isinstance(error, dict) else None,
    }


def is_location_error(status_code: int, body: str) -> bool:
    if status_code not in (400, 403):
        return False
    return bool(
        re.search(r'user location is not supported', body, re.IGNORECASE)
        or re.search(r'location is not supported', body, re.IGNORECASE)
        or re.search(r'not (?:available|supported) in your country', body, re.IGNORECASE)
        or re.search(r'unsupported country', body, re.IGNORECASE)
        or (re.search(r'failed_precondition', body, re.IGNORECASE) and re.search(r'location', body, re.IGNORECASE))
    )


def is_policy_blocked_error(status_code: int, body: str) -> bool:
    if status_code != 403:
        return False
    fields = _error_fields(body)
    reason = (fields.get('reason') or '').upper()
    if reason and reason in TERMINAL_POLICY_REASONS:
        return True
    return bool(
        re.search(r'service has been disabled in this account', body, re.IGNORECASE)
        or re.search(r'violation of terms of service', body, re.IGNORECASE)
        or re.search(r'(?:this |the )account has been (?:suspended|disabled|terminated|deactivated)', body, re.IGNORECASE)
        or re.search(r'account (?:is )?(?:suspended|disabled|terminated)', body, re.IGNORECASE)
    )


def is_invalid_grant_error(status_code: int, body: str) -> bool:
    return status_code == 400 and bool(re.search(r'invalid_grant', body, re.IGNORECASE))


def classify_upstream_error(status_code: int, body: str) -> Dict[str, Any]:
    fields = _error_fields(body)
    message = fields.get('message') or body[:1000]

    if is_policy_blocked_error(status_code, body):
        return {'kind': 'policy_blocked', 'reason': fields.get('reason') or 'TOS_VIOLATION', 'message': message}
    if is_location_error(status_code, body):
        return {'kind': 'location', 'reason': fields.get('reason'), 'message': message}
    if status_code == 401 or is_invalid_grant_error(status_code, body):
        return {
            'kind': 'authentication',
            'reason': fields.get('reason') or ('invalid_grant' if is_invalid_grant_error(status_code, body) else None),
            'message': message,
        }
    if status_code == 429:
        kind = 'quota' if re.search(r'quota|resource_exhausted|exhausted', body, re.IGNORECASE) else 'rate_limit'
        return {'kind': kind, 'reason': fields.get('reason'), 'message': message}
    if status_code in (408, 425) or status_code >= 500:
        return {'kind': 'transient', 'reason': fields.get('reason'), 'message': message}
    if status_code == 403:
        return {'kind': 'forbidden', 'reason': fields.get('reason'), 'message': message}
    if 400 <= status_code < 500:
        return {'kind': 'invalid_request', 'reason': fields.get('reason'), 'message': message}
    return {'kind': 'unknown', 'reason': fields.get('reason'), 'message': message}


def _parse_delay(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(float(value) * 1000))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0, int(float(text) * 1000))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((dt.timestamp() - datetime.now(timezone.utc).timestamp()) * 1000))
    except Exception:
        pass
    match = re.match(r'^(\d+(?:\.\d+)?)(ms|s|m|h)$', text, re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {'ms': 1, 's': 1000, 'm': 60000, 'h': 3600000}[unit]
    return max(0, int(amount * multiplier))


def retry_after_ms(headers: Any = None, body: str = '', fallback_ms: int = 60000) -> int:
    """优先使用上游 Retry-After / retryDelay，并限制在 1 秒 ~ 24 小时。"""
    header_value = None
    if headers is not None:
        try:
            header_value = headers.get('retry-after') or headers.get('Retry-After')
        except Exception:
            header_value = None
    delay = _parse_delay(header_value)
    if delay is not None:
        return min(max(delay, 1000), 24 * 60 * 60 * 1000)

    parsed: Any = None
    if body:
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
    details = None
    if isinstance(parsed, dict):
        details = (parsed.get('error') or {}).get('details') or parsed.get('details')
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            delay = _parse_delay(detail.get('retryDelay') or detail.get('quotaResetDelay'))
            if delay is not None:
                return min(max(delay, 1000), 24 * 60 * 60 * 1000)

    return min(max(int(fallback_ms), 1000), 24 * 60 * 60 * 1000)


def cooldown_ms_for(kind: str, retry_after: Optional[int] = None, base_cooldown_ms: int = 60000) -> int:
    """按失败类型给出冷却时长；0 表示不冷却（账号保持可用）。"""
    retry = max(0, int(retry_after or 0))
    base = max(0, int(base_cooldown_ms or 0))
    if kind == 'policy_blocked':
        return 0
    if kind == 'location':
        return max(30 * 60 * 1000, retry)
    if kind == 'quota':
        return max(15 * 60 * 1000, retry)
    if kind == 'rate_limit':
        return max(60 * 1000, base, retry)
    if kind == 'authentication':
        return max(5 * 60 * 1000, retry)
    if kind == 'transient':
        return max(30 * 1000, retry)
    if kind == 'forbidden':
        return max(5 * 60 * 1000, retry)
    if kind == 'invalid_request':
        return 0
    return max(base, retry)


def should_failover_on(kind: str) -> bool:
    """本地只有 Google 一个厂商：终局封禁和请求本身错误不再换号。"""
    return kind not in ('policy_blocked', 'invalid_request')