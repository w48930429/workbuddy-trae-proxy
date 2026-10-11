# Trae 独立上游路径（与 WorkBuddy 链路完全分离）。
#
# 上游端点：POST https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat
# （SOLO 网关，消耗通用积分 product_id 208；对齐 TraeWorkAssistant
#   api_server/payload.rs prepare_llm_chat_body + routes.rs make_upstream_request
#   2026-09 抓包实证。旧 /api/ide/v1/chat 只认 5 个老模型，GLM/Kimi/Qwen 系报 4023）
#
# 职责：
# - trae_first 优先路由判定（config.failover.trae_priority）
# - Trae 支持模型判定：内置支持表（IDE chat_v3 实证）+ config.trae_model_map 覆盖
# - 多账号轮换（vault 凭证 + config.trae_accounts enabled 状态，按积分缓存降序）
# - 非流式：SSE 全量聚合为 OpenAI chat.completion JSON
# - 流式：Trae SSE → OpenAI chat.completion.chunk 中继（生成器）
# - 4023（模型未知）/ 401（token 失效）等错误归类，供上层回落 WB
#
# 上层契约（wb_proxy._dispatch_chat_post）：
# - try_trae(model, payload) -> (200, body_json_str, usage_dict) 或 (错误码, 错误体, None)
# - trae_stream(model, payload) -> OpenAI chunk dict 迭代器（首 chunk 前失败抛
#   TraeStreamError，调用方回落 WB；首 chunk 后失败由调用方收尾）

import json
import os
import threading
import time
import sys
import urllib.error
import urllib.request
import uuid

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_ROOT_DIR, "config.json")

# SOLO 网关常量（对齐 TraeWorkAssistant api_server/mod.rs）
AGENT_HOST = "https://trae-api-cn.mchost.guru"
EP_LLM_CHAT = "/api/agent/v3/llm_utils_chat"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.50"
IDE_VERSION_CODE = "20260811"
WORKSPACE_ID = "e04cdd"

# Trae 实证支持的模型（IDE chat_v3 视图 + llm_utils_chat __dev 通路实测 200）。
# 小写比较。未列出的模型仍可经 config.trae_model_map 显式映射。
TRAE_SUPPORTED = {
    "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3", "glm-5.2",
    "deepseek-v4.1-flash",
    "kimi-k3", "kimi-k2.8-preview", "kimi-k2.6",
    "minimax-m3", "minimax-m2.7",
    "qwen3.8-flash", "qwen3.8-max", "qwen-3.7-plus", "qwen-3.6-plus",
    "doubao-seed-code", "doubao-seed-evolving",
    "doubao-seed-2.1-pro", "doubao-seed-2.1-turbo",
    "step-5-preview",
}

# 部分模型仅在 solo_agent 视图可用（solo_work_lite 下报 4001）；
# 其余走 solo_work_lite（Trae Work 模式默认 function）。
SOLO_AGENT_ONLY = {
    "doubao-seed-code", "glm-5.3-flash", "qwen3.8-flash",
    "deepseek-v4.1-flash", "kimi-k2.8-preview",
}

# Trae 官方积分倍率（batch_get_detail_param → display_contact_config →
# consumption_rate.data.rate，2026-10-03 实拉；与 TraeWorkAssistant
# models_sync.rs L2 提取同源）。
# 计费口径为：每 1000 tokens 消耗 rate 积分（即 credit = (total_tokens / 1000) * rate）。
TRAE_RATES = {
    "glm-5.3-flash": 0.06,
    "glm-5.3-flashx": 0.31,
    "glm-5.3": 0.78,
    "glm-5.2": 0.78,
    "deepseek-v4.1-flash": 0.08,
    "kimi-k3": 1.83,
    "kimi-k2.8-preview": 0.98,
    "kimi-k2.6": 0.83,          # kimi-k2.7-code 同价
    "minimax-m3": 0.26,
    "minimax-m2.7": 0.19,       # minimax-m2/m2.1 同价
    "qwen3.8-flash": 0.08,
    "qwen3.8-max": 1.5,
    "qwen-3.7-plus": 0.25,
    "qwen-3.6-plus": 0.26,      # qwen-3.5 同价
    "doubao-seed-code": 0.06,
    "doubao-seed-evolving": 0.08,
    "doubao-seed-2.1-pro": 0.08,
    "doubao-seed-2.1-turbo": 0.2,
    "step-5-preview": 0.48,
}
TRAE_DEFAULT_RATE = 0.1


def trae_catalog_models() -> list:
    """Return structured model metadata and credit rates for Trae models."""
    models = []
    seen = set()

    # 1. IDE 原生 Raw Chat 模型 (官方直连)
    raw_models = [
        {
            "id": "deepseek-R1",
            "name": "DeepSeek Reasoner (R1)",
            "rate": 0.08,
            "context_length": 64000,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_tool_calls": False,
            "supports_reasoning": True,
            "reasoning_effort": "原生推理",
            "channel": "IDE Raw Chat",
            "desc": "官方原生 DeepSeek R1 深度推理底座",
        },
        {
            "id": "deepseek-V3",
            "name": "DeepSeek V3",
            "rate": 0.08,
            "context_length": 64000,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_tool_calls": True,
            "supports_reasoning": False,
            "reasoning_effort": "—",
            "channel": "IDE Raw Chat",
            "desc": "官方原生 DeepSeek V3 高速旗舰",
        },
        {
            "id": "Doubao_1_5_thinking_pro",
            "name": "Doubao 1.5 Thinking Pro",
            "rate": 0.08,
            "context_length": 64000,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_tool_calls": False,
            "supports_reasoning": True,
            "reasoning_effort": "原生推理",
            "channel": "IDE Raw Chat",
            "desc": "豆包 1.5 深度思考推理模型",
        },
        {
            "id": "seed_m8",
            "name": "Doubao 1.5 Pro",
            "rate": 0.06,
            "context_length": 64000,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_tool_calls": True,
            "supports_reasoning": False,
            "reasoning_effort": "—",
            "channel": "IDE Raw Chat",
            "desc": "豆包 1.5 Pro 通用大模型",
        },
        {
            "id": "deepseek-V3-0324",
            "name": "DeepSeek V3 (0324)",
            "rate": 0.08,
            "context_length": 64000,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_tool_calls": True,
            "supports_reasoning": False,
            "reasoning_effort": "—",
            "channel": "IDE Raw Chat",
            "desc": "DeepSeek V3 0324 增强版本",
        },
    ]
    for m in raw_models:
        seen.add(m["id"].lower())
        models.append(m)

    # 2. SOLO 网关支持的模型（按倍率升序排序）
    cat = {}
    try:
        import wb_catalog
        for m in getattr(wb_catalog, "STATIC_CN_MODELS", []) + getattr(wb_catalog, "STATIC_INTL_MODELS", []):
            cat[m.get("id")] = m
    except Exception:
        pass

    sorted_supported = sorted(
        TRAE_SUPPORTED,
        key=lambda mid: (TRAE_RATES.get(mid, TRAE_DEFAULT_RATE), mid)
    )
    for mid in sorted_supported:
        if mid.lower() in seen:
            continue
        seen.add(mid.lower())
        rate = TRAE_RATES.get(mid, TRAE_DEFAULT_RATE)
        meta = cat.get(mid) or {}

        ctx = meta.get("maxAllowedSize") or 128000
        out = meta.get("maxOutputTokens") or 16384
        vis = bool(meta.get("supportsImages"))
        tool = bool(meta.get("supportsToolCall", True))
        rz = bool(meta.get("supportsReasoning", "r1" in mid or "thinking" in mid))

        if "doubao" in mid:
            ctx = 128000
            out = 16384
            tool = True
        elif "qwen" in mid:
            ctx = 1000000 if ("3.8" in mid or "plus" in mid) else 128000
            out = 32000
            vis = "vl" in mid or "vision" in mid
        elif "step" in mid:
            ctx = 256000
            out = 32000
            vis = True
        elif "flashx" in mid:
            ctx = 1000000
            out = 32000
            vis = True

        channel = "SOLO Agent" if mid in SOLO_AGENT_ONLY else "SOLO Work"
        desc = "Trae Agent 专属通路" if channel == "SOLO Agent" else "Trae Work 模式默认通路"

        models.append({
            "id": mid,
            "name": meta.get("name") or mid,
            "rate": rate,
            "context_length": ctx,
            "max_output_tokens": out,
            "supports_vision": vis,
            "supports_tool_calls": tool,
            "supports_reasoning": rz,
            "reasoning_effort": "原生" if rz else "—",
            "channel": channel,
            "desc": desc,
        })
    return models


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def trae_priority() -> str:
    """'fallback'（默认，WB 先 Trae 兜底）| 'trae_first'（Trae 优先扣分）。"""
    return (_load_config().get("failover") or {}).get("trae_priority", "fallback")


def failover_enabled() -> bool:
    return bool((_load_config().get("failover") or {}).get("enabled", False))


def _model_map() -> dict:
    """config.trae_model_map：{WB 模型名: Trae 模型名} 显式覆盖。"""
    return _load_config().get("trae_model_map") or {}


def map_model(model: str):
    """WB 模型名 → Trae 模型名；不支持且未映射返回 None（上层直走 WB）。

    优先级：显式映射表 > 内置支持表（原名直通）。
    """
    m = _model_map().get(model)
    if m:
        return m
    lower = model.lower()
    for wb, tr in _model_map().items():
        if wb.lower() == lower:
            return tr
    if lower in TRAE_SUPPORTED:
        return model
    return None


def _secrets() -> dict:
    try:
        import trae_vault
        return trae_vault.load_trae_secrets()
    except Exception:
        return {}


def _enabled_state() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return (json.load(f) or {}).get("trae_accounts", {}) or {}
    except Exception:
        return {}


def pick_account(model: str = "", exclude: set = None):
    """选一个可用 Trae 账号：enabled 且未过期，按缓存积分降序。

    model / exclude：跳过该模型限流中的账号。Trae 的限流按凭证判定（与
    WorkBuddy 的 code 6004 同理），而原实现只按积分取最高的那个，于是
    一个账号被限流就把整个 Trae 池拖下水——其余账号可能完好无损，调用方
    却只能整体跳过 Trae。

    Returns: (uid, jwt) 或 (None, None)
    """
    state = _enabled_state()
    now = time.time()
    exclude = set(exclude or ())
    rows = []
    for uid, data in _secrets().items():
        if uid in exclude:
            continue
        st = state.get(uid, {})
        if not st.get("enabled", True):
            continue
        exp = data.get("expires_at", 0)
        if exp and exp < now:
            continue
        jwt = data.get("token", "")
        if not jwt:
            continue
        if model and model.lower() in MODEL_COOLDOWNS.get(uid, {}):
            if MODEL_COOLDOWNS[uid][model.lower()] > now:
                continue
            MODEL_COOLDOWNS[uid].pop(model.lower(), None)
        credits = (st.get("credits") or {}).get("total", 0.0)
        rows.append((credits, uid, jwt))
    if not rows:
        return None, None
    rows.sort(reverse=True)
    return rows[0][1], rows[0][2]


# Per-account, per-model cooldowns: {uid: {model_lower: expires_at}}. Mirrors
# the WorkBuddy pool's model_cooldowns so the dashboard can render Trae the same
# way, and so one throttled credential never silences the whole Trae pool.
MODEL_COOLDOWNS: dict = {}
MODEL_COOLDOWN_LOCK = threading.Lock()

# Consecutive-throttle counters for exponential backoff: {uid: {model: strikes}}.
# Trae's upstream says only "exceeded the rate limit" with no reset timestamp
# (unlike WorkBuddy's "将在…重置"), so a fixed window is a guess: too short and
# every request keeps probing a wall that is still up. Each additional
# consecutive throttle doubles the wait; a successful call clears the streak.
_THROTTLE_STRIKES: dict = {}
COOLDOWN_BASE_SECONDS = 60.0
COOLDOWN_MAX_SECONDS = 600.0


def cooldown_wait_for(uid: str, model: str) -> float:
    """Backoff length for the Nth consecutive throttle on this account+model."""
    with MODEL_COOLDOWN_LOCK:
        strikes = _THROTTLE_STRIKES.get(uid, {}).get(model.lower(), 0)
    if strikes <= 0:
        return COOLDOWN_BASE_SECONDS
    return min(COOLDOWN_BASE_SECONDS * (2 ** (strikes - 1)), COOLDOWN_MAX_SECONDS)


def clear_cooldown_strikes(uid: str, model: str) -> None:
    """A successful call proves the account is usable again."""
    with MODEL_COOLDOWN_LOCK:
        _THROTTLE_STRIKES.get(uid, {}).pop(model.lower(), None)


def note_account_cooldown(uid: str, model: str, wait: float = 0.0) -> float:
    """Record that `uid` is throttled on `model`; returns the applied window.

    wait <= 0 means "use the backoff schedule" (the normal path). Consecutive
    throttles escalate 60s -> 120s -> 240s -> ... capped at 10 minutes, so a
    genuinely long rate-limit window is respected instead of being re-probed
    every minute.

    uid may legitimately be the empty string (the vault has an account whose
    key is ""), so guard on None rather than falsiness — `if not uid` silently
    dropped that account from cooldown accounting, which made the
    all-accounts check disagree with reality.
    """
    if uid is None or not model:
        return 0.0
    key = model.lower()
    with MODEL_COOLDOWN_LOCK:
        strikes = _THROTTLE_STRIKES.get(uid, {}).get(key, 0) + 1
        _THROTTLE_STRIKES.setdefault(uid, {})[key] = strikes
        effective = wait if wait > 0 else min(
            COOLDOWN_BASE_SECONDS * (2 ** (strikes - 1)), COOLDOWN_MAX_SECONDS)
        MODEL_COOLDOWNS.setdefault(uid, {})[key] = time.time() + effective
    return effective

def _strike(uid: str, model: str) -> int:
    """Current consecutive-throttle count for this account+model."""
    with MODEL_COOLDOWN_LOCK:
        return _THROTTLE_STRIKES.get(uid, {}).get(model.lower(), 0)


def account_cooldown_snapshot() -> dict:
    """{uid: [{model, expiresAt, wait, strikes}]} of live cooldowns.

    wait/strikes travel with the entry so the dashboard can explain *why* a
    window is long ("第 3 次连续限流，等待 4 分钟") instead of showing an
    unexplained number that keeps changing.
    """
    now = time.time()
    out = {}
    with MODEL_COOLDOWN_LOCK:
        for uid, models in list(MODEL_COOLDOWNS.items()):
            live = []
            for model, until in list(models.items()):
                if until > now:
                    wait = int(round(until - now))
                    live.append({
                        "model": model,
                        "expiresAt": int(until),
                        "wait": wait,
                        "strikes": _THROTTLE_STRIKES.get(uid, {}).get(model, 1),
                    })
                else:
                    models.pop(model, None)
            if live:
                live.sort(key=lambda r: (r["expiresAt"], r["model"]))
                out[uid] = live
            if not models:
                MODEL_COOLDOWNS.pop(uid, None)
    return out


def model_cooldown_wait_all_accounts(model: str) -> float:
    """Seconds until *every* usable account can serve `model` again.

    A single throttled credential must not silence the pool: pick_account
    already skips throttled accounts, so the gateway only needs to stop trying
    Trae when there is genuinely nobody left. Returns 0 as soon as one account
    is free.
    """
    if not model:
        return 0.0
    key = model.lower()
    state = _enabled_state()
    now = time.time()
    waits = []
    for uid, data in _secrets().items():
        st = state.get(uid, {})
        if not st.get("enabled", True):
            continue
        exp = data.get("expires_at", 0)
        if exp and exp < now:
            continue
        if not data.get("token", ""):
            continue
        with MODEL_COOLDOWN_LOCK:
            until = MODEL_COOLDOWNS.get(uid, {}).get(key, 0.0)
        waits.append(max(0.0, until - now))
    # No usable account at all -> let the caller try and surface the real error.
    if not waits:
        return 0.0
    active = [w for w in waits if w > 0]
    return int(min(active)) if len(active) == len(waits) else 0.0


def _device_ids():
    """设备指纹：优先本机 Trae IDE icube 凭证（服务端校验设备绑定）。"""
    try:
        import trae_icube
        creds = trae_icube.get_device_credentials()
        if creds:
            return creds[0].device_id, creds[0].machine_id
    except Exception:
        pass
    return "", ""


def _uuid_like():
    return str(uuid.uuid4())


def normalize_tool_choice(payload: dict) -> None:
    tc = payload.get("tool_choice")
    if not tc:
        return
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
        else:
            payload["tool_choice"] = tc.strip().lower()
    elif isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
        elif typ in ("auto", "required"):
            payload["tool_choice"] = typ
        elif typ == "function":
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or "auto"
            payload["tool_choice"] = str(name).strip()


def normalize_tools(tools):
    if not isinstance(tools, list) or not tools:
        return None
    out = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        item_copy = dict(item)
        fn = item_copy.get("function")
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        fn_copy = dict(fn)
        # parameters object → JSON string (Trae 上游协议要求)
        params = fn_copy.get("parameters")
        if isinstance(params, dict):
            fn_copy["parameters"] = json.dumps(params, ensure_ascii=False)
        item_copy["function"] = fn_copy
        out.append(item_copy)
    return out if out else None


def build_llm_chat_body(payload: dict, model: str, uid: str) -> dict:
    """OpenAI 请求体 → llm_utils_chat 请求体（对齐 payload.rs prepare_llm_chat_body）。"""
    messages = []
    for msg in payload.get("messages") or []:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = [{"type": "text", "text": content}]
        # assistant tool_calls: function → function_call（上游旧格式）
        if m.get("role") == "assistant" and m.get("tool_calls"):
            kept = []
            for tc in m["tool_calls"]:
                tc = dict(tc)
                if "function" in tc:
                    tc["function_call"] = tc.pop("function")
                if (tc.get("function_call") or {}).get("name"):
                    kept.append(tc)
            if kept:
                m["tool_calls"] = kept
            else:
                m.pop("tool_calls", None)
        messages.append(m)

    body_payload = dict(payload)
    normalize_tool_choice(body_payload)
    tools = normalize_tools(body_payload.get("tools"))
    tool_choice = body_payload.get("tool_choice")

    # model → config_name + model_name（未收录模型原样透传 + __dev，实证规律）
    lower = model.lower()
    config_name = model
    model_name = f"{model}__dev"

    # function：部分模型仅 solo_agent 视图可用，其余 solo_work_lite
    function = "solo_agent" if lower in SOLO_AGENT_ONLY else "solo_work_lite"

    device_id, machine_id = _device_ids()
    max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if not isinstance(max_tokens, (int, float)) or max_tokens <= 0:
        max_tokens = 4096

    body = {
        "messages": messages,
        "config_name": config_name,
        "model_name": model_name,
        "stream": True,
        "function": function,
        "max_tokens": max_tokens,
        "conversation_id": _uuid_like(),
        "user_id": uid,
        "session_id": _uuid_like(),
        "device_id": device_id,
        "machine_id": machine_id,
        "project_id": _uuid_like(),
        "workspace_id": WORKSPACE_ID,
        "prompt_max_tokens": 168_000,
        "mode": "FunctionCall",
        "ide_version": IDE_VERSION,
        "ide_version_code": IDE_VERSION_CODE,
        "app_id": APP_ID,
        "package_type": "stable_cn",
    }
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    return body

def _request_headers(jwt: str) -> dict:
    """llm_utils_chat 请求头（对齐 routes.rs make_upstream_request）。"""
    base = {
        "Content-Type": "application/json",
        "Request-Traffic-Type": "prod",
        "User-Agent": "TraeClient/TTNet",
        "x-app-id": APP_ID,
        "x-app-version": "default",
        "x-app-version-code": IDE_VERSION_CODE,
        "x-ide-version": IDE_VERSION,
        "x-ide-version-code": IDE_VERSION_CODE,
        "x-ide-version-type": "stable",
        "x-device-type": "windows",
        "x-device-brand": "CREFG-XX",
        "x-device-cpu": "Intel",
        "x-os-version": "Windows 11 Home China",
        "package-type": "stable_cn",
        "x-lgw-req-sdk-type": "3",
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "x-ss-dp": "787976",
        "referer": AGENT_HOST + EP_LLM_CHAT,
        "x-ide-token": jwt.removeprefix("Bearer "),
    }
    device_id, machine_id = _device_ids()
    if device_id:
        base["x-device-id"] = device_id
    if machine_id:
        base["x-machine-id"] = machine_id
    return base


_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_chat(jwt: str, trae_model: str, payload: dict, uid: str, timeout: int = 120):
    """发起 llm_utils_chat 请求，返回原始 HTTPResponse（SSE 流）。

    Raises: urllib.error.HTTPError / URLError。
    """
    body = build_llm_chat_body(payload, trae_model, uid)
    req = urllib.request.Request(
        AGENT_HOST + EP_LLM_CHAT,
        data=json.dumps(body).encode(),
        headers=_request_headers(jwt),
        method="POST")
    # trae 域名区域封锁：显式直连（绕过系统代理），走本机中国出口
    return _DIRECT_OPENER.open(req, timeout=timeout)


def _iter_sse_events(resp):
    """逐行解析 SSE 流，yield (event_name, data_dict)。"""
    event = ""
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            try:
                data = json.loads(data_str)
            except Exception:
                data = {"raw": data_str}
            yield event or "message", data
            event = ""
            continue
        if not line:
            event = ""


class TraeError(Exception):
    """Trae 业务错误（SSE event: error 或聚合失败）。

    kind: 'model_unknown' / 'auth' / 'quota' / 'other'
    """

    def __init__(self, kind: str, code, message: str):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message


class TraeStreamError(Exception):
    """流式前置失败（未发任何字节给客户端，可安全回落 WB）。"""
    pass


def _classify_sse_error(data) -> TraeError:
    if not isinstance(data, dict):
        data = {"message": str(data)}
    code = data.get("code")
    message = str(data.get("message") or data.get("error") or data)
    low = message.lower()
    # 限流优先判定：上游文案是 "We're sorry, your requests have exceeded the
    # rate limit." —— 既不含 quota 也不含 credit，原逻辑会把它落进 other
    # 并映射成 502，调用方因此无法把它当限流记账与退避（每个请求都白撞）。
    # 必须排在 token/auth 之前，否则文案里的词会被误判成鉴权失败。
    if code in (14018, 429) or "rate limit" in low or "rate_limit" in low \
            or "credit" in low or "quota" in low:
        return TraeError("quota", code, message)
    if code in (401, 40101) or "token" in low or "auth" in low:
        return TraeError("auth", code, message)
    if code == 4023 or "model is unknown" in message:
        return TraeError("model_unknown", code, message)
    if code == 4001:
        # 上游必填字段缺失/非法（如 prompt_max_tokens），归 other 但带码
        return TraeError("other", code, message)
    return TraeError("other", code, message)


def _usage_from(d: dict, model: str = "") -> dict:
    rate = TRAE_RATES.get((model or "").lower(), TRAE_DEFAULT_RATE)
    total = d.get("total_tokens", 0) or 0
    return {
        "prompt_tokens": d.get("prompt_tokens", 0),
        "completion_tokens": d.get("completion_tokens", 0),
        "total_tokens": total,
        "reasoning_tokens": d.get("reasoning_tokens", 0),
        "cached_tokens": d.get("cache_read_input_tokens", 0),
        # Trae usage 事件不带 credit；按官方倍率换算：每 1000 tokens 消耗 rate 积分
        "credit": round((total / 1000.0) * rate, 4),
    }


def _usage_estimate(payload: dict, content: str, reasoning: str, model: str = "") -> dict:
    """上游未回 usage 时按 4 字符/token 估算。"""
    def est(s):
        return max(1, len(s) // 4) if s else 0
    pt = sum(len(str(m.get("content") or "")) // 4 + 4
             for m in payload.get("messages") or [])
    ct = est(content) + est(reasoning)
    rate = TRAE_RATES.get((model or "").lower(), TRAE_DEFAULT_RATE)
    return {"prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": pt + ct, "reasoning_tokens": 0, "cached_tokens": 0,
            "credit": round(((pt + ct) / 1000.0) * rate, 4)}


def _convert_trae_tool_calls_delta(raw_calls):
    if not isinstance(raw_calls, list) or not raw_calls:
        return None
    converted = []
    for idx, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            continue
        c = dict(call)
        if "index" not in c or c["index"] is None:
            c["index"] = idx
        if "function_call" in c:
            c["function"] = c.pop("function_call")
        fn = c.get("function")
        if isinstance(fn, dict):
            clean_fn = dict(fn)
            clean_fn.pop("namespace", None)
            clean_fn.pop("partial_arguments", None)
            if "arguments" in clean_fn and clean_fn["arguments"] is None:
                clean_fn["arguments"] = ""
            c["function"] = clean_fn
        converted.append(c)
    return converted if converted else None


class ToolCallsBuffer:
    def __init__(self):
        self.by_index = {}

    def add_delta(self, tool_calls_delta):
        if not tool_calls_delta:
            return
        for item in tool_calls_delta:
            idx = item.get("index", 0)
            if idx not in self.by_index:
                self.by_index[idx] = {
                    "id": item.get("id") or ("call_%s" % uuid.uuid4().hex[:24]),
                    "type": "function",
                    "function": {
                        "name": (item.get("function") or {}).get("name") or "",
                        "arguments": (item.get("function") or {}).get("arguments") or ""
                    }
                }
            else:
                entry = self.by_index[idx]
                item_id = item.get("id")
                if item_id:
                    entry["id"] = item_id
                fn = item.get("function") or {}
                fn_name = fn.get("name")
                if fn_name:
                    entry["function"]["name"] = fn_name
                fn_args = fn.get("arguments")
                if fn_args:
                    entry["function"]["arguments"] += fn_args

    def to_list(self):
        if not self.by_index:
            return None
        return [self.by_index[i] for i in sorted(self.by_index.keys())]


def try_trae(model: str, payload: dict):
    """非流式请求走 Trae（llm_utils_chat，SSE 全量聚合）。

    Returns: (status_code, body_str, usage)
      - 200: body 是 OpenAI chat.completion JSON
      - 其它: body 是 OpenAI error JSON（上层可回落 WB 或透传）
    """
    trae_model = map_model(model)
    if not trae_model:
        return 404, json.dumps({"error": {"message": "model not supported by Trae: %s" % model,
                                          "type": "invalid_request_error"}}), None
    uid, jwt = pick_account(trae_model)
    if not jwt:
        return 503, json.dumps({"error": {"message": "no usable Trae account",
                                          "type": "server_error"}}), None
    try:
        resp = _post_chat(jwt, trae_model, payload, uid)
        content, reasoning, usage = "", "", {}
        tool_buf = ToolCallsBuffer()
        finish_reason = None
        for event, data in _iter_sse_events(resp):
            if event == "error":
                raise _classify_sse_error(data)
            if not isinstance(data, dict):
                continue
            if event in ("output", "thought") or "response" in data:
                if data.get("response"):
                    content += data["response"]
                if data.get("reasoning_content"):
                    reasoning += data["reasoning_content"]
                raw_tc = data.get("tool_calls")
                if raw_tc:
                    cleaned = _convert_trae_tool_calls_delta(raw_tc)
                    tool_buf.add_delta(cleaned)
            elif event in ("done", "turn_completion"):
                finish_reason = data.get("finish_reason")
            elif "prompt_tokens" in data and "total_tokens" in data:
                usage = _usage_from(data, trae_model)
    except TraeError as exc:
        kind_status = {"model_unknown": 400, "auth": 401, "quota": 429}.get(exc.kind, 502)
        # 限流记到真正发请求的那个账号，而不是"Trae 整体"。Trae 的限流按凭证
        # 判定，只冷却这一个账号即可让 pick_account 换人；整体冷却会白停掉
        # 其余仍然可用的账号。上游不给重置时间，故用连续次数指数退避。
        if exc.kind == "quota":
            applied = note_account_cooldown(uid, trae_model)
            print("[trae] %s throttled on %s, cooling %ds (strike %d)"
                  % (uid, trae_model, int(applied), _strike(uid, trae_model)),
                  file=sys.stderr)
        return kind_status, json.dumps({"error": {"message": "trae: %s" % exc.message,
                                                  "type": "upstream_error",
                                                  "code": exc.code}}), None

    # 成功即证明该账号恢复可用，退避计数归零，下一次限流重新从 60s 起算。
    clear_cooldown_strikes(uid, trae_model)
    collected_tools = tool_buf.to_list()
    full = (content or reasoning) or ("" if collected_tools else "")
    if not full and not collected_tools:
        return 502, json.dumps({"error": {"message": "trae empty response",
                                          "type": "upstream_error"}}), None
    if not usage:
        usage = _usage_estimate(payload, content, reasoning, trae_model)
    if not finish_reason:
        finish_reason = "tool_calls" if collected_tools else "stop"

    choices_msg = {"role": "assistant"}
    if content:
        choices_msg["content"] = content
    else:
        choices_msg["content"] = None
    if reasoning:
        choices_msg["reasoning_content"] = reasoning
    if collected_tools:
        choices_msg["tool_calls"] = collected_tools

    body = {
        "id": "trae-%s" % os.urandom(8).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": choices_msg,
            "finish_reason": finish_reason,
        }],
        "usage": usage,
        "upstream": "trae",
        "upstream_account": uid,
    }
    return 200, json.dumps(body, ensure_ascii=False), usage


def trae_stream(model: str, payload: dict):
    """流式请求走 Trae：yield OpenAI chat.completion.chunk dict。

    惰性连接：首次 next() 才发请求；模型不支持/无账号/首事件前失败直接抛
    TraeStreamError，调用方可在发出任何字节前回落 WB。
    成功结束时最后 yield 一个 usage dict。
    """
    trae_model = map_model(model)
    if not trae_model:
        raise TraeStreamError("model not supported by Trae: %s" % model)
    uid, jwt = pick_account(trae_model)
    if not jwt:
        raise TraeStreamError("no usable Trae account")

    resp = _post_chat(jwt, trae_model, payload, uid)
    chunk_id = "trae-%s" % os.urandom(8).hex()

    def _chunk(delta, finish=None):
        return {"id": chunk_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "upstream": "trae", "upstream_account": uid,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    content_parts, reasoning_parts = [], []
    usage = {}
    has_tool_calls = False
    finish_reason = None
    for event, data in _iter_sse_events(resp):
        if event == "error":
            _exc = _classify_sse_error(data)
            # 同 try_trae：只冷却发出请求的这个账号，pick_account 会换人；
            # 连续限流用指数退避，上游不给重置时间。
            if _exc.kind == "quota":
                _applied = note_account_cooldown(uid, trae_model)
                print("[trae] %s throttled on %s, cooling %ds (strike %d)"
                      % (uid, trae_model, int(_applied), _strike(uid, trae_model)),
                      file=sys.stderr)
            raise _exc
        if not isinstance(data, dict):
            continue
        if event in ("output", "thought") or "response" in data:
            r_text = data.get("response") or ""
            r_reason = data.get("reasoning_content") or ""
            raw_tc = data.get("tool_calls")
            cleaned_tc = _convert_trae_tool_calls_delta(raw_tc) if raw_tc else None

            delta = {}
            if r_text:
                content_parts.append(r_text)
                delta["content"] = r_text
            if r_reason:
                reasoning_parts.append(r_reason)
                delta["reasoning_content"] = r_reason
            if cleaned_tc:
                has_tool_calls = True
                delta["tool_calls"] = cleaned_tc
            if delta:
                yield _chunk(delta)
        elif event in ("done", "turn_completion"):
            finish_reason = data.get("finish_reason")
        elif "prompt_tokens" in data and "total_tokens" in data:
            usage = _usage_from(data, trae_model)

    if not finish_reason:
        finish_reason = "tool_calls" if has_tool_calls else "stop"
    # 成功即归零退避计数，与 try_trae 一致。
    clear_cooldown_strikes(uid, trae_model)
    yield _chunk({}, finish=finish_reason)
    yield usage or _usage_estimate(payload, "".join(content_parts),
                                   "".join(reasoning_parts), trae_model)
# ---- 看板模型目录：把支持表渲染成 /v1/models 同构条目 ----
# 上下文/输出为 SOLO 网关固定契约（prompt_max_tokens 必填 168000，
# 上游按此钳制），非逐模型宣告。
TRAE_CONTEXT = 168_000
TRAE_MAX_OUTPUT = 4096

# 视觉能力未经上游确认（llm_utils_chat content 只实证了 [{type:text}]），
# 按「文本 + 推理 + 工具」宣告。
TRAE_NO_REASONING = {
    "kimi-k2.6", "doubao-seed-code", "doubao-seed-2.1-turbo",
}


def model_catalog() -> list:
    """看板 Trae 模型清单：与 /v1/models 同构的条目列表。"""
    out = []
    for mid in sorted(TRAE_SUPPORTED):
        lower = mid.lower()
        reasoning = lower not in TRAE_NO_REASONING
        entry = {
            "id": mid,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "trae",
            "name": mid,
            "capabilities": {"vision": False, "tool_calls": True,
                             "reasoning": reasoning},
            "supports_vision": False,
            "supports_images": False,
            "supports_tool_calls": True,
            "supports_reasoning": reasoning,
            "vision": False,
            "multimodal": False,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "context_length": TRAE_CONTEXT,
            "max_input_tokens": TRAE_CONTEXT,
            "max_output_tokens": TRAE_MAX_OUTPUT,
            "max_completion_tokens": TRAE_MAX_OUTPUT,
            "function": "solo_agent" if lower in SOLO_AGENT_ONLY else "solo_work_lite",
        }
        if reasoning:
            entry["reasoning_efforts"] = ["low", "medium", "high", "xhigh", "max"]
            entry["reasoning_default_effort"] = "high"
        out.append(entry)
    return out


# ---- 向后兼容：wb_proxy 旧调用点（14018 兜底 failover）----
try_trae_failover = try_trae
