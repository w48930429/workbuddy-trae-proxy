"""CodeArts (Snap) 上游客户端 —— 纯 Python 复刻。

协议事实来源：2951461586/CodeArts2API 的 ``docs/UPSTREAM.md``（活文档）
与 ``internal/codearts/{signer,client}.go``。不 vendor、不调用 Go 二进制。

上游怪癖（每条都有对应实现，改动前先读 ``docs/UPSTREAM.md``）：

  1. **业务错误常以 HTTP 200 + ``error_code`` 表达** —— HTTP 状态码本身不可信。
     真实状态码编码在 ``error_code`` 里，三种形态：
       ``"4005"``              纯业务码 → 不在 400–599 → 兜底 400
       ``"400"``               纯状态码 → 原样
       ``InferHub.002002009.404``  复合码 → 取末段
     取不到一律兜底 400，绝不返回 200。
  2. **``model`` 必须与 ``model-id`` / ``model-name`` 两个头完全一致**，
     否则 ``InferHub.001001011.406``。改名要三处一起改。
  3. **``glm-5.3`` 只支持流式**：``stream=false`` 回 400 空 body。
  4. **每账号并发会话上限 3**，按 ``x-ot-session-id`` 统计，超出回
     ``TM.00001041``。该 id 必须按凭据派生并复用，不能每请求随机。
  5. 2026-10 起 benefit 通道下线，**不再发送 ``maas_type``**，只剩积分扣费。

自测：``python codearts/codearts_client.py``
"""

from __future__ import annotations

import datetime
import hashlib
import json
import urllib.error
import urllib.request
import uuid

import codearts_signer as signer

CHAT_HOST = "https://snap-access.cn-north-4.myhuaweicloud.com"
CHAT_PATH = "/api/v2/chat/completions"
CREDITS_PATH = "/snap-manager/v1/statistics/plugin"

# 客户端版本：上游按这些头做路由/埋点/限流，缺省值对齐 CodeArts Agent 26.9.902。
PLUGIN_NAME = "codemate_vscode"
PLUGIN_VERSION = "26.9.902"
IDE_NAME = "VSCode"
IDE_VERSION = "26.9.902"
APP_ID = "CodeAgent3.0"

# 只支持流式的模型（上游怪癖 3）。命中时强制 stream=true 再本地聚合。
STREAM_ONLY_MODELS = frozenset(("glm-5.3",))

# 凭据类错误码：即使以 HTTP 400 返回也必须算账号故障，不能当参数错误透传。
CREDENTIAL_CODES = ("APIG.0301", "APIG.0303", "APIG.0602",
                    "STS5.1804", "STS5.1806", "invalid_grant")
# 并发会话超限：临时故障，退避重试，不计账号失败。
CONCURRENCY_CODE = "TM.00001041"


class CodeArtsError(Exception):
    """上游调用失败。``status`` 是**对外**应该用的 HTTP 状态码。"""

    def __init__(self, status, message, code="", raw=""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.raw = raw

    def __str__(self):
        return "[%s] %s" % (self.code or self.status, self.message)


class CodeArtsCredentialError(CodeArtsError):
    """登录态失效 —— 唯一需要人工介入的错误（重新登录 IDE）。"""


def upstream_session_id(domain_id, access_key):
    """按凭据派生固定会话 id。

    上游按它统计并发（上限 3），随机会让每个请求都算一个新会话，
    很快撞上 ``TM.00001041``。
    """
    digest = hashlib.sha256(("%s|%s" % (domain_id or "", access_key or "")).encode())
    return "ses_" + digest.hexdigest()[:32]


def status_from_code(code):
    """从 ``error_code`` 提取真实 HTTP 状态码（怪癖 1）。"""
    text = str(code or "").strip()
    if not text:
        return None
    # 复合码 InferHub.002002009.404 → 取末段
    tail = text.rsplit(".", 1)[-1].strip()
    candidate = tail if tail.isdigit() else (text if text.isdigit() else "")
    if candidate:
        value = int(candidate)
        if 400 <= value <= 599:
            return value
    return None


def is_credential_failure(code, message=""):
    blob = ("%s %s" % (code or "", message or "")).lower()
    return any(marker.lower() in blob for marker in CREDENTIAL_CODES)


def chat_headers(model, session):
    """构造一次对话请求的身份头。``model`` 会同时写进三个位置（怪癖 2）。"""
    stamp = datetime.datetime.now(datetime.timezone.utc)
    return {
        "Content-Type": "application/json",
        "app-id": APP_ID,
        "X-Language": "zh-cn",
        "model-id": model,
        "model-name": model,
        "x-ot-function": "agent-general",
        "x-ot-trace-id": uuid.uuid4().hex,
        "x-snap-traceid": uuid.uuid4().hex,
        "x-ot-span-id": uuid.uuid4().hex[:16],
        "user-msg-id": "msg_" + uuid.uuid4().hex[:20],
        "created-time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-ot-client-type": "IDE",
        "x-ot-client-version": PLUGIN_VERSION,
        "x-ot-session-id": upstream_session_id(session.domain_id, session.access_key),
        "plugin-name": PLUGIN_NAME,
        "plugin-version": PLUGIN_VERSION,
        "ide-name": IDE_NAME,
        "ide-version": IDE_VERSION,
    }


def build_signed_request(url, payload, session, method="POST", headers=None):
    body = b"" if payload is None else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    signed = signer.sign_request(
        session.access_key, session.secret_key, session.security_token,
        method, url, body, headers or {})
    request = urllib.request.Request(url, data=body or None,
                                     method=method, headers=signed)
    return request


def _classify_error(status, text):
    """把上游的失败响应翻译成异常，状态码取 ``error_code`` 而非 HTTP。"""
    code = ""
    message = text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            code = str(parsed.get("error_code") or "")
            message = str(parsed.get("error_msg") or parsed.get("message") or text)
            details = parsed.get("details")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                inner = details[0].get("error_msg")
                if inner:
                    message = "%s (%s)" % (message, inner)
    except ValueError:
        pass
    effective = status_from_code(code) or (status if 400 <= status <= 599 else 400)
    if is_credential_failure(code, message):
        # A dead login is not a bad request: 401 is what tells the caller to
        # re-authenticate rather than retry the same payload.
        raise CodeArtsCredentialError(
            401, "CodeArts login expired - reopen the IDE to renew: " + message,
            code, text)
    raise CodeArtsError(effective, message, code, text)


def parse_response(resp):
    """读一次响应体并判定成功/失败。成功返回解析后的 JSON（或 None）。"""
    raw = resp.read()
    status = getattr(resp, "status", 200)
    text = raw.decode("utf-8", "replace")
    if status >= 400:
        _classify_error(status, text)
    try:
        obj = json.loads(text) if text.strip() else None
    except ValueError:
        obj = None
    # HTTP 200 也可能是失败（怪癖 1）—— 有 error_code 就当失败处理。
    if isinstance(obj, dict) and obj.get("error_code"):
        _classify_error(status, text)
    return obj


def call_chat(payload, session, timeout=600, proxy=""):
    """非流式对话。返回上游 JSON。"""
    request = build_signed_request(CHAT_HOST + CHAT_PATH, payload, session,
                                   "POST", chat_headers(payload.get("model"), session))
    return parse_response(_urlopen(request, timeout, proxy))


def stream_chat(payload, session, timeout=600, proxy=""):
    """流式对话，返回逐行迭代器（调用方负责 close）。

    上游在建立流之前就拒绝请求时（凭据过期、参数错、glm-5.3 以外的
    模型不支持流式等），这里立刻抛异常。若把错误响应当成空流返回，
    客户端会收到一个 0 字节的 SSE 流并误以为是空回答。
    """
    body = dict(payload, stream=True)
    request = build_signed_request(CHAT_HOST + CHAT_PATH, body, session,
                                   "POST", chat_headers(body.get("model"), session))
    response = _urlopen(request, timeout, proxy)
    if isinstance(response, _ErrorResponse):
        try:
            parse_response(response)   # 一定抛异常；成功解析才继续
        except CodeArtsError:
            raise
        raise CodeArtsError(response.status, "upstream rejected the stream request")
    return response


def fetch_credits(session, timeout=30, proxy=""):
    """积分套餐与用量。没有 ``usageTotalPackageCredit`` 表示当前不是积分套餐。"""
    headers = {"X-Language": "zh-cn", "app-id": APP_ID}
    request = build_signed_request(CHAT_HOST + CREDITS_PATH, None, session,
                                   "GET", headers)
    return parse_response(_urlopen(request, timeout, proxy))


def _urlopen(request, timeout, proxy):
    """Open a request, turning an HTTP error into a normal response object.

    ``urlopen`` raises on 4xx/5xx and the exception carries the body. The
    classification below depends on reading that body - upstream encodes the
    real status inside ``error_code`` - so flatten the exception into a
    response and let one code path handle both.
    """
    try:
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            return opener.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return _ErrorResponse(exc.code, exc.read())


class _ErrorResponse(object):
    """Minimal response shim so HTTP errors go through the same parser."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, *_args):
        return self._body

    def close(self):
        pass

    def __iter__(self):
        return iter(())
