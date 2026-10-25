"""OpenAI Chat Completion to Google CloudCode payload adapter."""
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .models import resolve_dynamic_upstream_model, ThinkingEffort
from .schema import sanitize_google_schema
from .thought_signature import (
    remember_thought_signature,
    resolve_thought_signature_for_replay,
    signed_id,
    unsigned_id,
)

def _parse_data_url(url: str) -> Optional[Tuple[str, str]]:
    """解析 data URL（data:image/png;base64,xxxx）-> (mimeType, base64 数据)。"""
    m = re.match(r"^data:([^;,]*)(;base64)?,(.*)$", url or "", re.S)
    if not m or not m.group(2) or not m.group(3):
        return None
    return (m.group(1) or "image/png", m.group(3))

def _guess_image_mime(url: str) -> str:
    low = (url or "").lower()
    if ".jpg" in low or ".jpeg" in low:
        return "image/jpeg"
    if ".webp" in low:
        return "image/webp"
    if ".gif" in low:
        return "image/gif"
    return "image/png"

def normalize_tool_name(name: Optional[str]) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name or "tool")[:128]
    return cleaned or "tool"

def convert_openai_to_cloudcode(req: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str]:
    """将 OpenAI 格式的 chat completions 请求转换为 Google CloudCode / Gemini 内部 payload."""
    effort: ThinkingEffort = "high"
    budget_tokens: Optional[int] = None

    if req.get("reasoning_effort"):
        effort = req["reasoning_effort"]
    elif req.get("thinking"):
        th = req["thinking"]
        if isinstance(th, dict):
            if th.get("type") == "disabled":
                effort = "none"
            elif isinstance(th.get("budget_tokens"), int):
                budget_tokens = th["budget_tokens"]
                if budget_tokens <= 1500:
                    effort = "low"
                elif budget_tokens <= 5000:
                    effort = "medium"
                else:
                    effort = "high"
    elif req.get("thinking_budget") is not None or req.get("thinkingBudget") is not None:
        budget_tokens = req.get("thinking_budget") or req.get("thinkingBudget")
        if budget_tokens is not None:
            if budget_tokens <= 1500:
                effort = "low"
            elif budget_tokens <= 5000:
                effort = "medium"
            else:
                effort = "high"

    model_name = req.get("model", "gemini-3.8-flash")
    upstream_model, pool = resolve_dynamic_upstream_model(model_name, effort)

    contents: List[Dict[str, Any]] = []
    system_instruction: Optional[Dict[str, Any]] = None
    tool_call_ids: Dict[str, str] = {}
    # 每个函数名下尚未被回应的 call id 队列：tool 结果只给 name 不带 id 时按序配对
    open_by_name: Dict[str, List[str]] = {}

    def append_content_part(parts: List[Dict[str, Any]], part: Any):
        if isinstance(part, str):
            if part:
                parts.append({"text": part})
        elif isinstance(part, dict):
            p_type = part.get("type")
            if p_type == "text" and isinstance(part.get("text"), str):
                if part["text"]:
                    parts.append({"text": part["text"]})
            elif p_type == "image_url":
                # 图片走 Gemini 原生多模态字段：base64 data URL -> inlineData；
                # 远程 URL -> fileData。早期实现把图片降级为文本标记，等于丢弃图像输入。
                url = (part.get("image_url") or {}).get("url") or ""
                data = _parse_data_url(url)
                if data:
                    parts.append({"inlineData": {"mimeType": data[0], "data": data[1]}})
                elif url:
                    parts.append({"fileData": {"mimeType": _guess_image_mime(url), "fileUri": url}})

    for msg in req.get("messages", []):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str):
                text_content = content
            elif isinstance(content, list):
                text_content = "\n".join(
                    p if isinstance(p, str) else (p.get("text") or "") for p in content
                )
            else:
                text_content = str(content or "")
            system_instruction = {"parts": [{"text": text_content}]}

        elif role == "assistant":
            parts: List[Dict[str, Any]] = []
            if isinstance(content, str):
                append_content_part(parts, content)
            elif isinstance(content, list):
                for part in content:
                    append_content_part(parts, part)

            tool_calls = msg.get("tool_calls") or []
            for call in tool_calls:
                func = call.get("function") or {}
                name = normalize_tool_name(func.get("name"))
                raw_call_id = call.get("id")
                # 客户端回传的 id 可能内嵌了上轮 thoughtSignature，先拆出真实 id
                own_id, _ = unsigned_id(raw_call_id)
                call_id = own_id or f"call_{len(contents)}_{len(parts)}"
                tool_call_ids[call_id] = name
                open_by_name.setdefault(name, []).append(call_id)
                raw_args = func.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {"raw_arguments": raw_args}
                parts.append({
                    "functionCall": {"id": call_id, "name": name, "args": args},
                    "thoughtSignature": resolve_thought_signature_for_replay(raw_call_id),
                })
            contents.append({
                "role": "model",
                "parts": parts if parts else [{"text": ""}],
            })

        elif role == "tool":
            own_tool_id, _ = unsigned_id(msg.get("tool_call_id"))
            name = msg.get("name") or tool_call_ids.get(own_tool_id) or "tool"
            # tool 结果没带 id 时，回退到该函数名下最早的未回应 call
            if not own_tool_id:
                queue = open_by_name.get(name)
                if queue:
                    own_tool_id = queue[0]
            queue = open_by_name.get(name)
            if queue and own_tool_id in queue:
                queue.remove(own_tool_id)
            output = content if isinstance(content, str) else json.dumps(content or "")
            contents.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "id": own_tool_id or msg.get("tool_call_id") or "",
                        "name": normalize_tool_name(name),
                        "response": {"output": output},
                    }
                }],
            })

        else:
            parts = []
            if isinstance(content, str):
                append_content_part(parts, content)
            elif isinstance(content, list):
                for part in content:
                    append_content_part(parts, part)
            contents.append({
                "role": "user",
                "parts": parts if parts else [{"text": ""}],
            })

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Hello"}]})

    is_gemini = upstream_model.startswith("gemini")
    default_max_output = 65536 if is_gemini else 8192
    req_max_tokens = req.get("max_tokens") or req.get("max_completion_tokens")
    max_output = min(req_max_tokens, default_max_output) if req_max_tokens else default_max_output

    generation_config: Dict[str, Any] = {
        "temperature": req.get("temperature", 0.7),
        "topP": req.get("top_p", 0.95),
        "maxOutputTokens": max_output,
    }

    if budget_tokens is not None and budget_tokens > 0:
        generation_config["thinkingConfig"] = {"thinkingBudget": budget_tokens}

    inner_request: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }

    if system_instruction:
        inner_request["systemInstruction"] = system_instruction

    req_tools = req.get("tools")
    if isinstance(req_tools, list) and req_tools:
        function_declarations = []
        for tool in req_tools:
            if tool.get("type") == "function" and (tool.get("function") or {}).get("name"):
                f = tool["function"]
                function_declarations.append({
                    "name": normalize_tool_name(f.get("name")),
                    "description": f.get("description") or "",
                    "parameters": sanitize_google_schema(f.get("parameters") or {"type": "object", "properties": {}}),
                })
        if function_declarations:
            inner_request["tools"] = [{"functionDeclarations": function_declarations}]

    tool_choice = req.get("tool_choice")
    if tool_choice and tool_choice != "auto":
        mode = "ANY" if tool_choice == "required" else "AUTO"
        fc_config: Dict[str, Any] = {"mode": mode}
        if isinstance(tool_choice, dict) and (tool_choice.get("function") or {}).get("name"):
            fc_config["allowedFunctionNames"] = [normalize_tool_name(tool_choice["function"]["name"])]
        inner_request["toolConfig"] = {"functionCallingConfig": fc_config}

    payload = {
        "project": "aicode-consumers",
        "model": upstream_model,
        "request": inner_request,
    }

    return payload, upstream_model, pool

def create_openai_chunk(
    chunk_id: str,
    model: str,
    content_delta: Optional[str] = None,
    finish_reason: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """创建标准的 OpenAI Chat Completion Chunk SSE 字符串."""
    delta: Dict[str, Any] = {}
    if content_delta:
        delta["content"] = content_delta
    elif tool_calls:
        delta["tool_calls"] = [
            {
                "index": call.get("index", 0),
                **({"id": call["id"]} if call.get("id") else {}),
                "type": "function",
                "function": {
                    "name": call.get("name", "tool"),
                    "arguments": call.get("arguments", "{}"),
                },
            }
            for call in tool_calls
        ]

    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def extract_text_from_google_chunk(raw_json: str) -> Dict[str, Any]:
    """解析 Google 上游返回的单行 SSE 数据，提取文本片段与函数调用."""
    try:
        parsed = json.loads(raw_json)
        usage_metadata = parsed.get("response", {}).get("usageMetadata") or parsed.get("usageMetadata")
        candidates = parsed.get("response", {}).get("candidates") or parsed.get("candidates") or []
        if not candidates:
            return {"text": "", "functionCalls": [], "usageMetadata": usage_metadata}

        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = ""
        function_calls = []

        for part in parts:
            if part.get("text") and not part.get("thought"):
                text += part["text"]
            if part.get("functionCall"):
                fc = part["functionCall"]
                sig = part.get("thoughtSignature") or part.get("thought_signature")
                raw_id = fc.get("id")
                remember_thought_signature(raw_id, sig)
                function_calls.append({
                    # 签名编码进 id 返回给客户端：客户端原样回传即带回签名，
                    # 不依赖服务端缓存（重启 / 多实例 / 客户端改写 id 时仍可靠）
                    "id": signed_id(raw_id, sig),
                    "name": fc.get("name"),
                    "args": fc.get("args") or {},
                })

        finish_reason = candidate.get("finishReason")
        if finish_reason == "STOP":
            finish_reason = "stop"

        return {
            "text": text,
            "functionCalls": function_calls,
            "finishReason": finish_reason,
            "usageMetadata": usage_metadata,
        }
    except Exception:
        return {"text": "", "functionCalls": []}
convert_openai_to_cloud_code = convert_openai_to_cloudcode