"""CodeArts 上游路由 —— 把 CodeArts (Snap) 包装成 OpenAI 兼容端点。

本仓库的第五个上游。与 ``trae/``、``qoder/`` 同一套形状：一个模块负责
协议，网关只按 ``codearts-`` 前缀分流，不把上游细节泄进 ``wb_proxy.py``。

**为什么必须带前缀**：CodeArts 同时提供 ``deepseek-v4.1-flash`` 和
``deepseek-v4.1-flash-1017``，而前者正是网关额度不足时的降级目标。
不加 ``codearts-`` 前缀会让同名模型把请求路由到错误的上游。

**模型名映射**：``codearts-kimi-k3`` ↔ 上游 ``kimi-k3``。前缀只在本仓库
内部存在，绝不发往上游。

自测：``python codearts/codearts_route.py``
"""

from __future__ import annotations

import json
import os
import time

import codearts_client as client
import codearts_session as session_store

MODEL_PREFIX = "codearts-"

# 本地展示用清单：id 带前缀，其余字段对齐本仓库 /v1/models 的输出习惯。
# 上游的真实清单以 /v1/model/builtin 为准，这里只是断网时的底座。
_BUILTIN_MODELS = [
    ("deepseek-v4.1-flash-1017", "DeepSeek V4.1 Flash 1017", 1048576, 65536, False),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash", 1048576, 65536, False),
    ("glm-5.3", "GLM-5.3", 1048576, 65536, False),
    ("GLM-5.2", "GLM-5.2", 202752, 65536, False),
    ("kimi-k3", "Kimi K3", 1048576, 65536, False),
    ("kimi-k2.6-vl", "Kimi K2.6 VL", 202752, 65536, True),
    ("Qwen3-VL-235B", "Qwen3-VL-235B", 1048576, 131072, True),
    ("openpangu-2.0-pro", "Pangu 2.0 Pro", 524288, 65535, False),
    ("openpangu-2.0-flash", "Pangu 2.0 Flash", 524288, 65535, False),
    ("glm-5.2-sft-harmony", "GLM-5.2 Harmony", 202752, 65535, False),
]


def is_codearts_model(model):
    return str(model or "").startswith(MODEL_PREFIX)


def upstream_model(model):
    """``codearts-kimi-k3`` → ``kimi-k3``。"""
    text = str(model or "")
    return text[len(MODEL_PREFIX):] if text.startswith(MODEL_PREFIX) else text


def local_model(model):
    """``kimi-k3`` → ``codearts-kimi-k3``（幂等）。"""
    text = str(model or "")
    return text if text.startswith(MODEL_PREFIX) else MODEL_PREFIX + text


def load_session():
    """读取本机 IDE 登录态。失败抛 codearts_session 里的具体异常。"""
    return session_store.load_session()


def is_expired(session):
    """STS 是否已过期；无法判断时返回 False。"""
    return session_store.is_expired(session)


# 网关只需要「是不是凭据问题」这一个分支，但不该深入 client 模块的内部结构。
CodeArtsError = client.CodeArtsError
CodeArtsCredentialError = client.CodeArtsCredentialError
fetch_credits = client.fetch_credits


def catalog_models():
    """给 /v1/models 与 /codearts/models 用的模型清单。

    数据来自本机 IDE 的插件配置快照，因此不需要联网也不依赖 STS 是否有效 ——
    登录过期时仍然能列出有哪些模型可用。
    """
    entries = []
    for model_id, label, context, max_out, vision in _BUILTIN_MODELS:
        entries.append({
            "id": local_model(model_id),
            "object": "model",
            "owned_by": "codearts",
            "name": label,
            "context_window": context,
            "max_output_tokens": max_out,
            "supports_images": vision,
            "supports_tool_calls": True,
        })
    return entries


def build_request(payload):
    """把 OpenAI 请求体改写成上游能接受的样子。只动必须动的地方。"""
    body = dict(payload or {})
    model = upstream_model(body.get("model"))
    body["model"] = model
    # 本仓库内部的字段，上游不认识。
    for key in ("_namespace_map", "stream_options"):
        body.pop(key, None)
    if model in client.STREAM_ONLY_MODELS:
        # glm-5.3 非流式回 400 空 body；强制流式后由调用方本地聚合。
        body["stream"] = True
    return body


def complete(payload, session=None, timeout=600, proxy=""):
    """非流式对话。``glm-5.3`` 会自动走流式再聚合。"""
    body = build_request(payload)
    session = session or load_session()
    if body.get("stream"):
        return _aggregate_stream(body, session, timeout, proxy)
    return client.call_chat(body, session, timeout=timeout, proxy=proxy)


def _aggregate_stream(body, session, timeout, proxy):
    """把上游 SSE 聚合成一个非流式响应体（glm-5.3 只能流式）。"""
    response = client.stream_chat(body, session, timeout=timeout, proxy=proxy)
    text_parts, reasoning_parts, usage = [], [], None
    finish_reason = None
    try:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                frame = json.loads(chunk)
            except ValueError:
                continue
            if frame.get("usage"):
                usage = frame["usage"]
            if frame.get("error_code"):
                client._classify_error(200, chunk)   # 统一翻译成异常
            for choice in frame.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    finally:
        try:
            response.close()
        except Exception:
            pass
    message = {"role": "assistant", "content": "".join(text_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    return {
        "id": "chatcmpl-codearts",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model"),
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {},
    }


def stream(payload, session=None, timeout=600, proxy=""):
    """流式对话，返回上游响应对象（逐行迭代）。"""
    body = build_request(payload)
    body["stream"] = True
    return client.stream_chat(body, session or load_session(),
                              timeout=timeout, proxy=proxy)


def error_payload(exc):
    """把上游异常翻成 OpenAI 错误体，供网关直接写回。"""
    status = getattr(exc, "status", 502) or 502
    message = getattr(exc, "message", None) or str(exc)
    code = getattr(exc, "code", "") or ""
    kind = "credentials_error" if isinstance(exc, client.CodeArtsCredentialError) \
        else "upstream_error"
    return status, {"error": {"message": message, "type": kind,
                              "code": code or None}}
