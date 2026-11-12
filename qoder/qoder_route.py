"""Qoder 上游路由模块 —— 把 Qoder 包装成 OpenAI 兼容 API 端点。

对应 Go: internal/bridge/bridge.go (CallQoderWithOpts) + messages.go + chat.go

核心功能：
  - build_qoder_request_body(): OpenAI 请求 -> Qoder 请求体
  - call_qoder(): 账号池选号 + SSE 流式调用 + 失败自动换号
  - parse_qoder_delta_to_openai(): Qoder Delta -> OpenAI chunk
  - qoder_models(): 获取 Qoder 模型列表
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Optional

import qoder_accounts as accounts
import qoder_client as client
import qoder_cosy as cosy
import qoder_logger as qlog
import qoder_pool as pool

log = qlog.info

BASEPROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qoder_baseprompt.json")

USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qoder_data", "usage.jsonl")
_seq_counter = 0

# 上游排队限流（code 10605 / isQueued）：同账号原地等待重试。免费档账号被
# 排进 p3 队列时，换号只是把同样的排队换个账号再挨一次，只有等待有意义。
# 旧逻辑：单个请求内最多原地等待 2 次、每次最多 60s，客户端全程没有输出，
# 看起来就是卡死。默认 0：把排队错误立刻交回网关，由网关回 429 +
# Retry-After，客户端自己决定重试节奏。需要旧行为时设 QODER_QUEUE_MAX_WAITS=2。
# QUEUE_MAX_WAITS = 2
QUEUE_MAX_WAITS = int(os.environ.get("QODER_QUEUE_MAX_WAITS", "0"))
# 排队是模型级队列（queueType p3/p4），换账号偶尔能躲开（付费档不排队）。
# 默认最多撞 2 个账号就收手：账号多的时候逐个试会退化成十几次上游往返，
# 客户端依然觉得慢。
QUEUE_MAX_ACCOUNTS = int(os.environ.get("QODER_QUEUE_MAX_ACCOUNTS", "2"))
QUEUE_DEFAULT_WAIT_SECONDS = 30  # 详情里没有 retryAfterSeconds 时的兜底等待
QUEUE_MAX_WAIT_SECONDS = 60    # 单次等待上限（详情可能给到 300）


def record_qoder_usage(model: str, account_id: str, stream: bool, outcome: str,
                       elapsed_ms: int, prompt_tokens: int = 0,
                       completion_tokens: int = 0, total_tokens: int = 0,
                       error: str = "") -> None:
    global _seq_counter
    _seq_counter += 1
    entry = {
        "seq": _seq_counter,
        "at": int(time.time()),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "account": account_id,
        "stream": stream,
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "error": error,
    }
    try:
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        with open(USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _account_realm_map() -> dict:
    """账号 id -> 区域（'global' / 'cn'）。用于按区域过滤历史用量记录。"""
    out = {}
    try:
        for a in accounts.list_accounts():
            out[a.id] = pool.normalize_realm(getattr(a, "region", "") or "")
    except Exception:
        pass
    return out


def recent_qoder_usage(limit: int = 20, page: int = 1, realm: str = "") -> dict:
    if not os.path.isfile(USAGE_FILE):
        return {"total": 0, "page": page, "total_pages": 0, "rows": []}
    rows = []
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except Exception:
        return {"total": 0, "page": page, "total_pages": 0, "rows": []}
    rows.reverse()
    target_realm = pool.normalize_realm(realm)
    if target_realm:
        realm_of = _account_realm_map()
        rows = [r for r in rows
                if realm_of.get(str(r.get("account", "")), "") == target_realm]
    total = len(rows)
    total_pages = max(1, (total + limit - 1) // limit)
    start = (page - 1) * limit
    end = start + limit
    return {"total": total, "page": page, "total_pages": total_pages, "rows": rows[start:end]}


def qoder_usage_snapshot(realm: str = "") -> dict:
    if not os.path.isfile(USAGE_FILE):
        return {"requests": 0, "total_tokens": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "by_model": {}, "by_account": {}}
    target_realm = pool.normalize_realm(realm)
    realm_of = _account_realm_map() if target_realm else {}
    requests = 0
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    by_model = {}
    by_account = {}
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if target_realm and realm_of.get(str(r.get("account", "")), "") != target_realm:
                    continue
                requests += 1
                pt = r.get("prompt_tokens", 0)
                ct = r.get("completion_tokens", 0)
                tt = r.get("total_tokens", pt + ct)
                prompt_tokens += pt
                completion_tokens += ct
                total_tokens += tt
                m = r.get("model", "")
                if m not in by_model:
                    by_model[m] = {"requests": 0, "total_tokens": 0}
                by_model[m]["requests"] += 1
                by_model[m]["total_tokens"] += tt
                a = r.get("account", "")
                if a not in by_account:
                    by_account[a] = {"requests": 0, "total_tokens": 0}
                by_account[a]["requests"] += 1
                by_account[a]["total_tokens"] += tt
    except Exception:
        pass
    return {"requests": requests, "total_tokens": total_tokens,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "by_model": by_model, "by_account": by_account}

DEFAULT_MODEL_MAPPING = {
    # ── Qoder 官方模型与别名映射 ──
    "qoduck": "lmodel",
    "lmodel": "lmodel",
    "auto": "auto",
    "qwen3.8-max": "qmodel_38max",
    "qwen-max": "qmodel_38max",
    "qwen3.8-flash": "qfmodel",
    "qwen-flash": "qfmodel",
    "qwen": "qfmodel",
    "deepseek-v4-pro": "dmodel",
    "deepseek-r1": "dmodel",
    "deepseek-flash": "dfmodel",
    "deepseek-chat": "dmodel",
    "deepseek-v3": "dmodel",
    "deepseek": "dmodel",
    "glm-5.3-flash": "gfmodel",
    "glm-flash": "gfmodel",
    "glm-5.3": "gmodel",
    "glm": "gmodel",
    "kimi-k3.1-pro": "kmodel_latest",
    "kimi-pro": "kmodel_latest",
    "kimi-latest": "kmodel_latest",
    "kimi-k2.8-preview": "kmodel",
    "kimi": "kmodel",
    "minimax-m3.1-flash": "mmodel",
    "minimax-flash": "mmodel",
    "minimax": "mmodel",
    "step-5-preview": "jmodel",
    "step-5": "jmodel",
    "step": "jmodel",
    # ── 第三方通用别名映射 ──
    "opus": "qmodel_38max",
    "sonnet": "gmodel",
    "haiku": "qfmodel",
    "gpt": "dmodel",
    "gemini": "gmodel",
}

_template_base: Optional[dict] = None
_pool: Optional[pool.Pool] = None

NO_ACCOUNT_MESSAGE = (
    "没有可用的 Qoder 账号（额度耗尽 / 冷却中 / token 失效）。"
    "请访问控制台 http://127.0.0.1:28087/（点击【Qoder 看板】）扫码登录新账号，登录后会自动恢复。"
)


def _lookup_mapping(model: str, table: dict) -> str:
    """在单张映射表内执行：精确匹配（忽略大小写）→ 包含匹配（长词优先）。"""
    if not table:
        return ""
    m_low = model.lower()
    # 1. 精确匹配（忽略大小写）
    for k, v in table.items():
        if k and v and k.lower() == m_low:
            return v
    # 2. 表中 key 是输入子串（长词优先）
    pairs = [(k, v) for k, v in table.items() if k and v]
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    for src, dst in pairs:
        if src.lower() in m_low:
            return dst
    # 3. 输入是表中 key 的子串兜底
    for src, dst in pairs:
        if m_low in src.lower():
            return dst
    return ""


def _get_template_base() -> dict:
    global _template_base
    if _template_base is None:
        _template_base = client.load_base_prompt(BASEPROMPT_PATH)
    return _template_base


_POOL_RECHECK_INTERVAL = 10  # 秒：周期性检测磁盘账号变化
_pool_check_at = 0.0


def get_pool(proxy: str = "") -> pool.Pool:
    global _pool, _pool_check_at
    now = time.time()
    if _pool is None or _pool.size() == 0:
        _pool = pool.build_pool_from_accounts(proxy)
        _pool_check_at = now
        return _pool
    # 周期性检测磁盘账号数变化（运行中新登录的账号自动入池，无需重启）
    if now - _pool_check_at >= _POOL_RECHECK_INTERVAL:
        _pool_check_at = now
        try:
            disk_count = len(accounts.list_accounts())
            if disk_count != _pool.size():
                _pool = pool.build_pool_from_accounts(proxy)
        except Exception:
            pass
    return _pool


def reload_pool(proxy: str = "") -> pool.Pool:
    global _pool, _pool_check_at
    _pool = pool.build_pool_from_accounts(proxy)
    _pool_check_at = time.time()
    return _pool


# ── 模型映射 ──────────────────────────────────────────────────────────────

def map_model(model: str, agent: str = "", user_mappings: dict = None) -> str:
    if not model:
        return model

    # 1. 调用方传入的 user_mappings 优先（显式参数）
    if user_mappings:
        agent_map = user_mappings.get(agent) or {}
        mapped = _lookup_mapping(model, agent_map)
        if mapped:
            return mapped

    # 2. 从 Settings 加载：agent 维度的用户配置优先
    try:
        settings = accounts.load_settings()
        if settings and agent:
            agent_table = (settings.model_mappings or {}).get(agent) or {}
            mapped = _lookup_mapping(model, agent_table)
            if mapped:
                return mapped
        # 3. 兼容旧的扁平 ModelMapping
        if settings and settings.model_mapping:
            mapped = _lookup_mapping(model, settings.model_mapping)
            if mapped:
                return mapped
    except Exception:
        pass

    # 4. 内置默认映射兜底
    mapped = _lookup_mapping(model, DEFAULT_MODEL_MAPPING)
    if mapped:
        return mapped

    # 5. 大小写归一化兜底
    return model.lower()


# ── 消息格式转换 ──────────────────────────────────────────────────────────

def normalize_content(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
        return "\n\n".join(texts)
    return ""


def extract_images(msg: dict) -> list[str]:
    content = msg.get("content") or msg.get("contents")
    if not isinstance(content, list):
        return []
    images = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in ("image_url", "input_image"):
            url = _image_url_from_part(block)
            if url and _looks_like_image_url(url):
                images.append(url)
        elif btype == "image":
            url = _claude_image_to_url(block)
            if url:
                images.append(url)
    return images


def _image_url_from_part(part: dict) -> str:
    iu = part.get("image_url")
    if isinstance(iu, str):
        return iu
    if isinstance(iu, dict):
        return iu.get("url", "")
    return part.get("url", "")


def _claude_image_to_url(block: dict) -> str:
    src = block.get("source")
    if not isinstance(src, dict):
        return ""
    stype = src.get("type", "")
    if stype == "base64":
        mt = src.get("media_type", "")
        data = src.get("data", "")
        if mt and data:
            return f"data:{mt};base64,{data}"
    elif stype == "url":
        u = src.get("url", "")
        if u and _looks_like_image_url(u):
            return u
    return ""


def _looks_like_image_url(url: str) -> bool:
    return url.startswith("data:") or url.startswith("http://") or url.startswith("https://")


def build_user_message(text: str, images: list[str] = None) -> dict:
    if images:
        contents = []
        for url in images:
            contents.append({"type": "image_url", "image_url": {"url": url}})
        if text or not contents:
            contents.append({"type": "text", "text": text})
        return {
            "role": "user",
            "content": "",
            "contents": contents,
            "response_meta": _blank_response_meta(),
            "reasoning_content_signature": "",
        }
    return {
        "role": "user",
        "content": text,
        "contents": [{"type": "text", "text": text}],
        "response_meta": _blank_response_meta(),
        "reasoning_content_signature": "",
    }


def _blank_response_meta() -> dict:
    return {
        "id": "",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def build_structured_message(role: str, text: str) -> dict:
    return {
        "role": role,
        "content": text,
        "contents": [{"type": "text", "text": text}],
        "response_meta": _blank_response_meta(),
        "reasoning_content_signature": "",
    }


def _has_block_type(blocks: list, block_type: str) -> bool:
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == block_type:
            return True
    return False


def convert_incoming_message(msg: dict, tools_enabled: bool = False) -> Optional[dict]:
    role = msg.get("role", "")
    content = msg.get("content")

    if isinstance(content, list) and len(content) > 0:
        first = content[0] if isinstance(content[0], dict) else {}
        first_type = first.get("type", "")

        if role == "assistant" and _has_block_type(content, "tool_use"):
            out = {"role": "assistant", "content": ""}
            tool_calls = []
            text_parts = ""
            for block in content:
                b = block if isinstance(block, dict) else {}
                btype = b.get("type", "")
                if btype == "tool_use":
                    input_json = json.dumps(b.get("input", {}))
                    tool_calls.append({
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": input_json,
                        },
                    })
                elif btype == "text":
                    text_parts += b.get("text", "")
            if text_parts:
                out["content"] = text_parts
            if tool_calls:
                out["tool_calls"] = tool_calls
            return out

        if role == "user" and first_type == "tool_result":
            for block in content:
                b = block if isinstance(block, dict) else {}
                if b.get("type") != "tool_result":
                    continue
                tool_call_id = b.get("tool_use_id", "")
                result_content = ""
                rc = b.get("content")
                if isinstance(rc, str):
                    result_content = rc
                elif isinstance(rc, list):
                    for part in rc:
                        if isinstance(part, dict):
                            result_content += part.get("text", "")
                return {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_content,
                }
            return None

        if first_type in ("thinking", "redacted_thinking"):
            text_parts = ""
            for block in content:
                b = block if isinstance(block, dict) else {}
                if b.get("type") == "text":
                    text_parts += b.get("text", "")
            if not text_parts:
                return None
            return build_structured_message(role, text_parts)

    text = normalize_content(msg)
    images = extract_images(msg)

    if role == "assistant" and tools_enabled and "tool_calls" in msg:
        out = build_structured_message("assistant", text)
        out["tool_calls"] = msg["tool_calls"]
        return out

    if role == "tool":
        out = build_structured_message("tool", text)
        if "name" in msg:
            out["name"] = msg["name"]
        if "tool_call_id" in msg:
            out["tool_call_id"] = msg["tool_call_id"]
        return out

    if not text and not images:
        return None

    if role == "user":
        return build_user_message(text, images)

    return build_structured_message(role, text)


def build_qoder_messages(template_msgs: list, incoming: list,
                         prompt: str, tools_enabled: bool = False) -> list:
    rebuilt = []

    has_system = any(
        isinstance(m, dict) and m.get("role") == "system"
        for m in incoming
    )
    if not has_system and template_msgs:
        for m in template_msgs:
            if isinstance(m, dict) and m.get("role") == "system":
                rebuilt.append(copy.deepcopy(m))

    for m in incoming:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        if role == "user" and isinstance(m.get("content"), list):
            first = m["content"][0] if m["content"] and isinstance(m["content"][0], dict) else {}
            if first.get("type") == "tool_result":
                for block in m["content"]:
                    b = block if isinstance(block, dict) else {}
                    if b.get("type") != "tool_result":
                        continue
                    tool_call_id = b.get("tool_use_id", "")
                    result_content = ""
                    rc = b.get("content")
                    if isinstance(rc, str):
                        result_content = rc
                    elif isinstance(rc, list):
                        for part in rc:
                            if isinstance(part, dict):
                                result_content += part.get("text", "")
                    rebuilt.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result_content,
                    })
                continue
        converted = convert_incoming_message(m, tools_enabled)
        if converted is not None:
            rebuilt.append(converted)

    if not rebuilt and prompt:
        rebuilt.append(build_user_message(prompt))

    return rebuilt


# ── 请求体构建 ────────────────────────────────────────────────────────────

def build_qoder_request_body(payload: dict, template_base: dict = None,
                              proxy: str = "") -> dict:
    """将 OpenAI /v1/chat/completions 请求体转为 Qoder 上游格式。"""
    if template_base is None:
        template_base = _get_template_base()

    body = copy.deepcopy(template_base)

    model = payload.get("model", "")
    agent = client.infer_agent(model)
    mapped = map_model(model, agent)

    body["stream"] = True
    mc = body.get("model_config")
    if isinstance(mc, dict):
        mc["key"] = mapped

    max_tokens = payload.get("max_tokens", 0)
    if max_tokens > 0:
        params = body.get("parameters")
        if isinstance(params, dict):
            params["max_tokens"] = max_tokens

    messages = payload.get("messages", [])
    tools = payload.get("tools")
    tools_enabled = tools is not None and len(tools) > 0

    prompt = ""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i] if isinstance(messages[i], dict) else {}
        if m.get("role") == "user":
            prompt = normalize_content(m)
            if not prompt:
                content = m.get("contents")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            prompt = block.get("text", "")
                            break
            break

    biz = body.get("business")
    if isinstance(biz, dict):
        biz["id"] = cosy.new_uuid()
        biz["begin_at"] = cosy.unix_ms()
        biz["name"] = prompt[:30] if len(prompt) > 30 else prompt

    cc = body.get("chat_context")
    if isinstance(cc, dict):
        txt = cc.get("text")
        if isinstance(txt, dict):
            txt["text"] = prompt
        extra = cc.get("extra")
        if isinstance(extra, dict):
            oc = extra.get("originalContent")
            if isinstance(oc, dict):
                oc["text"] = prompt

    body["messages"] = build_qoder_messages(
        template_base.get("messages", []),
        messages,
        prompt,
        tools_enabled,
    )

    if tools is not None:
        body["tools"] = tools

    return body


# ── Qoder Delta -> OpenAI chunk 转换 ──────────────────────────────────────

def parse_qoder_delta_to_openai(delta: client.Delta, model: str,
                                  _role_sent: dict = None) -> Optional[dict]:
    """把 Qoder Delta 转为 OpenAI chat.completion.chunk 格式。"""
    if delta.is_empty():
        return None

    chunk = {
        "id": "chatcmpl-" + cosy.new_uuid()[:8],
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
    }

    choice = {"index": 0, "delta": {}, "finish_reason": None}

    if delta.content:
        choice["delta"]["content"] = delta.content
    if delta.reasoning:
        choice["delta"]["reasoning_content"] = delta.reasoning
    if delta.tool_calls:
        choice["delta"]["tool_calls"] = delta.tool_calls
    if delta.role:
        if _role_sent is None or not _role_sent.get("sent"):
            choice["delta"]["role"] = delta.role
            if _role_sent is not None:
                _role_sent["sent"] = True

    chunk["choices"] = [choice]

    if delta.input_tokens or delta.output_tokens:
        chunk["usage"] = {
            "prompt_tokens": delta.input_tokens,
            "completion_tokens": delta.output_tokens,
            "total_tokens": delta.input_tokens + delta.output_tokens,
        }

    return chunk


# ── 模型目录缓存 + 按模型区域路由选号 ─────────────────────────────────────

MODEL_CATALOG_TTL = 30 * 60  # 30 分钟


def ensure_catalog(slot: pool.Slot):
    """确保账号的模型目录缓存新鲜。失败也记时间戳，避免每请求重试。"""
    if slot.catalog_at and (time.time() - slot.catalog_at) < MODEL_CATALOG_TTL:
        return
    try:
        ep = client.get_endpoints(slot.region)
        resp = slot.bearer.call_get(ep.model_list_url)
        raw = client.parse_qoder_models(resp)
        if raw:
            keys = {m.get("key", "") for m in raw}
            slot.set_catalog(keys)
        else:
            slot.set_catalog(set())
    except Exception:
        slot.set_catalog(set())


def is_flash_promo_model(model_key: str) -> bool:
    """判断是否为限免/0倍率活动模型（千问 3.8 Flash、QoDuck 等）。"""
    mk = (model_key or "").lower()
    if mk in ("qfmodel", "qmodel_38flash", "qwen3.8-flash", "qwen-flash", "qwen") or ("qwen" in mk and "flash" in mk):
        return True
    if mk in ("qoduck", "lmodel") or "qoduck" in mk:
        return True
    return False

def _safe_pool_size(p: pool.Pool, realm: str = "") -> int:
    try:
        return p.size(realm=realm) if realm else p.size()
    except TypeError:
        return p.size()


def _safe_pool_snapshot(p: pool.Pool, realm: str = "") -> list:
    try:
        return p.snapshot(realm=realm) if realm else p.snapshot()
    except TypeError:
        return p.snapshot()


def pick_slot_for_model(p: pool.Pool, model_key: str,
                         exclude: set = None,
                         realm: str = None) -> tuple[Optional[pool.Slot], bool]:
    """按模型选号：优先选目录里有该模型的账号；都没有则退回任意可用账号。
    若为千问 3.8 Flash 免费活动模型，则跳过当日免费额度已用尽的账号。
    若指定 realm，则仅在对应区域（global 或 cn）内选号。"""
    is_promo = is_flash_promo_model(model_key)

    def filter_fn(s: pool.Slot) -> bool:
        if is_promo and s.flash_free_exhausted:
            return False
        ensure_catalog(s)
        return s.supports_model(model_key)

    def _do_pick(ex, fn=None):
        if realm:
            try:
                return p.pick(ex, filter_fn=fn, realm=realm)
            except TypeError:
                pass
        return p.pick(ex, filter_fn=fn)

    slot, ok = _do_pick(exclude, filter_fn)
    if ok:
        return slot, True
    if is_promo:
        # Flash 模型不回退到 flash_free_exhausted 的账号
        return _do_pick(exclude, lambda s: not s.flash_free_exhausted)
    return _do_pick(exclude)
# ── 信封重试闸门 (streamWithSlot) ──────────────────────────────────────────

TRANSIENT_MAX_RETRIES = 2


def _is_retryable_stream_error(err) -> bool:
    if err is None:
        return False
    if isinstance(err, pool.UpstreamError):
        detail = getattr(err, "detail", "") or ""
        # provider_error 只换号、不原地重试：单次尝试可能耗时上百秒，
        # 而换号会拿到新 request_id + 新会话，实测可恢复。
        if client.is_provider_error(detail):
            return False
        return pool.is_transient_upstream(err.status, detail)
    return pool.is_transient_transport(err)


def stream_with_slot(slot: pool.Slot, qurl: str, body: dict, extra: dict,
                     on_delta) -> tuple[bool, Optional[Exception]]:
    """在单个账号上执行 SSE 信封重试闸门。

    连接阶段错误已由 open_stream 内部重试；此处是第二层防护（最多
    TRANSIENT_MAX_RETRIES 次），覆盖上游 HTTP200 建流后才在信封里投递
    418/5xx 的形态。返回 (emitted_any, error)。
    """
    last_err = None
    emitted_any = False

    for attempt in range(TRANSIENT_MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(min(2 ** attempt, 4))
        emitted = False
        upstream_err = [None]

        def on_delta_wrapped(delta: client.Delta):
            nonlocal emitted, emitted_any
            if not delta.is_empty():
                emitted = True
                emitted_any = True
            on_delta(delta)

        def on_line(line: str) -> bool:
            if upstream_err[0] is not None:
                return False
            if not line.startswith("data:"):
                return True
            data_payload = line[5:].strip()
            if data_payload == "[DONE]":
                return True
            try:
                delta = client.extract_delta(data_payload)
            except Exception as e:
                upstream_err[0] = pool.UpstreamError(0, str(e))
                return False
            if delta.err is not None:
                upstream_err[0] = delta.err
                return False
            if not delta.is_empty():
                on_delta_wrapped(delta)
            return True

        stream_err = None
        try:
            slot.bearer.open_stream(qurl, body, on_line, extra)
        except Exception as e:
            stream_err = e

        if upstream_err[0] is None and stream_err is None:
            if not emitted:
                return emitted_any, pool.UpstreamError(0, "empty upstream stream")
            return emitted_any, None

        last_err = upstream_err[0] or stream_err
        if emitted or attempt >= TRANSIENT_MAX_RETRIES or not _is_retryable_stream_error(last_err):
            return emitted_any, last_err

    return emitted_any, last_err


#F ── 请求体脱敏日志 ────────────────────────────────────────────────────────

_SENSITIVE_FIELDS = {"content", "text", "system", "input", "instructions", "prompt", "description"}


def redact_request_body_json(raw: str) -> str:
    """脱敏请求体中的敏感字段，用于日志输出。"""
    try:
        v = json.loads(raw)
    except Exception:
        return raw
    _redact_value(v, "")
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return raw


def _redact_value(v, parent_field: str):
    if isinstance(v, dict):
        for k, child in v.items():
            if isinstance(child, str):
                if (_SENSITIVE_FIELDS.__contains__(k) or _SENSITIVE_FIELDS.__contains__(parent_field)) and len(child) > 32:
                    v[k] = "<redacted len=%d>" % len(child)
            else:
                _redact_value(child, k)
    elif isinstance(v, list):
        for i, item in enumerate(v):
            if isinstance(item, str):
                if _SENSITIVE_FIELDS.__contains__(parent_field) and len(item) > 32:
                    v[i] = "<redacted len=%d>" % len(item)
            else:
                _redact_value(item, parent_field)


# ── 核心调用：账号池选号 + SSE 流式 + 失败换号 ──────────────────────────

def call_qoder(payload: dict, on_delta=None, proxy: str = "", realm: str = "") -> tuple[int, str]:
    """调用 Qoder 上游，返回 (status_code, response_body)。

    on_delta: 回调函数，接收 OpenAI chunk dict，用于 SSE 流式转发。
    如果 on_delta 为 None，则收集所有 delta 后一次性返回。
    """
    if not realm:
        realm = payload.get("realm", "")

    p = get_pool(proxy)
    if _safe_pool_size(p, realm=realm) == 0:
        msg = f"未配置或无启用的 Qoder {realm} 账号，请在控制台 (/qoder/dashboard) 添加对应区域账号。" if realm else NO_ACCOUNT_MESSAGE
        return 503, json.dumps({"error": {"type": "no_available_account",
                                           "message": msg}})

    model = payload.get("model", "")
    _t0 = time.time()
    agent = client.infer_agent(model)
    mapped = map_model(model, agent)
    template_base = _get_template_base()

    body = build_qoder_request_body(payload, template_base, proxy)

    extra = {
        "x-model-key": mapped,
        "x-model-source": "system",
    }

    tried: set[str] = set()
    refreshed: set[str] = set()
    last_err = None
    last_slot_id = ""
    collected_content = []
    collected_usage = {}
    waited = 0
    queue_failed_accounts = 0

    while True:
        slot, ok = pick_slot_for_model(p, mapped, tried, realm=realm)
        if not ok:
            break

        last_slot_id = slot.id
        ep = client.get_endpoints(slot.region)
        qurl = ep.chat_stream_url

        emitted_any = False
        err = None

        while True:
            nid = cosy.new_uuid()
            body["request_id"] = nid
            body["chat_record_id"] = nid
            body["request_set_id"] = cosy.new_uuid()
            body["session_id"] = cosy.new_uuid()
            body["aliyun_user_type"] = slot.sess.identity.user_type if slot.sess and slot.sess.identity else "personal_standard"

            biz = body.get("business")
            if isinstance(biz, dict):
                biz["id"] = cosy.new_uuid()
                biz["begin_at"] = cosy.unix_ms()

            role_sent = {}

            def on_delta_raw(delta: client.Delta):
                if on_delta:
                    chunk = parse_qoder_delta_to_openai(delta, model, role_sent)
                    if chunk:
                        on_delta(chunk)
                else:
                    if delta.content:
                        collected_content.append(delta.content)
                    if delta.input_tokens or delta.output_tokens:
                        collected_usage.setdefault("prompt_tokens", 0)
                        collected_usage.setdefault("completion_tokens", 0)
                        collected_usage["prompt_tokens"] += delta.input_tokens
                        collected_usage["completion_tokens"] += delta.output_tokens

            emitted_any, err = stream_with_slot(slot, qurl, body, extra, on_delta_raw)
            if err is None:
                break
            # 排队限流（10605 / isQueued）：旧逻辑会原地等待最多 2 次 × 60s，
            # 客户端在这段时间里完全没有输出，看起来就是卡死。现在默认不等待
            # （QUEUE_MAX_WAITS=0）：立刻把排队错误交回网关，由网关回 429 +
            # Retry-After，客户端自己决定何时重试。已开始出内容就不再等。
            if not emitted_any and pool.is_queued_error(err):
                delay = pool.retry_after_seconds(err) or QUEUE_DEFAULT_WAIT_SECONDS
                if waited < QUEUE_MAX_WAITS:
                    waited += 1
                    log("[queued] upstream=qoder model=%s account=%s retry_in=%ds (%d/%d)"
                        % (model, slot.id, delay, waited, QUEUE_MAX_WAITS))
                    time.sleep(min(delay, QUEUE_MAX_WAIT_SECONDS))
                    continue
                log("[queued] upstream=qoder model=%s account=%s action=fast-fail retry_after=%ds"
                    % (model, slot.id, delay))
                queue_failed_accounts += 1
                break
            # token 失效 → 尝试凭证轮换（dt- ↔ drt-）后重试同一账号，每账号一次
            if (not emitted_any and pool.is_token_expired(err)
                    and slot.id not in refreshed):
                refreshed.add(slot.id)
                if pool.rotate_slot_credential(slot, proxy):
                    continue
            break

        if err is None:
            p.note_success(slot.id)
            pt = collected_usage.get("prompt_tokens", 0)
            ct = collected_usage.get("completion_tokens", 0)
            record_qoder_usage(model, slot.id, on_delta is not None, "completed",
                               int((time.time() - _t0) * 1000), pt, ct, pt + ct)
            return 200, json.dumps({
                "id": "chatcmpl-" + cosy.new_uuid()[:8],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(collected_content)},
                    "finish_reason": "stop",
                }],
                "usage": collected_usage or None,
            })

        last_err = err
        if emitted_any:
            break
        if pool.is_queued_error(err) and queue_failed_accounts >= QUEUE_MAX_ACCOUNTS:
            log("[queued] upstream=qoder model=%s action=stop-failover after %d queued account(s)"
                % (model, queue_failed_accounts))
            break

        # 千问3.8 Flash 每日免费额度用尽：标记该账号 Flash 额度耗尽，记录日志，并换号重试
        if pool.is_flash_free_exhausted(err):
            p.mark_flash_free_exhausted(slot.id, str(err))
            log("qoder: slot '%s' flash free promo quota exhausted for today, marked and failover" % slot.id)
            tried.add(slot.id)
            continue

        if not pool.should_failover(err):
            break
        p.cool(slot.id, pool.cooldown_for(err), str(err))
        tried.add(slot.id)

    if last_err is None:
        last_err = pool.NoAccountError()

    msg, etype = _friendly_error(last_err, p, mapped, realm=realm)
    status = pool.error_status(last_err, etype)
    if pool.is_flash_free_exhausted(last_err) or etype == "flash_free_quota_exhausted":
        status = 429
    # 上游排队（10605 / isQueued）是临时不可用，必须回 429 让客户端退避重试。
    # 若按上游原始的 403 抛出去，网关会把它当成封禁/持久失败，进而触发跨厂商
    # 兜底，把另一家也拖进同一个拥堵窗口。
    if pool.is_queued_error(last_err):
        status = 429
    record_qoder_usage(model, last_slot_id, on_delta is not None, "failed",
                       int((time.time() - _t0) * 1000), 0, 0, 0, str(last_err))
    return status, json.dumps({"error": {"type": etype, "message": msg}})


def _friendly_error(err: Exception, p: Optional[pool.Pool] = None,
                    model_key: str = "", realm: str = "") -> tuple[str, str]:
    if pool.is_flash_free_exhausted(err):
        rem_s = int(max(0, pool.get_next_utc8_midnight() - time.time()))
        hrs = rem_s // 3600
        mins = (rem_s % 3600) // 60
        time_str = f"{hrs}小时{mins}分" if hrs > 0 else f"{mins}分钟"
        model_name = "QoDuck (lmodel)" if model_key in ("lmodel", "qoduck") else "千问3.8 Flash"
        return (
            f"当前 Qoder 账号 {model_name} 当日免费额度已用尽（上游提示：{getattr(err, 'detail', '') or str(err)}）。"
            f"免费活动额度将于次日 00:00 (UTC+8) 自动刷新恢复（约剩余 {time_str}）。"
            "如需继续使用，请在控制台添加新账号或切换至其他模型。",
            "flash_free_quota_exhausted",
        )
    if isinstance(err, pool.NoAccountError):
        if p and _safe_pool_size(p, realm=realm) > 0:
            slots = _safe_pool_snapshot(p, realm=realm)
            if is_flash_promo_model(model_key):
                all_flash_exhausted = all(s.flash_free_exhausted for s in slots if s.enabled)
                if all_flash_exhausted:
                    rem_s = int(max(0, pool.get_next_utc8_midnight() - time.time()))
                    hrs = rem_s // 3600
                    mins = (rem_s % 3600) // 60
                    time_str = f"{hrs}小时{mins}分" if hrs > 0 else f"{mins}分钟"
                    return (
                        f"所有可用 Qoder 账号今日千问3.8 Flash (Qwen3.8-Flash) 免费额度均已耗尽。"
                        f"该活动额度将于次日 00:00 (UTC+8) 自动刷新恢复（约剩余 {time_str}）。"
                        "如需立即使用，请访问控制台 http://127.0.0.1:28087/（点击【Qoder 看板】）添加新账号，或切换至其他模型。",
                        "flash_free_quota_exhausted",
                    )
            cool_times = [s.cooling_remaining_ms for s in slots if s.cooling_remaining_ms > 0]
            if cool_times and len(cool_times) == len(slots):
                min_s = int(min(cool_times) / 1000)
                realm_desc = f" ({realm})" if realm else ""
                return (
                    f"所有 Qoder{realm_desc} 账号当前处于冷却中（预计最快将在 {min_s} 秒后自动恢复）。"
                    "如需立即使用，请访问控制台 http://127.0.0.1:28087/（点击【Qoder 看板】）扫码登录新账号；或者稍候等待冷却结束。",
                    "no_available_account",
                )
        if realm:
            return f"没有可用的 Qoder {realm} 账号（或当前区域未配置/未启用账号）。", "no_available_account"
        return NO_ACCOUNT_MESSAGE, "no_available_account"
    if isinstance(err, (pool.UpstreamError, client.UpstreamError)):
        # 交给 pool 统一格式化：解构 provider_error 内层真实原因，完整透传
        # 上游原文（不再截断），并给出可恢复的提示。
        msg, etype = pool.friendly_upstream_error(
            err.status, getattr(err, "detail", "") or "",
            getattr(err, "err_type", "") or "")
        return msg, etype
    return str(err), "qoder_error"

# ── 模型列表 ──────────────────────────────────────────────────────────────

def qoder_models(proxy: str = "", realm: str = "") -> list[dict]:
    """获取 Qoder 模型列表，返回 OpenAI /v1/models 格式（支持按 realm 过滤）。

    合并模式（realm 为空）下按模型 id 去重，`region` 记录首次出现的区域，
    `regions` 列出该模型实际存在的全部区域，供前端判定「双域支持 / 区域独有」。
    """
    p = get_pool(proxy)
    models = []
    by_id = {}
    target_realm = pool.normalize_realm(realm)

    regions_to_probe = [target_realm] if target_realm else ["cn", "global"]

    for reg in regions_to_probe:
        slots = sorted(p.snapshot(realm=reg), key=lambda s: 0 if s.available() else 1)
        for slot in slots:
            try:
                ep = client.get_endpoints(slot.region)
                resp = slot.bearer.call_get(ep.model_list_url)
                raw = client.parse_qoder_models(resp)
                if not raw:
                    continue
                for m in raw:
                    mid = m.get("display_name") or m.get("key", "")
                    entry = by_id.get(mid)
                    if entry is None:
                        entry = {
                            "id": mid,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "qoder",
                            "region": slot.region,
                            "regions": [slot.region],
                            "qoder_key": m.get("key", ""),
                            "is_reasoning": m.get("is_reasoning", False),
                            "context_window": m.get("context_window", 0),
                            "price_factor": m.get("price_factor"),
                            "max_output_tokens": m.get("max_output_tokens", 0),
                        }
                        by_id[mid] = entry
                        models.append(entry)
                    elif slot.region not in entry["regions"]:
                        entry["regions"].append(slot.region)
                break
            except Exception:
                continue

    # ── 常驻免费模型 QoDuck 补齐（即使上游动态目录隐藏它，底层依然完全支持调用）──
    has_lmodel = any(m.get("qoder_key") == "lmodel" or str(m.get("id")).lower() in ("qoduck", "lmodel") for m in models)
    if not has_lmodel:
        models.insert(0, {
            "id": "QoDuck",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "qoder",
            "region": "cn",
            "regions": ["cn", "global"],
            "qoder_key": "lmodel",
            "is_reasoning": False,
            "context_window": 131072,
            "price_factor": 0.0,
            "max_output_tokens": 16384,
        })
    return models


def probe_free_quota(realm: str = "", model: str = "qoduck", proxy: str = "") -> dict:
    """针对池内账号探测免费模型（如 QoDuck / Qwen3.8-Flash）的当日额度可用性。"""
    p = get_pool(proxy)
    target_realm = pool.normalize_realm(realm)
    slots = _safe_pool_snapshot(p, realm=target_realm) if target_realm else _safe_pool_snapshot(p)
    slots = [s for s in slots if s.enabled]

    mapped = map_model(model, "chat")
    template_base = _get_template_base()
    results = []
    avail_count = 0
    exhaust_count = 0
    queued_count = 0

    for slot in slots:
        ep = client.get_endpoints(slot.region)
        qurl = ep.chat_stream_url

        if slot.flash_free_exhausted:
            exhaust_count += 1
            results.append({
                "id": slot.id,
                "name": slot.name,
                "region": slot.region,
                "status": "exhausted",
                "exhausted": True,
                "cached": True,
                "reset_at": slot.flash_free_reset_at,
                "error": slot.last_error or "今日免费额度已耗尽",
            })
            continue

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "1"}],
            "max_tokens": 1,
            "temperature": 0.1,
        }
        body = build_qoder_request_body(payload, template_base, proxy)
        nid = cosy.new_uuid()
        body["request_id"] = nid
        body["chat_record_id"] = nid
        body["request_set_id"] = cosy.new_uuid()
        body["session_id"] = cosy.new_uuid()
        body["aliyun_user_type"] = slot.sess.identity.user_type if slot.sess and slot.sess.identity else "personal_standard"
        biz = body.get("business")
        if isinstance(biz, dict):
            biz["id"] = cosy.new_uuid()
            biz["begin_at"] = cosy.unix_ms()

        extra = {
            "x-model-key": mapped,
            "x-model-source": "system",
        }

        t0 = time.time()
        emitted_any, err = stream_with_slot(slot, qurl, body, extra, lambda delta: None)
        latency_ms = int((time.time() - t0) * 1000)

        if err is None:
            slot.clear_flash_free_exhausted()
            avail_count += 1
            results.append({
                "id": slot.id,
                "name": slot.name,
                "region": slot.region,
                "status": "available",
                "exhausted": False,
                "latency_ms": latency_ms,
            })
        elif pool.is_flash_free_exhausted(err):
            p.mark_flash_free_exhausted(slot.id, str(err))
            exhaust_count += 1
            results.append({
                "id": slot.id,
                "name": slot.name,
                "region": slot.region,
                "status": "exhausted",
                "exhausted": True,
                "reset_at": slot.flash_free_reset_at,
                "error": str(err),
            })
        elif pool.is_queued_error(err):
            queued_count += 1
            results.append({
                "id": slot.id,
                "name": slot.name,
                "region": slot.region,
                "status": "queued",
                "exhausted": False,
                "error": "上游排队中（并发资源限制，非额度耗尽）",
                "latency_ms": latency_ms,
            })
        else:
            results.append({
                "id": slot.id,
                "name": slot.name,
                "region": slot.region,
                "status": "error",
                "exhausted": False,
                "error": str(err),
                "latency_ms": latency_ms,
            })

    return {
        "ok": True,
        "model": model,
        "mapped_model": mapped,
        "results": results,
        "summary": {
            "total": len(slots),
            "available": avail_count,
            "exhausted": exhaust_count,
            "queued": queued_count,
        }
    }


# ── 自测 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== qoder_route.py 自测 ===")

    # Test 1: map_model
    assert map_model("claude-sonnet-4-6") == "gmodel"
    assert map_model("claude-opus-4-6-thinking") == "qmodel_38max"
    assert map_model("gpt-5") == "dmodel"
    print("Test 1 PASS: map_model")

    # Test 2: normalize_content
    assert normalize_content({"content": "hello"}) == "hello"
    assert normalize_content({"content": [{"type": "text", "text": "hi"}]}) == "hi"
    assert normalize_content({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "a\n\nb"
    print("Test 2 PASS: normalize_content")

    # Test 3: extract_images
    imgs = extract_images({"content": [{"type": "image_url", "image_url": {"url": "https://x.com/a.png"}}]})
    assert imgs == ["https://x.com/a.png"]
    imgs2 = extract_images({"content": [{"type": "text", "text": "hello"}]})
    assert imgs2 == []
    print("Test 3 PASS: extract_images")

    # Test 4: build_user_message
    msg = build_user_message("hello")
    assert msg["role"] == "user"
    assert msg["content"] == "hello"
    msg2 = build_user_message("hello", ["https://x.com/a.png"])
    assert msg2["contents"][0]["type"] == "image_url"
    assert msg2["contents"][1]["type"] == "text"
    print("Test 4 PASS: build_user_message")

    # Test 5: convert_incoming_message
    result = convert_incoming_message({"role": "user", "content": "hi"})
    assert result is not None and result["role"] == "user"
    result2 = convert_incoming_message({"role": "system", "content": "sys prompt"})
    assert result2 is not None and result2["role"] == "system"
    result3 = convert_incoming_message({"role": "assistant", "content": [{"type": "text", "text": "resp"}]})
    assert result3 is not None and result3["role"] == "assistant"
    print("Test 5 PASS: convert_incoming_message")

    # Test 6: build_qoder_messages
    msgs = build_qoder_messages([], [{"role": "user", "content": "hello"}], "hello")
    assert len(msgs) >= 1
    assert msgs[-1]["role"] == "user"
    print("Test 6 PASS: build_qoder_messages")

    # Test 7: build_qoder_request_body
    body = build_qoder_request_body({"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]})
    assert body["stream"] is True
    assert body.get("model_config", {}).get("key") == "gmodel"
    assert "messages" in body

    print("Test 7 PASS: build_qoder_request_body")

    # Test 8: parse_qoder_delta_to_openai
    delta = client.Delta(content="hello")
    chunk = parse_qoder_delta_to_openai(delta, "claude-sonnet-4-6")
    assert chunk is not None
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"][0]["delta"]["content"] == "hello"
    print("Test 8 PASS: parse_qoder_delta_to_openai")

    # Test 8b: _role_sent dedup
    rs = {}
    d1 = client.Delta(role="assistant", content="first")
    c1 = parse_qoder_delta_to_openai(d1, "m", rs)
    assert c1["choices"][0]["delta"].get("role") == "assistant"
    d2 = client.Delta(role="assistant", content="second")
    c2 = parse_qoder_delta_to_openai(d2, "m", rs)
    assert "role" not in c2["choices"][0]["delta"]
    assert c2["choices"][0]["delta"]["content"] == "second"
    print("Test 8b PASS: _role_sent dedup")

    # Test 9: _friendly_error
    msg, etype = _friendly_error(pool.NoAccountError())
    assert etype == "no_available_account"
    msg2, etype2 = _friendly_error(pool.UpstreamError(401, "unauthorized"))
    assert etype2 == "upstream_error"
    print("Test 9 PASS: _friendly_error")

    # Test 10: qoder_models (needs network, skip if no accounts)
    try:
        models = qoder_models()
        print(f"Test 10: qoder_models returned {len(models)} models")
    except Exception as e:
        print(f"Test 10 SKIP: qoder_models (no network/accounts): {e}")

    # Test 11: Flash free promo routing & friendly error
    assert is_flash_promo_model("qfmodel")
    assert is_flash_promo_model("qwen3.8-flash")
    assert is_flash_promo_model("qmodel_38flash")
    assert not is_flash_promo_model("gmodel")
    assert not is_flash_promo_model("dmodel")

    s_normal = pool.Slot(id="normal", name="Normal", region="cn")
    s_flash_out = pool.Slot(id="flash_out", name="FlashOut", region="cn")
    s_flash_out.mark_flash_free_exhausted()
    p_test = pool.Pool([s_flash_out, s_normal])

    # For flash promo model, should skip s_flash_out and pick s_normal
    picked_s, ok = pick_slot_for_model(p_test, "qfmodel")
    assert ok and picked_s.id == "normal"

    # For general model (e.g. gmodel), s_flash_out is still selectable!
    picked_gen, ok_gen = pick_slot_for_model(p_test, "gmodel", exclude={"normal"})
    assert ok_gen and picked_gen.id == "flash_out"

    # When all flash slots exhausted
    s_normal.mark_flash_free_exhausted()
    picked_none, ok_none = pick_slot_for_model(p_test, "qfmodel")
    assert not ok_none and picked_none is None

    msg_flash, etype_flash = _friendly_error(pool.NoAccountError(), p_test, model_key="qfmodel")
    assert etype_flash == "flash_free_quota_exhausted"
    assert "免费额度均已耗尽" in msg_flash

    err_exhaust = pool.UpstreamError(0, "今日当前模型免费使用次数已用尽")
    msg_err, etype_err = _friendly_error(err_exhaust, p_test, model_key="qfmodel")
    assert etype_err == "flash_free_quota_exhausted"
    print("Test 11 PASS: Flash free promo routing & friendly error")

    print("\n=== All 10 core tests PASSED ===")