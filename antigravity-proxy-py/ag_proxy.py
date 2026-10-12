import os
import sys
import json
import time
import uuid
import hashlib
import asyncio
from typing import Dict, Any, Optional, List, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from config import load_config
from pool.account_pool import AccountPool
from pool.refresher import TokenRefresher
from pool.quota_manager import QuotaManager
from pool.breaker import RequestExecutor, UpstreamRequestError, ContextOverflowError
from gateway.models import SUPPORTED_MODELS
from gateway.adapter import convert_openai_to_cloud_code, create_openai_chunk, extract_text_from_google_chunk
from gateway.anthropic import build_anthropic_router
from admin.routes import build_admin_router

# 全局上下文对象
config = load_config()
pool = AccountPool(initial_strategy=config.get('routingStrategy', 'zero-waste'))
refresher = TokenRefresher(pool, config)
quota_manager = QuotaManager(pool, config)
executor = RequestExecutor(pool, refresher, config, quota_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动后台服务
    print('[Startup] Starting TokenRefresher & QuotaManager...')
    refresher.start()
    quota_manager.start_background_worker()
    yield
    # 优雅退出
    print('[Shutdown] Stopping TokenRefresher & QuotaManager...')
    refresher.stop()
    quota_manager.stop()


app = FastAPI(
    title="Antigravity Multi-Account Proxy",
    version="2.14.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan
)

# 跨域支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 请求标识与路径规范化中间件
@app.middleware("http")
async def dispatch_middleware(request: Request, call_next):
    req_id = request.headers.get('x-request-id') or str(uuid.uuid4())
    request.state.req_id = req_id

    # 规范化连续的 /v1/v1 前缀
    scope = request.scope
    path = scope.get('path', '')
    if path.startswith('/v1/v1/'):
        scope['path'] = '/v1/' + path[7:]

    response = await call_next(request)
    response.headers['x-request-id'] = req_id
    return response


# 客户端鉴权验证
def verify_client_auth(request: Request) -> Optional[Response]:
    master_key = config.get('apiMasterKey')
    if not master_key:
        return None

    auth_header = request.headers.get('authorization') or request.headers.get('x-api-key')
    if not auth_header:
        return JSONResponse(
            status_code=401,
            content={'error': {'message': 'Missing Authorization or x-api-key header', 'type': 'invalid_request_error'}}
        )

    token = auth_header[7:].strip() if auth_header.startswith('Bearer ') else auth_header.strip()
    if token != master_key:
        return JSONResponse(
            status_code=401,
            content={'error': {'message': 'Incorrect API key provided', 'type': 'invalid_request_error'}}
        )
    return None


# 静态管理后台
public_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public')
if os.path.exists(public_dir):
    app.mount('/admin', StaticFiles(directory=public_dir, html=True), name='admin')


@app.get('/')
async def root_redirect():
    return RedirectResponse(url='/admin/')

# 健康探针
@app.get('/health')
async def health_check():
    return {
        'status': 'ok',
        'service': 'antigravity-proxy',
        'stats': pool.get_stats()
    }


# ===================== CC Switch / OneAPI / NewAPI 用量查询路由 =====================
async def handle_user_balance(request: Request):
    auth_header = request.headers.get('authorization') or request.headers.get('x-api-key')
    token = ''
    if auth_header:
        token = auth_header[7:].strip() if auth_header.startswith('Bearer ') else auth_header.strip()

    client_host = request.client.host if request.client else '127.0.0.1'
    is_localhost = client_host in ['127.0.0.1', '::1', 'localhost', 'testclient']

    master_key = config.get('apiMasterKey')
    if master_key and token != master_key and not is_localhost:
        return JSONResponse(
            status_code=401,
            content={'error': {'message': 'Incorrect API key provided', 'type': 'invalid_request_error'}}
        )

    accounts = pool.get_all_accounts()
    active_accounts = [a for a in accounts if a.get('status') == 'active']

    total_claude_5h = 0.0
    claude_account_count = 0
    nearest_reset_time = None
    nearest_reset_diff_ms = float('inf')
    now_ms = time.time() * 1000

    for acc in active_accounts:
        quota = acc.get('quota')
        if quota and isinstance(quota.get('claude5hFraction'), (int, float)):
            total_claude_5h += quota['claude5hFraction']
            claude_account_count += 1
            if quota.get('claudeResetTime'):
                try:
                    from datetime import datetime
                    rst = quota['claudeResetTime'].replace('Z', '+00:00')
                    dt = datetime.fromisoformat(rst)
                    reset_diff = dt.timestamp() * 1000 - now_ms
                    if 0 < reset_diff < nearest_reset_diff_ms:
                        nearest_reset_diff_ms = reset_diff
                        nearest_reset_time = quota['claudeResetTime']
                except Exception:
                    pass
        else:
            total_claude_5h += 1.0
            claude_account_count += 1

    avg_claude_percent = round((total_claude_5h / claude_account_count) * 100 * 10) / 10 if claude_account_count > 0 else 100.0

    total_points = len(active_accounts) * 100
    remaining_points = round(total_claude_5h * 100 * 10) / 10
    used_points = max(0.0, round((total_points - remaining_points) * 10) / 10)

    return {
        'success': True,
        'isValid': True,
        'remaining': avg_claude_percent,
        'used': round((100 - avg_claude_percent) * 10) / 10,
        'total': 100,
        'unit': '%',
        'planName': f"Antigravity ({len(active_accounts)}/{len(accounts)}号在线)",
        'data': {
            'quota': remaining_points * 5000,
            'total_quota': total_points * 5000,
            'used_quota': used_points * 5000
        },
        'total_usage': used_points,
        'total_tokens': total_points,
        'active_accounts': len(active_accounts),
        'total_accounts': len(accounts),
        'claude_5h_percent': avg_claude_percent,
        'nearest_reset_time': nearest_reset_time,
        'routing_strategy': pool.get_routing_strategy()
    }

app.add_api_route('/v1/user/balance', handle_user_balance, methods=['GET'])
app.add_api_route('/user/balance', handle_user_balance, methods=['GET'])
app.add_api_route('/api/user/balance', handle_user_balance, methods=['GET'])
app.add_api_route('/v1/dashboard/billing/usage', handle_user_balance, methods=['GET'])
app.add_api_route('/dashboard/billing/usage', handle_user_balance, methods=['GET'])
app.add_api_route('/v1/usage', handle_user_balance, methods=['GET'])


# ===================== 模型列表路由 =====================
async def handle_get_models(request: Request):
    auth_err = verify_client_auth(request)
    if auth_err:
        return auth_err

    model_items = []
    for m in SUPPORTED_MODELS:
        mid = m['id']
        owned_by = 'google'
        if 'claude' in mid or 'opus' in mid or 'sonnet' in mid or 'haiku' in mid:
            owned_by = 'anthropic'
        elif 'gpt' in mid or 'o1' in mid or 'o3' in mid or 'chatgpt' in mid:
            owned_by = 'openai'

        model_items.append({
            'id': mid,
            'object': 'model',
            'owned_by': owned_by,
            'permission': [],
            'root': mid,
            'parent': None,
            'context_length': m.get('contextLength', 1048576),
            'max_tokens': m.get('contextLength', 1048576),
            'max_output_tokens': m.get('maxOutputTokens', 65536),
            'description': m.get('description', '')
        })

    return {'object': 'list', 'data': model_items}

app.add_api_route('/v1/models', handle_get_models, methods=['GET'])
app.add_api_route('/models', handle_get_models, methods=['GET'])


# ===================== POST /v1/chat/completions =====================
async def handle_chat_completions(request: Request):
    auth_err = verify_client_auth(request)
    if auth_err:
        return auth_err

    try:
        chat_req = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON body', 'type': 'invalid_request_error'}})

    if not chat_req or not isinstance(chat_req.get('messages'), list):
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid request: messages array is required', 'type': 'invalid_request_error'}})

    payload, upstream_model, *rest = convert_openai_to_cloud_code(chat_req)
    is_stream = bool(chat_req.get('stream'))
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    # 会话指纹
    header_session = request.headers.get('x-session-id') or request.headers.get('session-id') or request.headers.get('conversation-id') or chat_req.get('user')
    session_key = header_session
    if not session_key and chat_req.get('messages'):
        msgs = chat_req['messages']
        first_sys = next((m for m in msgs if m.get('role') == 'system'), None)
        first_user = next((m for m in msgs if m.get('role') == 'user'), None)
        sys_key = first_sys.get('content') if first_sys else ''
        user_key = first_user.get('content') if first_user else ''
        session_key = hashlib.md5(f"openai_{str(sys_key)[:200]}_{str(user_key)[:200]}".encode('utf-8')).hexdigest()

    try:
        upstream_res = await executor.execute_with_retry(payload, max_retries=3, session_key=session_key)
    except ContextOverflowError:
        return JSONResponse(
            status_code=400,
            content={'error': {'message': 'Request context exceeds the upstream limit. The client must compact its conversation and retry.', 'type': 'invalid_request_error'}}
        )
    except UpstreamRequestError as err:
        return JSONResponse(
            status_code=err.status_code,
            content={'error': {'message': str(err), 'type': 'invalid_request_error' if err.status_code == 400 else 'api_error'}}
        )
    except Exception as err:
        return JSONResponse(
            status_code=503,
            content={'error': {'message': f"Antigravity upstream unavailable: {err}", 'type': 'api_error'}}
        )

    response_obj = upstream_res['response']
    client_obj = upstream_res['client']
    account_used = upstream_res['accountUsed']

    if is_stream:
        async def event_generator() -> AsyncGenerator[str, None]:
            buffer = ""
            usage_metadata = None
            tool_calls: List[Dict[str, Any]] = []

            try:
                async for chunk in response_obj.aiter_bytes():
                    buffer += chunk.decode('utf-8', errors='replace')
                    lines = buffer.split('\n')
                    buffer = lines.pop()

                    for line in lines:
                        trimmed = line.strip()
                        if not trimmed.startswith('data:'):
                            continue
                        json_str = trimmed[5:].strip()
                        if not json_str or json_str == '[DONE]':
                            continue

                        extracted = extract_text_from_google_chunk(json_str)
                        text = extracted.get('text', '')
                        function_calls = extracted.get('functionCalls', [])
                        finish_reason = extracted.get('finishReason')
                        chunk_usage = extracted.get('usageMetadata')

                        if chunk_usage:
                            usage_metadata = chunk_usage

                        if text:
                            yield create_openai_chunk(completion_id, chat_req.get('model', 'gemini-3.8-flash'), text, None)

                        for call in function_calls:
                            tc_index = len(tool_calls)
                            tc_obj = {
                                'id': call.get('id') or f"call_{completion_id}_{tc_index}",
                                'name': call.get('name', ''),
                                'arguments': json.dumps(call.get('args', {}), ensure_ascii=False)
                            }
                            tool_calls.append(tc_obj)
                            yield create_openai_chunk(
                                completion_id,
                                chat_req.get('model', 'gemini-3.8-flash'),
                                '',
                                None,
                                [{'index': tc_index, 'id': tc_obj['id'], 'name': tc_obj['name'], 'arguments': tc_obj['arguments']}]
                            )

                        if finish_reason:
                            final_reason = 'tool_calls' if tool_calls else finish_reason
                            yield create_openai_chunk(completion_id, chat_req.get('model', 'gemini-3.8-flash'), '', final_reason)

                yield 'data: [DONE]\n\n'
                if usage_metadata:
                    pool.record_token_usage(usage_metadata)
            except Exception as e:
                print(f"[Gateway] SSE Streaming error: {e}")
            finally:
                await response_obj.aclose()
                await client_obj.aclose()
                pool.release_account(account_used['id'])

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                'Cache-Control': 'no-cache, no-transform',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'
            }
        )
    else:
        full_text = ""
        buffer = ""
        usage_metadata = None
        tool_calls: List[Dict[str, Any]] = []

        try:
            async for chunk in response_obj.aiter_bytes():
                buffer += chunk.decode('utf-8', errors='replace')
                lines = buffer.split('\n')
                buffer = lines.pop()

                for line in lines:
                    trimmed = line.strip()
                    if not trimmed.startswith('data:'):
                        continue
                    json_str = trimmed[5:].strip()
                    if not json_str or json_str == '[DONE]':
                        continue

                    extracted = extract_text_from_google_chunk(json_str)
                    text = extracted.get('text', '')
                    function_calls = extracted.get('functionCalls', [])
                    chunk_usage = extracted.get('usageMetadata')

                    if chunk_usage:
                        usage_metadata = chunk_usage
                    if text:
                        full_text += text
                    for call in function_calls:
                        tool_calls.append({
                            'id': call.get('id') or f"call_{completion_id}_{len(tool_calls)}",
                            'name': call.get('name', ''),
                            'arguments': json.dumps(call.get('args', {}), ensure_ascii=False)
                        })

            if usage_metadata:
                pool.record_token_usage(usage_metadata)

            msg_payload: Dict[str, Any] = {
                'role': 'assistant',
                'content': full_text if full_text else None
            }
            if tool_calls:
                msg_payload['tool_calls'] = [
                    {
                        'id': tc['id'],
                        'type': 'function',
                        'function': {'name': tc['name'], 'arguments': tc['arguments']}
                    }
                    for tc in tool_calls
                ]

            return {
                'id': completion_id,
                'object': 'chat.completion',
                'created': int(time.time()),
                'model': chat_req.get('model', 'gemini-3.8-flash'),
                'choices': [
                    {
                        'index': 0,
                        'message': msg_payload,
                        'finish_reason': 'tool_calls' if tool_calls else 'stop'
                    }
                ],
                'usage': {
                    'prompt_tokens': usage_metadata.get('promptTokenCount', 0) if usage_metadata else 0,
                    'completion_tokens': usage_metadata.get('candidatesTokenCount', 0) if usage_metadata else 0,
                    'total_tokens': usage_metadata.get('totalTokenCount', 0) if usage_metadata else 0
                }
            }
        except Exception as err:
            pool.release_account(account_used['id'], {'code': 500, 'message': str(err)})
            return JSONResponse(status_code=500, content={'error': {'message': f"Stream parsing error: {err}", 'type': 'api_error'}})
        finally:
            await response_obj.aclose()
            await client_obj.aclose()
            pool.release_account(account_used['id'])

app.add_api_route('/v1/chat/completions', handle_chat_completions, methods=['POST'])
app.add_api_route('/chat/completions', handle_chat_completions, methods=['POST'])
app.add_api_route('/v1/v1/chat/completions', handle_chat_completions, methods=['POST'])


# ===================== POST /v1/responses (OpenAI Responses API) =====================
async def handle_responses(request: Request):
    auth_err = verify_client_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON body', 'type': 'invalid_request_error'}})

    if not body or not body.get('model'):
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid request: model is required', 'type': 'invalid_request_error'}})

    messages: List[Dict[str, Any]] = []
    if isinstance(body.get('instructions'), str) and body['instructions'].strip():
        messages.append({'role': 'system', 'content': body['instructions']})

    raw_input = body.get('input')
    items = raw_input if isinstance(raw_input, list) else [{'role': 'user', 'content': raw_input or ''}]
    for item in items:
        if isinstance(item, str):
            messages.append({'role': 'user', 'content': item})
        elif isinstance(item, dict):
            item_type = item.get('type')
            if item_type == 'message' or 'role' in item:
                role = 'system' if item.get('role') == 'developer' else item.get('role', 'user')
                content = item.get('content', '')
                if isinstance(content, list):
                    content = '\n'.join([p.get('text', '') or p.get('input_text', '') or p.get('output_text', '') for p in content if isinstance(p, dict)])
                messages.append({'role': role, 'content': content})
            elif item_type == 'function_call_output':
                messages.append({
                    'role': 'tool',
                    'tool_call_id': item.get('call_id'),
                    'content': item.get('output') if isinstance(item.get('output'), str) else json.dumps(item.get('output') or '')
                })
            elif item_type == 'function_call':
                messages.append({
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [{
                        'id': item.get('call_id'),
                        'type': 'function',
                        'function': {'name': item.get('name'), 'arguments': item.get('arguments', '{}')}
                    }]
                })

    if not messages:
        messages.append({'role': 'user', 'content': ''})

    chat_req = {
        'model': body['model'],
        'messages': messages,
        'stream': bool(body.get('stream')),
        'temperature': body.get('temperature'),
        'max_tokens': body.get('max_output_tokens')
    }
    if isinstance(body.get('tools'), list):
        chat_req['tools'] = [
            {'type': 'function', 'function': {'name': t['name'], 'description': t.get('description', ''), 'parameters': t.get('parameters')}}
            for t in body['tools'] if isinstance(t, dict) and t.get('type') == 'function' and t.get('name')
        ]
    if body.get('tool_choice'):
        chat_req['tool_choice'] = body['tool_choice']

    payload, upstream_model, *rest = convert_openai_to_cloud_code(chat_req)
    response_id = f"resp_{uuid.uuid4().hex[:24]}"

    header_session = request.headers.get('x-session-id') or request.headers.get('session-id') or body.get('user')
    session_key = header_session or hashlib.md5(f"responses_{json.dumps(messages[:2])}".encode('utf-8')).hexdigest()

    try:
        upstream_res = await executor.execute_with_retry(payload, max_retries=3, session_key=session_key)
    except ContextOverflowError:
        return JSONResponse(status_code=400, content={'error': {'message': 'Request context exceeds the upstream limit.', 'type': 'invalid_request_error'}})
    except UpstreamRequestError as err:
        return JSONResponse(status_code=err.status_code, content={'error': {'message': str(err), 'type': 'invalid_request_error' if err.status_code == 400 else 'api_error'}})
    except Exception as err:
        return JSONResponse(status_code=503, content={'error': {'message': str(err), 'type': 'api_error'}})

    response_obj = upstream_res['response']
    client_obj = upstream_res['client']
    account_used = upstream_res['accountUsed']

    full_text = ""
    buffer = ""
    usage_metadata = None
    tool_calls: List[Dict[str, Any]] = []

    def make_response_payload(status: str, output: list) -> Dict[str, Any]:
        return {
            'id': response_id,
            'object': 'response',
            'created_at': int(time.time()),
            'status': status,
            'incomplete_details': None,
            'model': body['model'],
            'output': output,
            'instructions': body.get('instructions'),
            'tools': body.get('tools', []),
            'tool_choice': body.get('tool_choice', 'auto'),
            'temperature': body.get('temperature', 1.0),
            'top_p': body.get('top_p', 1.0),
            'max_output_tokens': body.get('max_output_tokens'),
            'previous_response_id': body.get('previous_response_id'),
            'reasoning': body.get('reasoning', {'effort': None, 'summary': None}),
            'text': body.get('text', {'format': {'type': 'text'}}),
            'parallel_tool_calls': body.get('parallel_tool_calls', True),
            'metadata': body.get('metadata', {}),
            'usage': {
                'input_tokens': usage_metadata.get('promptTokenCount', 0) if usage_metadata else 0,
                'output_tokens': usage_metadata.get('candidatesTokenCount', 0) if usage_metadata else 0,
                'total_tokens': usage_metadata.get('totalTokenCount', 0) if usage_metadata else 0
            } if usage_metadata else None
        }

    if body.get('stream'):
        async def event_generator() -> AsyncGenerator[str, None]:
            nonlocal full_text, buffer, usage_metadata
            try:
                msg_id = f"msg_{response_id}"
                yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': make_response_payload('in_progress', [])})}\n\n"
                yield f"event: response.in_progress\ndata: {json.dumps({'type': 'response.in_progress', 'response': make_response_payload('in_progress', [])})}\n\n"
                yield f"event: response.output_item.added\ndata: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []}})}\n\n"
                yield f"event: response.content_part.added\ndata: {json.dumps({'type': 'response.content_part.added', 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"

                async for chunk in response_obj.aiter_bytes():
                    buffer += chunk.decode('utf-8', errors='replace')
                    lines = buffer.split('\n')
                    buffer = lines.pop()

                    for line in lines:
                        trimmed = line.strip()
                        if not trimmed.startswith('data:'):
                            continue
                        json_str = trimmed[5:].strip()
                        if not json_str or json_str == '[DONE]':
                            continue

                        extracted = extract_text_from_google_chunk(json_str)
                        if extracted.get('usageMetadata'):
                            usage_metadata = extracted['usageMetadata']
                        txt = extracted.get('text', '')
                        if txt:
                            full_text += txt
                            yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'response_id': response_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': txt})}\n\n"

                output_items = [{
                    'type': 'message',
                    'id': msg_id,
                    'status': 'completed',
                    'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]
                }]
                yield f"event: response.output_text.done\ndata: {json.dumps({'type': 'response.output_text.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': full_text})}\n\n"
                yield f"event: response.content_part.done\ndata: {json.dumps({'type': 'response.content_part.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': full_text, 'annotations': []}})}\n\n"
                yield f"event: response.output_item.done\ndata: {json.dumps({'type': 'response.output_item.done', 'output_index': 0, 'item': output_items[0]})}\n\n"
                yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': make_response_payload('completed', output_items)})}\n\n"
                yield 'data: [DONE]\n\n'
                if usage_metadata:
                    pool.record_token_usage(usage_metadata)
            finally:
                await response_obj.aclose()
                await client_obj.aclose()
                pool.release_account(account_used['id'])

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={'Cache-Control': 'no-cache, no-transform', 'Connection': 'keep-alive'}
        )
    else:
        try:
            async for chunk in response_obj.aiter_bytes():
                buffer += chunk.decode('utf-8', errors='replace')
                lines = buffer.split('\n')
                buffer = lines.pop()

                for line in lines:
                    trimmed = line.strip()
                    if not trimmed.startswith('data:'):
                        continue
                    json_str = trimmed[5:].strip()
                    if not json_str or json_str == '[DONE]':
                        continue

                    extracted = extract_text_from_google_chunk(json_str)
                    if extracted.get('usageMetadata'):
                        usage_metadata = extracted['usageMetadata']
                    txt = extracted.get('text', '')
                    if txt:
                        full_text += txt

            output_items = [{
                'type': 'message',
                'id': f"msg_{response_id}",
                'status': 'completed',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]
            }]
            if usage_metadata:
                pool.record_token_usage(usage_metadata)

            res = make_response_payload('completed', output_items)
            res['output_text'] = full_text
            return res
        finally:
            await response_obj.aclose()
            await client_obj.aclose()
            pool.release_account(account_used['id'])

app.add_api_route('/v1/responses', handle_responses, methods=['POST'])
app.add_api_route('/responses', handle_responses, methods=['POST'])
app.add_api_route('/v1/v1/responses', handle_responses, methods=['POST'])

# 注册 Anthropic 兼容路由
app.include_router(build_anthropic_router(pool, executor, verify_client_auth))

# 注册 Admin 路由
app.include_router(build_admin_router(pool, refresher, config, quota_manager))


if __name__ == '__main__':
    port = int(config.get('port', 43811))
    host = config.get('host', '0.0.0.0')
    print("====================================================")
    print("       🚀 Antigravity Multi-Account Proxy (Python)   ")
    print("====================================================")
    print(f"\n✅ Proxy Service is starting on http://{host}:{port}")
    print(f"📌 [Web Management Console]: http://localhost:{port}/admin")
    print(f"📌 [OpenAI Endpoints]: http://localhost:{port}/v1/chat/completions")
    print(f"📌 [Anthropic Endpoints]: http://localhost:{port}/v1/messages")
    print("====================================================\n")
    uvicorn.run(app, host=host, port=port, access_log=True)
