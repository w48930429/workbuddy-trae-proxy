"""Codex Responses API 格式适配 —— 把 Qoder 包装成 OpenAI /v1/responses 端点。

对应 Go: internal/bridge/codex.go

核心功能：
  - call_qoder_codex(): Codex Responses 请求 -> Qoder 调用 -> Codex SSE/JSON 响应
  - codex_input_to_messages(): Codex input 字段 -> OpenAI messages 格式
  - Codex SSE 事件序列: response.created/response.output_item.added/response.content_part.added/
    response.output_text.delta/response.output_text.done/response.function_call_arguments.delta/
    response.function_call_arguments.done/response.completed
"""

from __future__ import annotations

import json
import time
from typing import Optional

import qoder_client as client
import qoder_cosy as cosy
import qoder_pool as pool
import qoder_route as route


def codex_input_to_messages(input_val, instructions: str = "") -> list:
    """将 Codex Responses API 的 input 字段转换为 OpenAI messages 格式。"""
    msgs = []
    if instructions:
        msgs.append({"role": "system", "content": instructions})

    if isinstance(input_val, str):
        msgs.append({"role": "user", "content": input_val})
        return msgs

    if not isinstance(input_val, list):
        return msgs

    for item in input_val:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "message":
            role = item.get("role", "user")
            content = item.get("content")
            if isinstance(content, str):
                msgs.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        text_parts.append(block["text"])
                msgs.append({"role": role, "content": "\n".join(text_parts)})
        elif item_type == "function_call":
            name = item.get("name", "")
            args = item.get("arguments", "")
            call_id = item.get("call_id", "")
            msgs.append({
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": args}}]})
        elif item_type == "function_call_output":
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            msgs.append({"role": "tool", "tool_call_id": call_id, "content": output})

    return msgs


def call_qoder_codex(req: dict, on_event=None, proxy: str = "") -> tuple[int, str]:
    """处理 Codex /v1/responses 请求，返回 (status_code, response_body)。

    on_event: 回调函数，接收 (event_type, data_dict)，用于 SSE 流式转发。
    """
    stream = bool(req.get("stream"))
    model = req.get("model") or "auto"
    _t0 = time.time()
    instructions = req.get("instructions") or ""
    tools = req.get("tools")
    tools_enabled = tools is not None

    incoming_msgs = codex_input_to_messages(req.get("input"), instructions)
    prompt = route._extract_latest_user_prompt(incoming_msgs) if hasattr(route, '_extract_latest_user_prompt') else ""
    if not prompt:
        for i in range(len(incoming_msgs) - 1, -1, -1):
            m = incoming_msgs[i] if isinstance(incoming_msgs[i], dict) else {}
            if m.get("role") == "user":
                prompt = route.normalize_content(m)
                break

    openai_payload = {
        "model": model,
        "messages": incoming_msgs,
        "stream": True,
    }
    if tools:
        openai_payload["tools"] = tools

    body = route.build_qoder_request_body(openai_payload, route._get_template_base(), proxy)

    p = route.get_pool(proxy)
    if p.size() == 0:
        return 503, json.dumps({"error": {"message": route.NO_ACCOUNT_MESSAGE, "type": "server_error"}})

    agent = client.infer_agent(model)
    mapped = route.map_model(model, agent)
    extra = {"x-model-key": mapped, "x-model-source": "system"}

    tried: set[str] = set()
    resp_id = "resp_" + cosy.new_uuid()[:12]

    if stream:
        tool_call_buf = []

        def on_delta(delta: client.Delta):
            if delta.content:
                on_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": output_item_id,
                    "output_index": 0, "content_index": 0,
                    "delta": delta.content})
            if delta.tool_calls:
                tool_call_buf.extend(delta.tool_calls)

        output_item_id = "msg_" + cosy.new_uuid()[:12]
        content_part_id = "cp_" + cosy.new_uuid()[:12]

        on_event("response.created", {
            "type": "response.created",
            "response": {"id": resp_id, "model": model, "status": "in_progress", "output": []}})
        on_event("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {"id": output_item_id, "type": "message", "role": "assistant",
                     "status": "in_progress", "content": []}})
        on_event("response.content_part.added", {
            "type": "response.content_part.added",
            "item_id": output_item_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": ""}})

        status, body_str = _call_with_pool(p, body, extra, mapped, tried, on_delta)
        if status != 200:
            err_data = json.loads(body_str) if body_str else {}
            err_msg = err_data.get("error", {}).get("message", "unknown error")
            on_event("error", {"type": "error", "message": err_msg})
            route.record_qoder_usage(model, "", stream, "failed",
                                     int((time.time() - _t0) * 1000), 0, 0, 0, err_msg)
            return status, body_str

        on_event("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": output_item_id, "output_index": 0, "content_index": 0})

        for i, tc in enumerate(tool_call_buf):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            call_id = tc.get("id") or ("call_" + cosy.new_uuid()[:12])
            name = fn.get("name", "")
            args = fn.get("arguments", "")

            fc_item_id = "fc_" + cosy.new_uuid()[:12]
            on_event("response.output_item.added", {
                "type": "response.output_item.added", "output_index": i + 1,
                "item": {"id": fc_item_id, "type": "function_call",
                         "call_id": call_id, "name": name, "arguments": "",
                         "status": "in_progress"}})
            on_event("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "item_id": fc_item_id, "output_index": i + 1, "delta": args})
            on_event("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": fc_item_id, "output_index": i + 1, "arguments": args})

        on_event("response.completed", {
            "type": "response.completed",
            "response": {"id": resp_id, "model": model, "status": "completed",
                         "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})

        route.record_qoder_usage(model, "", stream, "completed",
                                 int((time.time() - _t0) * 1000))
        return 200, json.dumps({"status": "streamed"})

    else:
        full_content = []
        tool_call_buf = []

        def on_delta(delta: client.Delta):
            if delta.content:
                full_content.append(delta.content)
            if delta.tool_calls:
                tool_call_buf.extend(delta.tool_calls)

        status, body_str = _call_with_pool(p, body, extra, mapped, tried, on_delta)
        if status != 200:
            route.record_qoder_usage(model, "", stream, "failed",
                                     int((time.time() - _t0) * 1000), 0, 0, 0, body_str[:200])
            return status, body_str

        output = []
        if full_content:
            output.append({
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "".join(full_content)}]})
        for tc in tool_call_buf:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            call_id = tc.get("id") or ("call_" + cosy.new_uuid()[:12])
            name = fn.get("name", "")
            args = fn.get("arguments", "")
            output.append({
                "type": "function_call", "call_id": call_id,
                "name": name, "arguments": args})

        resp = {
            "id": resp_id, "model": model, "status": "completed",
            "output": output,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
        route.record_qoder_usage(model, "", stream, "completed",
                                 int((time.time() - _t0) * 1000))
        return 200, json.dumps(resp)


def _call_with_pool(p: pool.Pool, body: dict, extra: dict, mapped_model: str,
                     tried: set, on_delta) -> tuple[int, str]:
    """账号池选号 + SSE 流式 + 失败换号。"""
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
        etype = "server_error"
    return status, json.dumps({"error": {"message": msg, "type": etype}})


if __name__ == "__main__":
    print("=== qoder_codex.py 自测 ===")

    msgs = codex_input_to_messages("hello", "sys")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "hello"
    print("Test 1 PASS: codex_input_to_messages (string)")

    msgs2 = codex_input_to_messages([
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "function_call", "name": "foo", "arguments": "{}", "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "result"},
    ])
    assert len(msgs2) == 3
    assert msgs2[0]["role"] == "user"
    assert msgs2[1]["role"] == "assistant"
    assert "tool_calls" in msgs2[1]
    assert msgs2[2]["role"] == "tool"
    print("Test 2 PASS: codex_input_to_messages (list)")

    print("\n=== All 2 core tests PASSED ===")