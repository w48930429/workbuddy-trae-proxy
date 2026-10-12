import json
import re
import secrets
import hashlib
from typing import Dict, Any, List, Optional, Tuple, Callable, Set
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from pool.account_pool import AccountPool
from pool.breaker import RequestExecutor, ContextOverflowError, UpstreamRequestError
from .models import resolve_dynamic_upstream_model, ThinkingEffort
from .schema import sanitize_google_schema
from .thought_signature import remember_thought_signature, resolve_thought_signature_for_replay


def sanitize_tool_name(name: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '_', name or '')[:128]
    return cleaned or 'tool'


def parse_google_stream_chunk(json_str: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(json_str)
        usage_metadata = parsed.get('response', {}).get('usageMetadata') or parsed.get('usageMetadata')
        candidates = parsed.get('response', {}).get('candidates') or parsed.get('candidates') or []
        candidate = candidates[0] if candidates else None

        if not candidate:
            return {'text': '', 'functionCalls': [], 'usageMetadata': usage_metadata, 'finishReason': None}

        parts = candidate.get('content', {}).get('parts', [])
        text = ''
        function_calls = []

        if isinstance(parts, list):
            for part in parts:
                if part.get('text') and not part.get('thought'):
                    text += part['text']
                if part.get('functionCall'):
                    fc = part['functionCall']
                    fc_id = fc.get('id')
                    sig = part.get('thoughtSignature') or part.get('thought_signature')
                    remember_thought_signature(fc_id, sig)
                    function_calls.append({
                        'id': fc_id,
                        'name': fc.get('name', ''),
                        'args': fc.get('args', {})
                    })

        finish_reason = candidate.get('finishReason')
        return {
            'text': text,
            'functionCalls': function_calls,
            'finishReason': finish_reason,
            'usageMetadata': usage_metadata
        }
    except Exception:
        return {'text': '', 'functionCalls': [], 'usageMetadata': None, 'finishReason': None}


def convert_anthropic_messages(
    messages: List[Dict[str, Any]],
    is_gemini: bool = False
) -> Tuple[List[Dict[str, Any]], List[str]]:
    contents: List[Dict[str, Any]] = []
    system_from_messages: List[str] = []
    tool_call_id_to_name: Dict[str, str] = {}

    for msg in messages:
        c = msg.get('content')
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get('type') == 'tool_use' and part.get('id') and part.get('name'):
                    tool_call_id_to_name[part['id']] = part['name']

    for msg in messages:
        role = msg.get('role')
        content = msg.get('content')

        if role == 'system':
            if isinstance(content, str):
                sys_text = content
            elif isinstance(content, list):
                sys_text = '\n'.join(p if isinstance(p, str) else p.get('text', '') for p in content if isinstance(p, (str, dict)))
            else:
                sys_text = str(content or '')
            if sys_text.strip():
                system_from_messages.append(sys_text)
            continue

        google_role = 'model' if role == 'assistant' else 'user'
        parts: List[Dict[str, Any]] = []

        if isinstance(content, str):
            if content:
                parts.append({'text': content})
        elif isinstance(content, list):
            for part in content:
                if not part:
                    continue
                if isinstance(part, str):
                    if part:
                        parts.append({'text': part})
                elif isinstance(part, dict):
                    part_type = part.get('type')
                    if part_type == 'text':
                        txt = part.get('text')
                        if isinstance(txt, str) and txt:
                            parts.append({'text': txt})
                    elif part_type == 'tool_use':
                        fc_part: Dict[str, Any] = {
                            'functionCall': {
                                'id': part.get('id'),
                                'name': sanitize_tool_name(part.get('name', '')),
                                'args': part.get('input') or {}
                            }
                        }
                        if is_gemini:
                            sig = resolve_thought_signature_for_replay(part.get('id'))
                            if sig:
                                fc_part['thoughtSignature'] = sig
                        parts.append(fc_part)
                    elif part_type == 'tool_result':
                        tool_use_id = part.get('tool_use_id', '')
                        c_res = part.get('content')
                        if isinstance(c_res, str):
                            content_str = c_res
                        elif isinstance(c_res, list):
                            content_str = '\n'.join(c if isinstance(c, str) else (c.get('text') or json.dumps(c)) for c in c_res)
                        else:
                            content_str = json.dumps(c_res if c_res is not None else '')

                        tool_name = tool_call_id_to_name.get(tool_use_id, 'tool')
                        parts.append({
                            'functionResponse': {
                                'id': tool_use_id,
                                'name': sanitize_tool_name(tool_name),
                                'response': {
                                    'output': content_str
                                }
                            }
                        })

        if parts:
            contents.append({'role': google_role, 'parts': parts})

    if not contents:
        contents.append({'role': 'user', 'parts': [{'text': 'Hello'}]})

    return contents, system_from_messages


def build_anthropic_router(
    pool: AccountPool,
    executor: RequestExecutor,
    verify_auth: Callable[[Request], Optional[Response]]
) -> APIRouter:
    router = APIRouter()

    async def handle_count_tokens(request: Request) -> Response:
        auth_err = verify_auth(request)
        if auth_err:
            return auth_err

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Invalid JSON body'}}
            )

        messages = body.get('messages')
        if not isinstance(messages, list):
            return JSONResponse(
                status_code=400,
                content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'messages is required and must be an array'}}
            )

        model_name = body.get('model') or 'claude-sonnet-4-6'
        upstream_model, model_pool = resolve_dynamic_upstream_model(model_name, 'high')
        is_gemini = upstream_model.startswith('gemini')
        contents, system_from_messages = convert_anthropic_messages(messages, is_gemini)

        system_texts = list(system_from_messages)
        body_system = body.get('system')
        if body_system:
            if isinstance(body_system, str):
                system_texts.append(body_system)
            elif isinstance(body_system, list):
                system_texts.append('\n'.join(p if isinstance(p, str) else p.get('text', '') for p in body_system))

        req_payload: Dict[str, Any] = {'contents': contents}
        if system_texts:
            req_payload['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system_texts)}]}

        body_tools = body.get('tools')
        if isinstance(body_tools, list) and body_tools:
            req_payload['tools'] = [{
                'functionDeclarations': [
                    {
                        'name': sanitize_tool_name(t.get('name', '')),
                        'description': t.get('description', ''),
                        'parameters': sanitize_google_schema(t.get('input_schema') or t.get('parameters') or {'type': 'object', 'properties': {}})
                    }
                    for t in body_tools if isinstance(t, dict)
                ]
            }]

        headers = request.headers
        header_session = headers.get('x-session-id') or headers.get('session-id') or headers.get('conversation-id') or body.get('metadata', {}).get('user_id')
        session_key = header_session
        if not session_key and messages:
            first_user = next((m for m in messages if m.get('role') == 'user'), None)
            user_key = first_user.get('content') if first_user else ''
            if not isinstance(user_key, str):
                user_key = json.dumps(user_key or '')
            session_key = hashlib.md5(f"anthropic_{user_key[:200]}".encode('utf-8')).hexdigest()

        try:
            counted = await executor.count_tokens(
                {'project': 'aicode-consumers', 'model': upstream_model, 'request': req_payload},
                3,
                session_key
            )
            return JSONResponse({'input_tokens': counted['totalTokens']})
        except Exception as err:
            status_code = getattr(err, 'status_code', 503)
            if not (400 <= status_code < 500):
                status_code = 503
            err_type = 'invalid_request_error' if status_code == 400 else 'api_error'
            return JSONResponse(
                status_code=status_code,
                content={'type': 'error', 'error': {'type': err_type, 'message': f"Unable to count request tokens: {err}"}}
            )

    async def handle_messages(request: Request) -> Response:
        auth_err = verify_auth(request)
        if auth_err:
            return auth_err

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Invalid JSON body'}}
            )

        messages = body.get('messages')
        if not isinstance(messages, list):
            return JSONResponse(
                status_code=400,
                content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'messages is required and must be an array'}}
            )

        # 1. 动态思考预算检测
        effort: ThinkingEffort = 'high'
        budget_tokens = 0
        thinking = body.get('thinking')
        if isinstance(thinking, dict):
            if thinking.get('type') == 'disabled':
                effort = 'none'
            elif isinstance(thinking.get('budget_tokens'), (int, float)):
                budget_tokens = int(thinking['budget_tokens'])
                if budget_tokens <= 1500:
                    effort = 'low'
                elif budget_tokens <= 5000:
                    effort = 'medium'
                else:
                    effort = 'high'

        model_name = body.get('model') or 'claude-sonnet-4-6'
        upstream_model, model_pool = resolve_dynamic_upstream_model(model_name, effort)
        is_gemini = upstream_model.startswith('gemini')
        contents, system_from_messages = convert_anthropic_messages(messages, is_gemini)

        default_max_output = 65536 if is_gemini else 8192
        max_tokens = body.get('max_tokens')
        generation_config: Dict[str, Any] = {
            'temperature': body.get('temperature', 0.7),
            'maxOutputTokens': min(max_tokens, default_max_output) if max_tokens else default_max_output
        }

        if budget_tokens > 0:
            generation_config['thinkingConfig'] = {'thinkingBudget': budget_tokens}

        output_format = body.get('output_config', {}).get('format')
        if output_format:
            if not isinstance(output_format, dict) or output_format.get('type') != 'json_schema' or not isinstance(output_format.get('schema'), dict):
                return JSONResponse(
                    status_code=400,
                    content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Unsupported output_config.format; expected json_schema with an object schema'}}
                )
            generation_config['responseMimeType'] = 'application/json'
            generation_config['responseSchema'] = sanitize_google_schema(output_format['schema'])

        inner_request: Dict[str, Any] = {
            'contents': contents,
            'generationConfig': generation_config
        }

        # 3. 转换 Tools & ToolConfig
        tool_name_mapping: Dict[str, str] = {}
        body_tools = body.get('tools')
        if isinstance(body_tools, list) and body_tools:
            declarations = []
            for t in body_tools:
                if not isinstance(t, dict):
                    continue
                clean_name = sanitize_tool_name(t.get('name', ''))
                tool_name_mapping[clean_name] = t.get('name', '')
                declarations.append({
                    'name': clean_name,
                    'description': t.get('description', ''),
                    'parameters': sanitize_google_schema(t.get('input_schema') or t.get('parameters') or {'type': 'object', 'properties': {}})
                })
            if declarations:
                inner_request['tools'] = [{'functionDeclarations': declarations}]

            tool_choice = body.get('tool_choice')
            if isinstance(tool_choice, dict):
                tc_type = tool_choice.get('type')
                if tc_type == 'any':
                    inner_request['toolConfig'] = {'functionCallingConfig': {'mode': 'ANY'}}
                elif tc_type == 'tool' and tool_choice.get('name'):
                    inner_request['toolConfig'] = {
                        'functionCallingConfig': {
                            'mode': 'ANY',
                            'allowedFunctionNames': [sanitize_tool_name(tool_choice['name'])]
                        }
                    }
                elif tc_type == 'none':
                    inner_request['toolConfig'] = {'functionCallingConfig': {'mode': 'NONE'}}
                else:
                    inner_request['toolConfig'] = {'functionCallingConfig': {'mode': 'AUTO'}}

        # 4. 系统提示词
        sys_parts: List[str] = list(system_from_messages)
        body_system = body.get('system')
        if body_system:
            if isinstance(body_system, str):
                sys_parts.append(body_system)
            elif isinstance(body_system, list):
                sys_parts.append('\n'.join(s if isinstance(s, str) else s.get('text', '') for s in body_system))
        if sys_parts:
            inner_request['systemInstruction'] = {'parts': [{'text': '\n\n'.join(sys_parts)}]}

        payload: Dict[str, Any] = {
            'project': 'aicode-consumers',
            'model': upstream_model,
            'request': inner_request
        }

        is_stream = bool(body.get('stream'))
        message_id = f"msg_{secrets.token_hex(12)}"

        headers = request.headers
        header_session = headers.get('x-session-id') or headers.get('session-id') or headers.get('conversation-id') or body.get('metadata', {}).get('user_id')
        session_key = header_session
        if not session_key and messages:
            sys_key = body.get('system')
            sys_str = sys_key if isinstance(sys_key, str) else json.dumps(sys_key or '')
            first_user = next((m for m in messages if m.get('role') == 'user'), None)
            user_key = first_user.get('content') if first_user else ''
            user_str = user_key if isinstance(user_key, str) else json.dumps(user_key or '')
            session_key = hashlib.md5(f"anthropic_{sys_str[:200]}_{user_str[:200]}".encode('utf-8')).hexdigest()

        input_token_count = 0
        try:
            counted = await executor.count_tokens(payload, 3, session_key)
            input_token_count = counted.get('totalTokens', 0)
        except Exception as count_err:
            print(f"[AnthropicGateway] Preflight countTokens unavailable; continuing: {count_err}")

        try:
            upstream_res = await executor.execute_with_retry(payload, 3, session_key)
        except ContextOverflowError:
            return JSONResponse(
                status_code=400,
                content={
                    'type': 'error',
                    'error': {
                        'type': 'invalid_request_error',
                        'message': 'Request context exceeds the upstream limit. Claude Code must compact the conversation and retry.'
                    }
                }
            )
        except UpstreamRequestError as u_err:
            return JSONResponse(
                status_code=u_err.status_code,
                content={
                    'type': 'error',
                    'error': {
                        'type': 'invalid_request_error' if u_err.status_code == 400 else 'api_error',
                        'message': u_err.upstream_body
                    }
                }
            )
        except Exception as ex:
            return JSONResponse(
                status_code=503,
                content={
                    'type': 'error',
                    'error': {
                        'type': 'api_error',
                        'message': f"Antigravity upstream unavailable: {ex}"
                    }
                }
            )

        resp = upstream_res['response']
        client = upstream_res['client']
        account_used = upstream_res['accountUsed']

        if is_stream:
            async def sse_generator():
                released = False

                async def cleanup(err_info=None):
                    nonlocal released
                    if not released:
                        released = True
                        pool.release_account(account_used['id'], err_info)
                        try:
                            await resp.aclose()
                        except Exception:
                            pass
                        try:
                            await client.aclose()
                        except Exception:
                            pass

                try:
                    start_msg = {
                        'type': 'message_start',
                        'message': {
                            'id': message_id,
                            'type': 'message',
                            'role': 'assistant',
                            'content': [],
                            'model': model_name,
                            'stop_reason': None,
                            'stop_sequence': None,
                            'usage': {'input_tokens': input_token_count, 'output_tokens': 0}
                        }
                    }
                    yield f"event: message_start\ndata: {json.dumps(start_msg, ensure_ascii=False)}\n\n".encode('utf-8')

                    buffer = ""
                    current_block_index = 0
                    is_text_block_open = False
                    text_block_started = False
                    has_tool_use = False
                    last_usage_metadata = None
                    seen_tool_calls = set()

                    async for chunk in resp.aiter_bytes():
                        buffer += chunk.decode('utf-8', errors='replace')
                        lines = buffer.split('\n')
                        buffer = lines.pop() if lines else ""

                        for line in lines:
                            trimmed = line.strip()
                            if not trimmed.startswith('data:'):
                                continue
                            json_str = trimmed[5:].strip()
                            if not json_str or json_str == '[DONE]':
                                continue

                            parsed = parse_google_stream_chunk(json_str)
                            text = parsed.get('text', '')
                            function_calls = parsed.get('functionCalls', [])
                            usage_meta = parsed.get('usageMetadata')
                            if usage_meta:
                                last_usage_metadata = usage_meta

                            if text:
                                if not is_text_block_open:
                                    start_block = {
                                        'type': 'content_block_start',
                                        'index': current_block_index,
                                        'content_block': {'type': 'text', 'text': ''}
                                    }
                                    yield f"event: content_block_start\ndata: {json.dumps(start_block, ensure_ascii=False)}\n\n".encode('utf-8')
                                    is_text_block_open = True
                                    text_block_started = True

                                delta_block = {
                                    'type': 'content_block_delta',
                                    'index': current_block_index,
                                    'delta': {'type': 'text_delta', 'text': text}
                                }
                                yield f"event: content_block_delta\ndata: {json.dumps(delta_block, ensure_ascii=False)}\n\n".encode('utf-8')

                            if function_calls:
                                if is_text_block_open:
                                    stop_block = {
                                        'type': 'content_block_stop',
                                        'index': current_block_index
                                    }
                                    yield f"event: content_block_stop\ndata: {json.dumps(stop_block, ensure_ascii=False)}\n\n".encode('utf-8')
                                    is_text_block_open = False
                                    current_block_index += 1

                                for fn in function_calls:
                                    call_key = f"{fn.get('id')}_{fn.get('name')}_{json.dumps(fn.get('args', {}), sort_keys=True)}"
                                    if call_key in seen_tool_calls:
                                        continue
                                    seen_tool_calls.add(call_key)

                                    has_tool_use = True
                                    tool_block_index = current_block_index
                                    current_block_index += 1
                                    tool_id = fn.get('id') or f"toolu_{secrets.token_hex(12)}"
                                    tool_name = tool_name_mapping.get(fn.get('name', ''), fn.get('name', ''))

                                    tool_start = {
                                        'type': 'content_block_start',
                                        'index': tool_block_index,
                                        'content_block': {
                                            'type': 'tool_use',
                                            'id': tool_id,
                                            'name': tool_name,
                                            'input': {}
                                        }
                                    }
                                    yield f"event: content_block_start\ndata: {json.dumps(tool_start, ensure_ascii=False)}\n\n".encode('utf-8')

                                    tool_delta = {
                                        'type': 'content_block_delta',
                                        'index': tool_block_index,
                                        'delta': {
                                            'type': 'input_json_delta',
                                            'partial_json': json.dumps(fn.get('args') or {}, ensure_ascii=False)
                                        }
                                    }
                                    yield f"event: content_block_delta\ndata: {json.dumps(tool_delta, ensure_ascii=False)}\n\n".encode('utf-8')

                                    tool_stop = {
                                        'type': 'content_block_stop',
                                        'index': tool_block_index
                                    }
                                    yield f"event: content_block_stop\ndata: {json.dumps(tool_stop, ensure_ascii=False)}\n\n".encode('utf-8')

                    if is_text_block_open:
                        stop_block = {'type': 'content_block_stop', 'index': current_block_index}
                        yield f"event: content_block_stop\ndata: {json.dumps(stop_block, ensure_ascii=False)}\n\n".encode('utf-8')
                        is_text_block_open = False

                    if not text_block_started and not has_tool_use:
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n".encode('utf-8')
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0}, ensure_ascii=False)}\n\n".encode('utf-8')

                    out_tokens = 0
                    if last_usage_metadata and last_usage_metadata.get('candidatesTokenCount'):
                        out_tokens = last_usage_metadata['candidatesTokenCount']

                    msg_delta = {
                        'type': 'message_delta',
                        'delta': {
                            'stop_reason': 'tool_use' if has_tool_use else 'end_turn',
                            'stop_sequence': None
                        },
                        'usage': {'output_tokens': out_tokens}
                    }
                    yield f"event: message_delta\ndata: {json.dumps(msg_delta, ensure_ascii=False)}\n\n".encode('utf-8')
                    yield b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"

                    if last_usage_metadata:
                        pool.record_token_usage(last_usage_metadata)

                    await cleanup()
                except Exception as stream_err:
                    print(f"[AnthropicGateway] Stream error: {stream_err}")
                    await cleanup({'code': 500, 'message': str(stream_err)})

            return StreamingResponse(
                sse_generator(),
                media_type="text/event-stream; charset=utf-8",
                headers={
                    'Cache-Control': 'no-cache, no-transform',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no'
                }
            )

        # 非流式处理
        full_text = ""
        content_parts: List[Dict[str, Any]] = []
        seen_tool_calls: Set[str] = set()
        has_tool_use = False
        buffer = ""
        last_usage_metadata = None

        try:
            async for chunk in resp.aiter_bytes():
                buffer += chunk.decode('utf-8', errors='replace')
                lines = buffer.split('\n')
                buffer = lines.pop() if lines else ""

                for line in lines:
                    trimmed = line.strip()
                    if not trimmed.startswith('data:'):
                        continue
                    json_str = trimmed[5:].strip()
                    if not json_str or json_str == '[DONE]':
                        continue

                    parsed = parse_google_stream_chunk(json_str)
                    text = parsed.get('text', '')
                    function_calls = parsed.get('functionCalls', [])
                    usage_meta = parsed.get('usageMetadata')
                    if usage_meta:
                        last_usage_metadata = usage_meta
                    if text:
                        full_text += text

                    for fn in function_calls:
                        call_key = f"{fn.get('id')}_{fn.get('name')}_{json.dumps(fn.get('args', {}), sort_keys=True)}"
                        if call_key in seen_tool_calls:
                            continue
                        seen_tool_calls.add(call_key)

                        has_tool_use = True
                        tool_id = fn.get('id') or f"toolu_{secrets.token_hex(12)}"
                        tool_name = tool_name_mapping.get(fn.get('name', ''), fn.get('name', ''))
                        content_parts.append({
                            'type': 'tool_use',
                            'id': tool_id,
                            'name': tool_name,
                            'input': fn.get('args') or {}
                        })

            self_cleanup = True
            pool.release_account(account_used['id'])
            await resp.aclose()
            await client.aclose()

            if last_usage_metadata:
                pool.record_token_usage(last_usage_metadata)

            if full_text:
                content_parts.insert(0, {'type': 'text', 'text': full_text})

            if not content_parts:
                content_parts.append({'type': 'text', 'text': ''})

            in_tokens = last_usage_metadata.get('promptTokenCount', 0) if last_usage_metadata else 0
            out_tokens = last_usage_metadata.get('candidatesTokenCount', 0) if last_usage_metadata else 0

            return JSONResponse({
                'id': message_id,
                'type': 'message',
                'role': 'assistant',
                'model': model_name,
                'content': content_parts,
                'stop_reason': 'tool_use' if has_tool_use else 'end_turn',
                'stop_sequence': None,
                'usage': {
                    'input_tokens': in_tokens,
                    'output_tokens': out_tokens
                }
            })
        except Exception as err:
            pool.release_account(account_used['id'], {'code': 500, 'message': str(err)})
            try:
                await resp.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass
            return JSONResponse(
                status_code=500,
                content={'type': 'error', 'error': {'type': 'api_error', 'message': f"Stream parsing error: {err}"}}
            )

    # 注册路由
    router.add_api_route('/v1/messages', handle_messages, methods=['POST'])
    router.add_api_route('/messages', handle_messages, methods=['POST'])
    router.add_api_route('/v1/v1/messages', handle_messages, methods=['POST'])

    router.add_api_route('/v1/messages/count_tokens', handle_count_tokens, methods=['POST'])
    router.add_api_route('/messages/count_tokens', handle_count_tokens, methods=['POST'])
    router.add_api_route('/v1/v1/messages/count_tokens', handle_count_tokens, methods=['POST'])

    return router
