"""Multi-upstream dispatcher for WorkBuddy + Trae.

Handles:
- Upstream selection based on model prefix or config
- Automatic failover when primary upstream fails (14018, 429, etc.)
- Chinese error messages for quota exhaustion
"""

import json
import os
import time
from enum import Enum
from typing import Optional, Callable, Any

# Trae adapter
from trae_route import (
    trae_headers,
    trae_models,
    build_trae_request_body,
    parse_trae_sse_event,
    classify_trae_error,
    TRAE_BASE_URL,
    TRAE_IDE_TOKEN,
)


# Upstream types
class Upstream(Enum):
    WORKBUDDY = "workbuddy"
    TRAE = "trae"


# Model prefix to upstream mapping
MODEL_PREFIX_MAP = {
    # Trae-specific models (if any)
    "trae-": Upstream.TRAE,
    # WorkBuddy models are default
}

# Failover trigger error codes
FAILOVER_CODES = {14018, 429}

# Chinese hint for 14018
CREDIT_EXHAUSTED_HINT = (
    "【额度已用完】上游 CodeBuddy 账号的 credit 已耗尽（错误码 14018）。"
    "本地代理本身没有故障，重试不会恢复：需要充值或等额度周期重置。"
    "用量/充值 https://www.codebuddy.ai/profile/usage"
)


def select_upstream(model: str, config: dict) -> Upstream:
    """Select upstream based on model prefix or config."""
    # Check model prefix first
    for prefix, upstream in MODEL_PREFIX_MAP.items():
        if model.startswith(prefix):
            return upstream
    
    # Check config
    upstreams = config.get("upstreams", {})
    if upstreams.get("trae", {}).get("enabled"):
        # If Trae is enabled, use it as fallback
        pass
    
    # Default to WorkBuddy
    return Upstream.WORKBUDDY


def should_failover(status_code: int, body: str, upstream: Upstream) -> bool:
    """Check if we should failover to another upstream."""
    # WorkBuddy 14018 -> failover to Trae
    if upstream == Upstream.WORKBUDDY and status_code == 14018:
        return True
    
    # 429 rate limit -> failover
    if status_code == 429:
        return True
    
    # Check body for error codes
    if "14018" in body:
        return True
    
    return False


def get_chinese_hint(status_code: int, body: str) -> Optional[str]:
    """Get Chinese hint for specific error codes."""
    if status_code == 14018 or "14018" in body:
        return CREDIT_EXHAUSTED_HINT
    return None


def proxy_request(
    model: str,
    request_data: dict,
    stream_callback: Callable[[str], Any],
    config: dict,
    account_provider: Any = None,
) -> tuple[int, str, Optional[dict]]:
    """Proxy request to appropriate upstream with failover.
    
    Args:
        model: Model ID
        request_data: Request body (OpenAI format)
        stream_callback: Callback for streaming chunks
        config: Proxy configuration
        account_provider: WorkBuddy account provider (for WorkBuddy upstream)
    
    Returns:
        (status_code, response_body, usage_info)
    """
    primary = select_upstream(model, config)
    fallback = Upstream.TRAE if primary == Upstream.WORKBUDDY else Upstream.WORKBUDDY
    
    # Check if failover is enabled
    failover_config = config.get("failover", {})
    failover_enabled = failover_config.get("enabled", False)
    
    # Try primary upstream
    status, body, usage = _call_upstream(
        primary, model, request_data, stream_callback, config, account_provider
    )
    
    # Check if we should failover
    if failover_enabled and should_failover(status, body, primary):
        # Add Chinese hint
        hint = get_chinese_hint(status, body)
        if hint:
            body = _add_hint_to_response(body, hint)
        
        # Try fallback
        status, body, usage = _call_upstream(
            fallback, model, request_data, stream_callback, config, account_provider
        )
    
    return status, body, usage


def _call_upstream(
    upstream: Upstream,
    model: str,
    request_data: dict,
    stream_callback: Callable[[str], Any],
    config: dict,
    account_provider: Any,
) -> tuple[int, str, Optional[dict]]:
    """Call specific upstream."""
    if upstream == Upstream.TRAE:
        return _call_trae(model, request_data, stream_callback, config)
    else:
        return _call_workbuddy(model, request_data, stream_callback, config, account_provider)


def _call_trae(
    model: str,
    request_data: dict,
    stream_callback: Callable[[str], Any],
    config: dict,
) -> tuple[int, str, Optional[dict]]:
    """Call Trae upstream."""
    if not TRAE_IDE_TOKEN:
        return 401, '{"error": {"message": "Trae token not configured"}}', None
    
    # Build request body
    body = build_trae_request_body(request_data, model)
    
    # Make request
    url = f"{TRAE_BASE_URL}/api/ide/v1/chat"
    headers = trae_headers()
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=120) as resp:
            # Trae returns non-streaming response
            data = json.loads(resp.read().decode())
            return 200, json.dumps(data), None
            
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else str(e)
        return e.code, body, None
    except Exception as e:
        return 500, str(e), None


def _call_workbuddy(
    model: str,
    request_data: dict,
    stream_callback: Callable[[str], Any],
    config: dict,
    account_provider: Any,
) -> tuple[int, str, Optional[dict]]:
    """Call WorkBuddy upstream (handled by wb_proxy.py)."""
    # This is a placeholder - the actual implementation is in wb_proxy.py
    # We return a special status to indicate "use existing WorkBuddy logic"
    return 0, "", None  # Signal to use wb_proxy.py


def _add_hint_to_response(body: str, hint: str) -> str:
    """Add Chinese hint to error response."""
    try:
        data = json.loads(body)
        if "error" in data:
            data["error"]["hint"] = hint
        return json.dumps(data)
    except:
        return body


def list_models(config: dict) -> list:
    """List available models from all enabled upstreams."""
    models = []
    
    upstreams = config.get("upstreams", {})
    
    # WorkBuddy models (always available if enabled)
    if upstreams.get("workbuddy", {}).get("enabled", True):
        # These will be fetched dynamically by wb_proxy.py
        models.append({"id": "*workbuddy*", "upstream": "workbuddy"})
    
    # Trae models
    if upstreams.get("trae", {}).get("enabled", False) and TRAE_IDE_TOKEN:
        status, trae_models_list = trae_models()
        if status == 200:
            for m in trae_models_list:
                models.append({"id": m, "upstream": "trae"})
    
    return models