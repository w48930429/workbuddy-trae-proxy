"""Claude Messages API 格式适配 —— 把 Qoder 包装成 Claude /v1/messages 端点。

对应 Go: internal/bridge/claude.go

核心功能：
  - call_qoder_claude(): Claude Messages 请求 -> Qoder 调用 -> Claude SSE/JSON 响应
  - convert_claude_tools_to_openai(): Claude tools 格式 -> OpenAI tools 格式
  - Claude SSE 事件序列: message_start/content_block_start/content_block_delta/content_block_stop/message_delta/message_stop
  - thinking block 支持 (reasoning_delta)
  - tool_use block 支持
"""

from __future__ import annotations

import json
import time
from typing import Optional

import qoder_client as client
import qoder_cosy as cosy
import qoder_pool as pool
import qoder_route as route


def convert_claude_tools_to_openai(raw_tools):
    if not isinstance(raw_tools, list) or len(raw_tools) == 0:
        return None
    converted = []
    for t in raw_tools:
        if not isinstance(t, dict):
            continue
        if "type" in t:
            converted.append(t)
            continue
        fn = {"name": t.get("name", "")}
        if "description" in t:
            fn["description"] = t["description"]
        if "input_schema" in t:
            fn["parameters"] = t["input_schema"]
        converted.append({"type": "function", "function": fn})
    if not converted:
        return None
    return converted


def _extract_system_text(req: dict) -> str:
    sys = req.get("system")
    if sys is None:
        return ""
    if isinstance(sys, str):
        return sys
    if isinstance(sys, list):
        parts = []
        for block in sys:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _extract_latest_user_prompt(messages: list) -> str:
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i] if isinstance(messages[i], dict) else {}
        if m.get("role") == "user":
            return route.normalize_content(m)
    return ""


def _merge_tool_call_chunks(merged: dict, chunks: list):
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        idx = 0
        if "index" in chunk and isinstance(chunk["index"], (int, float)):
            idx = int(chunk["index"])
        if idx not in merged:
            merged[idx] = {}
        existing = merged[idx]
        if chunk.get("id"):
            existing["id"] = chunk["id"]
        if chunk.get("type"):
            existing["type"] = chunk["type"]
        fn = chunk.get("function")
        if isinstance(fn, dict):
            if "function" not in existing:
                existing["function"] = {}
            efn = existing["function"]
            if fn.get("name"):
                efn["name"] = fn["name"]
            if "arguments" in fn:
                efn["arguments"] = efn.get("arguments", "") + fn["arguments"]


def _sorted_tool_calls(merged: dict) -> list:
    if not merged:
        return []
    max_idx = max(merged.keys()) if merged else 0
    result = []
    for i in range(max_idx + 1):
        if i in merged:
            result.append(merged[i])
    return result


def call_qoder_claude(req: dict, on_event=None, proxy: str = "") -> tuple[int, str]:
    """处理 Claude /v1/messages 请求，返回 (status_code, response_body)。

    on_event: 回调函数，接收 (event_type, data_dict)，用于 SSE 流式转发。
    如果 on_event 为 None，则收集所有内容后一次性返回 JSON。
    """
    stream = bool(req.get("stream"))
    model = req.get("model") or "auto"
    _t0 = time.time()
    incoming_msgs = req.get("messages") or []

    tools = convert_claude_tools_to_openai(req.get("tools"))
    tools_enabled = tools is not None

    sys_text = _extract_system_text(req)
    if sys_text:
        incoming_msgs = [{"role": "system", "content": sys_text}] + list(incoming_msgs)

    prompt = _extract_latest_user_prompt(incoming_msgs)

    is_reasoning = False
    thinking = req.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        is_reasoning = True

    max_tokens = 0
    mt = req.get("max_tokens")
    if isinstance(mt, (int, float)) and int(mt) > 0:
        max_tokens = int(mt)

    openai_payload = {
        "model": model,
        "messages": incoming_msgs,
        "stream": True,
    }
    if tools:
        openai_payload["tools"] = tools

    body = route.build_qoder_request_body(openai_payload, route._get_template_base(), proxy)

    if is_reasoning:
        mc = body.get("model_config")
        if isinstance(mc, dict):
            mc["is_reasoning"] = True
    if max_tokens > 0:
        params = body.get("parameters")
        if isinstance(params, dict):
            params["max_tokens"] = max_tokens

    p = route.get_pool(proxy)
    if p.size() == 0:
        return 503, json.dumps({"type": "error", "error": {
            "type": "api_error", "message": route.NO_ACCOUNT_MESSAGE}})

    agent = client.infer_agent(model)
    mapped = route.map_model(model, agent)

    extra = {"x-model-key": mapped, "x-model-source": "system"}

    tried: set[str] = set()
    last_err = None
    msg_id = "msg_" + cosy.new_uuid()[:12]

    if stream:
        total_input_tokens = 0
        total_output_tokens = 0
        content_block_index = 0
        thinking_block_open = False
        tool_call_merged = {}
        stream_content_len = 0

        def on_delta(delta: client.Delta):
            nonlocal content_block_index, thinking_block_open
            nonlocal total_input_tokens, total_output_tokens, stream_content_len

            if delta.input_tokens or delta.output_tokens:
                total_input_tokens = delta.input_tokens
                total_output_tokens = delta.output_tokens

            if delta.reasoning and is_reasoning:
                if not thinking_block_open:
                    on_event("content_block_stop", {
                        "type": "content_block_stop", "index": content_block_index})
                    content_block_index += 1
                    on_event("content_block_start", {
                        "type": "content_block_start", "index": content_block_index,
                        "content_block": {"type": "thinking", "thinking": ""}})
                    thinking_block_open = True
                on_event("content_block_delta", {
                    "type": "content_block_delta", "index": content_block_index,
                    "delta": {"type": "thinking_delta", "thinking": delta.reasoning}})

            if delta.content:
                if thinking_block_open:
                    on_event("content_block_stop", {
                        "type": "content_block_stop", "index": content_block_index})
                    content_block_index += 1
                    on_event("content_block_start", {
                        "type": "content_block_start", "index": content_block_index,
                        "content_block": {"type": "text", "text": ""}})
                    thinking_block_open = False
                stream_content_len += len(delta.content)
                on_event("content_block_delta", {
                    "type": "content_block_delta", "index": content_block_index,
                    "delta": {"type": "text_delta", "text": delta.content}})

            if delta.tool_calls:
                _merge_tool_call_chunks(tool_call_merged, delta.tool_calls)

        on_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant", "model": model,
                "stop_reason": None, "stop_sequence": None,
                "content": [],
                "usage": {"input_tokens": 0, "output_tokens": 0}}})
        on_event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})
        on_event("ping", {"type": "ping"})

        status, body_str = _call_with_pool(p, body, extra, mapped, tried, on_delta)
        if status != 200:
            err_data = json.loads(body_str) if body_str else {}
            err_msg = err_data.get("error", {}).get("message", "unknown error")
            on_event("error", {"type": "error", "error": {"type": "api_error", "message": err_msg}})
            route.record_qoder_usage(model, "", stream, "failed",
                                     int((time.time() - _t0) * 1000), 0, 0, 0, err_msg)
            return status, body_str

        on_event("content_block_stop", {
            "type": "content_block_stop", "index": content_block_index})

        merged_tools = _sorted_tool_calls(tool_call_merged)
        for i, tc in enumerate(merged_tools):
            block_idx = content_block_index + 1 + i
            fn = tc.get("function") or {}
            tool_id = tc.get("id") or ("toolu_" + cosy.new_uuid()[:12])
            name = fn.get("name", "")
            args_str = fn.get("arguments", "")
            try:
                input_val = json.loads(args_str) if args_str else {}
            except Exception:
                input_val = {}

            on_event("content_block_start", {
                "type": "content_block_start", "index": block_idx,
                "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}})
            on_event("content_block_delta", {
                "type": "content_block_delta", "index": block_idx,
                "delta": {"type": "input_json_delta", "partial_json": args_str}})
            on_event("content_block_stop", {
                "type": "content_block_stop", "index": block_idx})

        stop_reason = "tool_use" if merged_tools else "end_turn"
        on_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}})
        on_event("message_stop", {"type": "message_stop"})

        route.record_qoder_usage(model, "", stream, "completed",
                                 int((time.time() - _t0) * 1000),
                                 total_input_tokens, total_output_tokens,
                                 total_input_tokens + total_output_tokens)
        return 200, json.dumps({"status": "streamed"})

    else:
        full_content = []
        reasoning_buf = []
        tool_call_buf = []
        total_input_tokens = 0
        total_output_tokens = 0

        def on_delta(delta: client.Delta):
            nonlocal total_input_tokens, total_output_tokens
            if delta.input_tokens or delta.output_tokens:
                total_input_tokens = delta.input_tokens
                total_output_tokens = delta.output_tokens
            if delta.reasoning:
                reasoning_buf.append(delta.reasoning)
            if delta.content:
                full_content.append(delta.content)
            if delta.tool_calls:
                tool_call_buf.extend(delta.tool_calls)

        status, body_str = _call_with_pool(p, body, extra, mapped, tried, on_delta)
        if status != 200:
            route.record_qoder_usage(model, "", stream, "failed",
                                     int((time.time() - _t0) * 1000), 0, 0, 0, body_str[:200])
            return status, body_str

        content = []
        if reasoning_buf and is_reasoning:
            content.append({"type": "thinking", "thinking": "".join(reasoning_buf)})
        if full_content:
            content.append({"type": "text", "text": "".join(full_content)})
        for tc in tool_call_buf:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            tool_id = tc.get("id") or ("toolu_" + cosy.new_uuid()[:12])
            name = fn.get("name", "")
            args_str = fn.get("arguments", "")
            try:
                input_val = json.loads(args_str) if args_str else {}
            except Exception:
                input_val = {}
            content.append({"type": "tool_use", "id": tool_id, "name": name, "input": input_val})

        stop_reason = "tool_use" if tool_call_buf else "end_turn"
        resp = {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "stop_reason": stop_reason, "stop_sequence": None,
            "content": content,
            "usage": {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}}
        route.record_qoder_usage(model, "", stream, "completed",
                                 int((time.time() - _t0) * 1000),
                                 total_input_tokens, total_output_tokens,
                                 total_input_tokens + total_output_tokens)
        return 200, json.dumps(resp)


def _call_with_pool(p: pool.Pool, body: dict, extra: dict, mapped_model: str,
                     tried: set, on_delta) -> tuple[int, str]:
    """账号池选号 + SSE 流式 + 失败换号（复用 qoder_route 逻辑）。"""
    refreshed: set = set()
    while True:
        slot, ok = route.pick_slot_for_model(p, mapped_model, tried)
        if not ok:
            break

        ep = client.get_endpoints(slot.region)
        qurl = ep.chat_stream_url

        emitted_any = False
        err = None
        for _attempt in range(2):
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

            emitted_any, err = route.stream_with_slot(slot, qurl, body, extra, on_delta)
            if err is None:
                break
            if (not emitted_any and pool.is_token_expired(err)
                    and slot.id not in refreshed):
                refreshed.add(slot.id)
                if pool.rotate_slot_credential(slot):
                    continue
            break

        if err is None:
            p.note_success(slot.id)
            return 200, json.dumps({"status": "ok"})

        last_err = err
        if emitted_any or not pool.should_failover(err):
            break
        p.cool(slot.id, pool.cooldown_for(err), str(err))
        tried.add(slot.id)

    if last_err is None:
        last_err = pool.NoAccountError()
    status = pool.error_status(last_err)
    msg, etype = route._friendly_error(last_err)
    if etype == "qoder_error":
        etype = "api_error"
    return status, json.dumps({"type": "error", "error": {"type": etype, "message": msg}})


def claude_models(proxy: str = "") -> list[dict]:
    """获取 Qoder 模型列表，返回 Claude /v1/models 兼容格式。"""
    p = route.get_pool(proxy)
    models = []
    for slot in p.snapshot():
        if not slot.available():
            continue
        try:
            ep = client.get_endpoints(slot.region)
            resp = slot.bearer.call_get(ep.model_list_url)
            raw = client.parse_qoder_models(resp)
            for m in raw:
                ctx_win = m.get("context_window", 0)
                if ctx_win == 0:
                    ctx_win = m.get("max_input_tokens", 0)
                if ctx_win == 0:
                    ctx_win = 180000
                is_reasoning = m.get("is_reasoning", False)
                max_out = 32768 if is_reasoning else 16384
                models.append({
                    # Same contract as qoder_route.qoder_models(): advertise the
                    # name a client can actually call, keep the internal key
                    # alongside for the dispatcher.
                    "id": m["display_name"] if m.get("display_name") else m.get("key", ""),
                    "qoder_key": m.get("key", ""),
                    "object": "model",
                    "created": 1687864820,
                    "owned_by": "qoder",
                    "display_name": m.get("display_name", ""),
                    "context_window": ctx_win,
                    "max_output_tokens": max_out,
                    "enable": m.get("enable", True),
                    "is_default": m.get("is_default", False),
                    "is_reasoning": is_reasoning,
                    "price_factor": m.get("price_factor", 0),
                })
            break
        except Exception:
            continue
    if not models:
        for key in ["auto", "qmodel_38max", "qfmodel", "qmodel_latest", "qmodel",
                     "q37fmodel", "dmodel", "dfmodel", "gmodel", "gfmodel",
                     "gm51model", "kmodel_latest", "kmodel", "mmodel"]:
            models.append({
                "id": key, "object": "model", "created": 1687864820,
                "owned_by": "qoder", "qoder_key": key, "display_name": key, "context_window": 180000})
    return models


if __name__ == "__main__":
    print("=== qoder_claude.py 自测 ===")

    assert convert_claude_tools_to_openai(None) is None
    assert convert_claude_tools_to_openai([]) is None
    tools = convert_claude_tools_to_openai([{"name": "foo", "description": "bar", "input_schema": {"type": "object"}}])
    assert tools is not None
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "foo"
    print("Test 1 PASS: convert_claude_tools_to_openai")

    assert _extract_system_text({"system": "hello"}) == "hello"
    assert _extract_system_text({"system": [{"text": "a"}, {"text": "b"}]}) == "a\nb"
    assert _extract_system_text({}) == ""
    print("Test 2 PASS: _extract_system_text")

    merged = {}
    _merge_tool_call_chunks(merged, [{"index": 0, "id": "t1", "function": {"name": "foo", "arguments": "{\"a\":"}}])
    _merge_tool_call_chunks(merged, [{"index": 0, "function": {"arguments": "1}"}}])
    sorted_tc = _sorted_tool_calls(merged)
    assert len(sorted_tc) == 1
    assert sorted_tc[0]["id"] == "t1"
    assert sorted_tc[0]["function"]["name"] == "foo"
    assert sorted_tc[0]["function"]["arguments"] == "{\"a\":1}"
    print("Test 3 PASS: _merge_tool_call_chunks / _sorted_tool_calls")

    print("\n=== All 3 core tests PASSED ===")