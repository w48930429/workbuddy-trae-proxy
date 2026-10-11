"""Trae upstream adapter - handles Trae-specific request/response transformation.

Trae API differences from WorkBuddy:
- Different base URL and endpoints
- Device fingerprint headers required
- Token format: Bearer token from TRAE_IDE_TOKEN env
- SSE streaming with different event types
"""

import json
import os
import time
import uuid
from typing import Optional
import urllib.request
import urllib.error

# Trae device configuration from environment
TRAE_DEVICE = {
    "x-app-id": os.environ.get("TRAE_APP_ID", ""),
    "x-device-brand": os.environ.get("TRAE_DEVICE_BRAND", ""),
    "x-device-cpu": os.environ.get("TRAE_DEVICE_CPU", ""),
    "x-device-id": os.environ.get("TRAE_DEVICE_ID", ""),
    "x-device-type": os.environ.get("TRAE_DEVICE_TYPE", ""),
    "x-ide-version": os.environ.get("TRAE_IDE_VERSION", ""),
    "x-ide-version-code": os.environ.get("TRAE_IDE_VERSION_CODE", ""),
    "x-ide-version-type": os.environ.get("TRAE_IDE_VERSION_TYPE", ""),
    "x-machine-id": os.environ.get("TRAE_MACHINE_ID", ""),
    "x-os-version": os.environ.get("TRAE_OS_VERSION", ""),
}

TRAE_BASE_URL = os.environ.get("TRAE_BASE_URL", "https://trae-api-cn.mchost.guru")
TRAE_IDE_TOKEN = os.environ.get("TRAE_IDE_TOKEN", "")

# 完整设备/客户端头（对齐 TraeWorkAssistant models_sync.rs 2026-09 抓包固化；
# CN host + 完整头 = 200，缺头会被网关 401/500）
TRAE_HEADERS = {
    "Content-Type": "application/json",
    "Request-Traffic-Type": "prod",
    "User-Agent": "TraeClient/TTNet",
    "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
    "x-app-version": "default",
    "x-app-version-code": "20260811",
    "x-bridge-transport": "aha",
    "x-device-brand": "CREFG-XX",
    "x-device-cpu": "Intel",
    "x-device-type": "windows",
    "x-ide-version": "0.1.50",
    "x-ide-version-code": "20260811",
    "x-ide-version-type": "stable",
    "x-lgw-req-sdk-type": "3",
    "x-os-version": "Windows 11 Home China",
    "package-type": "stable_cn",
    "x-lscbd-aid": "787976",
    "x-lscbd-platform": "windows",
    "x-ss-dp": "787976",
}


def trae_headers(token: str = None) -> dict:
    """Build Trae API headers with device fingerprint.

    device_id / machine_id 优先用本机 Trae IDE icube 凭证（与 OAuth 交换同源，
    服务端校验设备绑定），缺省回落 CREFG-XX 占位形态。
    """
    token = (token or TRAE_IDE_TOKEN).removeprefix("Bearer ")
    headers = dict(TRAE_HEADERS)
    try:
        import trae_icube
        creds = trae_icube.get_device_credentials()
        if creds:
            cred = creds[0]
            headers["x-device-id"] = cred.device_id
            headers["x-machine-id"] = cred.machine_id
    except Exception:
        pass
    headers["x-ide-token"] = token
    return headers


def trae_models(token: str = None) -> tuple[int, list]:
    """Fetch available models from Trae.
    
    Returns: (status_code, list_of_model_ids)
    """
    url = f"{TRAE_BASE_URL}/api/ide/v1/model_list?type=llm_raw_chat"
    req = urllib.request.Request(url, headers=trae_headers(token))
    try:
        # trae 域名区域封锁：显式直连（绕过系统代理），走本机中国出口
        direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with direct_opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            models = [m["name"] for m in data.get("model_configs", [])]
            return 200, models
    except urllib.error.HTTPError as e:
        return e.code, []
    except Exception:
        return 500, []


def build_trae_request_body(request_data: dict, model: str) -> dict:
    """Transform OpenAI-style request to Trae format."""
    messages = request_data.get("messages", [])
    current_turn = sum(1 for msg in messages[:-1] if msg.get("role") == "user")
    last_assistant = next(
        (msg for msg in reversed(messages) if msg.get("role") == "assistant"),
        None
    )
    
    body = {
        "chat_history": [
            {**msg, "status": "success", "locale": "zh-cn"}
            for msg in messages[:-1]
        ],
        "context_resolvers": [],
        "conversation_id": str(uuid.uuid4()),
        "current_turn": current_turn,
        "generate_suggested_questions": False,
        "intent_name": "general_qa_intent",
        "is_preset": True,
        "last_llm_response_info": (
            {"turn": current_turn - 1, "is_error": False, "response": last_assistant.get("content", "")}
            if last_assistant else {}
        ),
        "model_name": model,
        "multi_media": [],
        "provider": "",
        "session_id": str(uuid.uuid4()),
        "user_input": messages[-1].get("content", "") if messages else "",
        "valid_turns": list(range(current_turn)),
        "variables": json.dumps({
            "locale": "zh-cn",
            "current_time": time.strftime("%Y%m%d %H:%M:%S %A")
        }),
    }
    return body


def parse_trae_sse_event(sse_data: dict) -> Optional[dict]:
    """Parse Trae SSE event into OpenAI chunk format."""
    event_type = sse_data.get("event", "")
    data = sse_data.get("data", {})
    
    if event_type == "metadata":
        return {"type": "metadata", "id": data.get("prompt_completion_id", "")}
    
    if event_type == "output":
        return {
            "type": "content",
            "content": data.get("response", ""),
            "reasoning_content": data.get("reasoning_content", ""),
        }
    
    if event_type == "token_usage":
        return {
            "type": "usage",
            "completion_tokens": data.get("completion_tokens", 0),
            "prompt_tokens": data.get("prompt_tokens", 0),
            "total_tokens": data.get("total_tokens", 0),
        }
    
    if event_type == "done":
        return {"type": "done", "finish_reason": "stop"}
    
    if event_type == "error":
        return {
            "type": "error",
            "message": str(data),
            "finish_reason": "error",
        }
    
    return None


def classify_trae_error(status_code: int, body: str) -> str:
    """Classify Trae error for retry/failover logic.
    
    Returns: 'soft' (retry), 'hard' (switch account), 'fatal'
    """
    if status_code == 429:
        return "soft"
    if status_code == 401:
        return "hard"
    if status_code >= 500:
        return "soft"
    
    # Check for Trae-specific error codes in body
    body_lower = body.lower()
    if "rate" in body_lower or "too many" in body_lower:
        return "soft"
    if "token" in body_lower or "auth" in body_lower:
        return "hard"
    
    return "fatal"