"""Qoder 上游客户端 —— 纯 Python 移植。

来源：qoder2api/internal/bridge/*.go（client.go / delta.go / bridge.go 核心）
      + account/region.go（区域端点）。

职责：
- 区域端点（global / cn）
- BearerClient：cosy 签名请求头、GET/POST、SSE 流式读取
- 会话建立：PAT / OAuth device token → cosy SessionContext
- ExtractDelta：解析上游 SSE 信封 {"headers","body","statusCodeValue"}
- 模型名映射与模型列表解析

依赖：qoder_cosy.py（同目录）、qoder_baseprompt.json（请求模板）
自测：python qoder_client.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

import qoder_cosy as cosy

HERE = os.path.dirname(os.path.abspath(__file__))
BASEPROMPT_PATH = os.path.join(HERE, "qoder_baseprompt.json")

# 瞬时故障重试次数与退避（等价 Go: TransientMaxRetries / RetryBackoff）
TRANSIENT_MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = [0, 1, 2]


# ---------------------------------------------------------------------------
# account/region.go —— 区域端点
# ---------------------------------------------------------------------------

REGION_GLOBAL = "global"
REGION_CN = "cn"


@dataclass
class Endpoints:
    device_login_base: str
    poll_endpoint: str
    userinfo_base: str
    plan_endpoint: str
    quota_endpoint: str
    chat_stream_url: str
    model_list_url: str
    job_token_url: str
    openapi_host: str


_ENDPOINTS = {
    REGION_GLOBAL: Endpoints(
        device_login_base="https://qoder.com/device/selectAccounts",
        poll_endpoint="https://openapi.qoder.sh/api/v1/deviceToken/poll",
        userinfo_base="https://openapi.qoder.sh/api/v1/userinfo",
        plan_endpoint="https://openapi.qoder.sh/api/v2/user/plan",
        quota_endpoint="https://openapi.qoder.sh/api/v2/quota/usage",
        chat_stream_url=(
            "https://api1.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation"
            "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
        ),
        model_list_url="https://api2.qoder.sh/algo/api/v2/model/list?Encode=1",
        job_token_url="https://center.qoder.sh/algo/api/v3/user/jobToken?Encode=1",
        openapi_host="openapi.qoder.sh",
    ),
    REGION_CN: Endpoints(
        device_login_base="https://qoder.com.cn/device/selectAccounts",
        poll_endpoint="https://openapi.qoder.com.cn/api/v1/deviceToken/poll",
        userinfo_base="https://openapi.qoder.com.cn/api/v1/userinfo",
        plan_endpoint="https://openapi.qoder.com.cn/api/v2/user/plan",
        quota_endpoint="https://openapi.qoder.com.cn/api/v2/quota/usage",
        chat_stream_url=(
            "https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation"
            "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
        ),
        model_list_url="https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1",
        job_token_url="https://gateway.qoder.com.cn/algo/api/v3/user/jobToken?Encode=1",
        openapi_host="openapi.qoder.com.cn",
    ),
}


def normalize_region(s: str) -> str:
    return REGION_CN if (s or "").lower() == "cn" else REGION_GLOBAL


def get_endpoints(region: str) -> Endpoints:
    return _ENDPOINTS[normalize_region(region)]


# ---------------------------------------------------------------------------
# 模型名映射（bridge.go InferAgent / MapModel）
# ---------------------------------------------------------------------------

DEFAULT_MODEL_MAPPING = {
    "opus": "qmodel_38max",
    "sonnet": "gmodel",
    "haiku": "qfmodel",
    "gpt": "dmodel",
    "gemini": "gmodel",
}


def infer_agent(model: str) -> str:
    low = (model or "").lower()
    if "gemini" in low:
        return "gemini"
    if any(k in low for k in ("claude", "sonnet", "opus", "haiku")):
        return "claude"
    return "codex"


def _lookup_mapping(model: str, table: dict) -> str:
    if not table:
        return ""
    if model in table and table[model]:
        return table[model]
    pairs = [kv for kv in table.items() if kv[0] and kv[1]]
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    m_low = model.lower()
    for src, dst in pairs:
        s_low = src.lower()
        if s_low in m_low or m_low in s_low:
            return dst
    return ""


def map_model(model: str, agent: str = "", user_mappings: Optional[dict] = None) -> str:
    """等价 Go: MapModel —— 用户映射 > 内置默认 > 大小写归一。

    user_mappings 形如 {"claude": {"sonnet": "gmodel"}, ...}。
    """
    if not model:
        return model
    if user_mappings and agent:
        table = user_mappings.get(agent) or {}
        mapped = _lookup_mapping(model, table)
        if mapped:
            return mapped
    mapped = _lookup_mapping(model, DEFAULT_MODEL_MAPPING)
    if mapped:
        return mapped
    return model.lower()


# ---------------------------------------------------------------------------
# Delta —— extract delta.go ExtractDelta
# ---------------------------------------------------------------------------

@dataclass
class Delta:
    role: str = ""
    content: str = ""
    reasoning: str = ""
    tool_calls: Optional[list] = None
    input_tokens: int = 0
    output_tokens: int = 0
    err: Optional[BaseException] = None

    def is_empty(self) -> bool:
        return (self.role == "" and self.content == "" and self.reasoning == ""
                and not self.tool_calls and self.input_tokens == 0
                and self.output_tokens == 0 and self.err is None)


class UpstreamError(Exception):
    """上游错误（结构化）。status=0 表示流内业务错误。"""

    def __init__(self, status: int, detail: str, err_type: str = ""):
        self.status = status
        self.detail = detail
        self.err_type = err_type
        super().__init__(f"upstream status={status} detail={detail[:200]}")


def _to_envelope_status(v) -> int:
    if isinstance(v, bool):
        return 502
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return 502
    return 502


def extract_delta(data_line: str) -> Delta:
    """等价 Go: ExtractDelta —— 解析上游 SSE 信封。

    上游帧：data:{"headers":{...},"body":"<内层 chunk>","statusCodeValue":200}
    """
    try:
        wrapper = json.loads(data_line)
    except json.JSONDecodeError:
        return Delta()
    if not isinstance(wrapper, dict):
        return Delta()

    # 信封状态检查：statusCodeValue != 200 → 上游故障
    if "statusCodeValue" in wrapper:
        status = _to_envelope_status(wrapper.get("statusCodeValue"))
        if status != 200:
            return Delta(err=UpstreamError(status, _envelope_detail(wrapper)))

    inner = wrapper.get("body")
    if not isinstance(inner, str) or inner == "":
        return Delta()
    try:
        inner_json = json.loads(inner)
    except json.JSONDecodeError:
        return Delta()
    if not isinstance(inner_json, dict):
        return Delta()

    usage_in = usage_out = 0
    usage = inner_json.get("usage")
    if isinstance(usage, dict):
        usage_in = int(usage.get("prompt_tokens") or 0)
        usage_out = int(usage.get("completion_tokens") or 0)

    choices = inner_json.get("choices") or []
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta")
        if not isinstance(delta, dict):
            continue
        role = delta.get("role") or ""
        content = delta.get("content") or ""
        reasoning = delta.get("reasoning_content") or ""
        tool_calls = delta.get("tool_calls")
        if not (isinstance(tool_calls, list) and tool_calls):
            tool_calls = None
        if role or content or reasoning or tool_calls:
            return Delta(role=role, content=content, reasoning=reasoning,
                         tool_calls=tool_calls,
                         input_tokens=usage_in, output_tokens=usage_out)

    # 流内业务错误：{"code":"115","message":"..."}
    code = inner_json.get("code")
    if isinstance(code, str) and code not in ("", "0"):
        msg = inner_json.get("message") or ""
        err_type = "content_policy_rejected" if _is_content_policy(code, msg) else ""
        return Delta(err=UpstreamError(0, f"upstream error code={code}: {msg}", err_type))

    if usage_in > 0 or usage_out > 0:
        return Delta(input_tokens=usage_in, output_tokens=usage_out)
    return Delta()


def _envelope_detail(wrapper: dict) -> str:
    body = wrapper.get("body")
    if isinstance(body, str) and body:
        return body
    try:
        return json.dumps(wrapper, ensure_ascii=False)[:400]
    except (TypeError, ValueError):
        return ""


def _is_content_policy(code: str, msg: str) -> bool:
    low = (code + " " + msg).lower()
    return any(k in low for k in ("content", "policy", "sensitive", "审核", "违规"))


def is_transient_upstream(status: int, detail: str) -> bool:
    """等价 Go: IsTransientUpstream —— 418/5xx 视为瞬时。"""
    if status == 418 or status >= 500:
        return True
    return False


# ---------------------------------------------------------------------------
# BearerClient（client.go）
# ---------------------------------------------------------------------------

class BearerClient:
    def __init__(self, sess: cosy.SessionContext, timeout: int = 30, proxy: str = ""):
        self.sess = sess
        self.timeout = timeout
        self.proxy = proxy or ""

    def _opener(self):
        if self.proxy:
            handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            return urllib.request.build_opener(handler)
        # 显式不走代理：Qoder 为国内服务，避免误走本地代理
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def build_headers(self, path_sig: str, body: str, accept: str,
                      extra: Optional[dict] = None) -> dict:
        """等价 Go: buildHeaders —— 组装 cosy 签名头。"""
        payload_b64 = cosy.build_payload_b64(self.sess.info)
        date = str(cosy.unix_sec())
        sig = cosy.sign_request(payload_b64, self.sess.cosy_key, date, body, path_sig)
        bearer = cosy.compose_bearer(payload_b64, sig)
        h = {
            "cosy-data-policy": "agree",
            "content-type": "application/json",
            "cosy-machinetype": self.sess.machine_type,
            "cosy-clienttype": "5",
            "cosy-date": date,
            "cosy-user": self.sess.identity.uid,
            "cosy-key": self.sess.cosy_key,
            "cache-control": "no-cache",
            "accept": accept,
            "authorization": bearer,
            "cosy-version": cosy.VERSION,
            "cosy-machineid": self.sess.machine_id,
            "cosy-machinetoken": self.sess.machine_token,
            "login-version": "v2",
            "user-agent": "Go-http-client/2.0",
            "cosy-scene": "assistant",
            "cosy-business-product": "ide",
            "cosy-business-type": "agent",
        }
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def path_sig_from(url: str) -> str:
        """等价 Go: PathSigFrom —— 去掉 /algo 前缀的路径。"""
        from urllib.parse import urlparse
        p = urlparse(url).path
        if p.startswith("/algo"):
            p = p[len("/algo"):]
        return p

    def call_get(self, url: str) -> dict:
        """等价 Go: callGet —— body 参与签名为空串。"""
        headers = self.build_headers(self.path_sig_from(url), "", "application/json")
        req = urllib.request.Request(url, headers=headers, method="GET")
        opener = self._opener()
        try:
            with opener.open(req, timeout=15) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace") if e.fp else str(e)
            raise UpstreamError(e.code, detail) from e
        return json.loads(data.decode())

    def call_post(self, url: str, json_body) -> dict:
        """等价 Go: callPost —— body 经 cosy.Encode 编码后签名。"""
        path_sig = self.path_sig_from(url)
        body_str = ""
        if json_body is not None:
            plain = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode()
            body_str = cosy.encode(plain)
        opener = self._opener()
        last_err: Optional[BaseException] = None
        for attempt in range(TRANSIENT_MAX_RETRIES + 1):
            if attempt > 0:
                import time as _t
                _t.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            headers = self.build_headers(path_sig, body_str, "application/json")
            req = urllib.request.Request(
                url, data=body_str.encode(), headers=headers, method="POST")
            transport_err = False
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    data = resp.read()
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace") if e.fp else str(e)
                last_err = UpstreamError(e.code, detail)
                if is_transient_upstream(e.code, detail) and attempt < TRANSIENT_MAX_RETRIES:
                    continue
                raise last_err from e
            except Exception as e:
                transport_err = True
                last_err = e
                if pool.is_transient_transport(e) and attempt < TRANSIENT_MAX_RETRIES:
                    continue
                raise
            return json.loads(data.decode())
        if last_err is None:
            last_err = RuntimeError("call_post failed")
        raise _wrap_transport_error(last_err)


    def open_stream(self, url: str, json_body, on_line: Callable[[str], bool],
                    extra: Optional[dict] = None) -> None:
        """等价 Go: openStreamLines —— POST 后逐行读取 SSE。

        on_line 返回 False 表示消费方要求停止读取（如已捕获错误帧）。
        """
        path_sig = self.path_sig_from(url)
        plain = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode()
        body_str = cosy.encode(plain)
        opener = self._opener()
        last_err: Optional[BaseException] = None
        for attempt in range(TRANSIENT_MAX_RETRIES + 1):
            if attempt > 0:
                import time as _t
                _t.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            headers = self.build_headers(path_sig, body_str, "text/event-stream", extra)
            req = urllib.request.Request(
                url, data=body_str.encode(), headers=headers, method="POST")
            try:
                resp = opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace") if e.fp else str(e)
                last_err = UpstreamError(e.code, detail)
                if is_transient_upstream(e.code, detail) and attempt < TRANSIENT_MAX_RETRIES:
                    continue
                raise last_err from e
            # 建流成功：进入流式读取，不再本层重试
            try:
                for raw in resp:
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    if line == "":
                        continue
                    if not on_line(line):
                        return
            finally:
                resp.close()
            return
        raise last_err if last_err else RuntimeError("open_stream failed")


# ---------------------------------------------------------------------------
# 会话建立（bridge.go NewBridge / ParseOAuthSecret / FetchUserInfo）
# ---------------------------------------------------------------------------

def _str_val(m: dict, key: str) -> str:
    v = m.get(key)
    return v if isinstance(v, str) else ""


def parse_oauth_secret(secret: str):
    """等价 Go: ParseOAuthSecret —— 返回 (device_token, refresh_token)。"""
    secret = (secret or "").strip()
    if not secret:
        return "", ""
    if secret.startswith("dt-") or secret.startswith("drt-"):
        return secret, ""
    try:
        payload = json.loads(secret)
    except json.JSONDecodeError:
        return secret, ""
    if isinstance(payload, dict):
        return payload.get("device_token", "") or "", payload.get("refresh_token", "") or ""
    return secret, ""


def resolve_oauth_user_id(user_info: dict) -> str:
    for k in ("id", "userId", "uid"):
        v = _str_val(user_info, k)
        if v:
            return v
    return ""


def fetch_user_info_with_token(token: str, region: str, proxy: str = "") -> dict:
    ep = get_endpoints(region)
    req = urllib.request.Request(ep.userinfo_base, headers={"Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy
        else urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def build_session_from_secret(secret: str, region: str = REGION_GLOBAL,
                              proxy: str = "") -> "BridgeSession":
    """等价 Go: NewBridge —— 由 PAT / OAuth device token 建立 cosy 会话。

    直接凭证：dt-（device token）和 drt-（refresh token）均可直接作为
    Bearer 使用（实测 Qoder 两者都能建立可用会话）。
    """
    ep = get_endpoints(region)
    device_token, refresh_token = parse_oauth_secret(secret)
    if device_token.startswith(("dt-", "drt-")):
        user_info = fetch_user_info_with_token(device_token, region, proxy)
        name = _str_val(user_info, "name")
        uid = resolve_oauth_user_id(user_info)
        identity = cosy.AuthIdentity(
            name=name, aid=uid, uid=uid,
            organization_id=_str_val(user_info, "organization_id"),
            organization_name=_str_val(user_info, "organization_name"),
            user_type=_str_val(user_info, "userType") or "personal_standard",
            security_oauth_token=device_token,
            refresh_token=refresh_token,
        )
    else:
        seed = cosy.fingerprint_seed("", secret)
        jt = cosy.exchange_job_token(
            secret, cosy.derive_machine_id(seed), cosy.derive_machine_token(seed),
            cosy.derive_machine_type(seed), ep.job_token_url)
        name = _str_val(jt, "name")
        uid = _str_val(jt, "id")
        identity = cosy.AuthIdentity(
            name=name, aid=uid, uid=uid,
            user_type=_str_val(jt, "userType") or "personal_standard",
            security_oauth_token=_str_val(jt, "securityOauthToken"),
            refresh_token=_str_val(jt, "refreshToken"),
        )
    seed = cosy.fingerprint_seed(uid, secret)
    sess = cosy.new_session(
        identity, cosy.derive_machine_id(seed),
        cosy.derive_machine_token(seed), cosy.derive_machine_type(seed))
    return BridgeSession(name=name, uid=uid, region=normalize_region(region), session=sess)


@dataclass
class BridgeSession:
    name: str
    uid: str
    region: str
    session: cosy.SessionContext

    def client(self, proxy: str = "") -> BearerClient:
        return BearerClient(self.session, proxy=proxy)


# ---------------------------------------------------------------------------
# 模型列表（bridge.go ListAvailableModels / parseQoderModels）
# ---------------------------------------------------------------------------

def list_available_models(client: BearerClient, region: str) -> list:
    ep = get_endpoints(region)
    resp = client.call_get(ep.model_list_url)
    return parse_qoder_models(resp)


def parse_qoder_models(resp: dict) -> list:
    for cat in ("assistant", "developer", "chat"):
        raw = resp.get(cat)
        if isinstance(raw, list):
            out = _extract_models(raw)
            if out:
                return out
    return []


def _extract_models(raw_list: list) -> list:
    out = []
    for it in raw_list:
        if not isinstance(it, dict):
            continue
        ctx = 0
        cc = it.get("context_config")
        if isinstance(cc, dict):
            default_cfg = None
            for cfg in cc.values():
                if isinstance(cfg, dict) and cfg.get("is_default"):
                    default_cfg = cfg
                    break
            if default_cfg is None:
                for cfg in cc.values():
                    if isinstance(cfg, dict):
                        default_cfg = cfg
                        break
            if default_cfg is not None:
                ctx = int(default_cfg.get("token_count") or 0)
        max_input = int(it.get("max_input_tokens") or 0)
        if ctx == 0:
            ctx = max_input
        is_reasoning = bool(it.get("is_reasoning"))
        out.append({
            "key": _str_val(it, "key"),
            "display_name": _str_val(it, "display_name"),
            "enable": bool(it.get("enable")),
            "is_default": bool(it.get("is_default")),
            "is_reasoning": is_reasoning,
            "context_window": ctx,
            "max_output_tokens": 32768 if is_reasoning else 16384,
            "max_input_tokens": max_input,
            "price_factor": float(it.get("price_factor") or 0),
        })
    return out


def load_base_prompt(path: str = BASEPROMPT_PATH) -> dict:
    """等价 Go: startBridgeWithAccount —— 先替换 {UUID1..5}/{TIME1} 占位符再解析。"""
    with open(path, "r", encoding="utf-8") as f:
        tmpl = f.read()
    for key in ("{UUID1}", "{UUID2}", "{UUID3}", "{UUID4}", "{UUID5}"):
        tmpl = tmpl.replace(key, cosy.new_uuid())
    tmpl = tmpl.replace("{TIME1}", str(cosy.unix_ms()))
    return json.loads(tmpl)


# ---------------------------------------------------------------------------
# 自测（离线，不涉及网络）
# ---------------------------------------------------------------------------

def _wrap_transport_error(err: Exception) -> Exception:
    """等价 Go: WrapTransportError —— 瞬时传输错误包装为 UpstreamError(502)。"""
    if err is None:
        return RuntimeError("unknown error")
    if pool.is_transient_transport(err):
        return pool.UpstreamError(502, str(err))
    return err


if __name__ == "__main__":
    fails = []

    # 1) 区域端点
    assert get_endpoints("cn").openapi_host == "openapi.qoder.com.cn"
    assert get_endpoints("").openapi_host == "openapi.qoder.sh"
    assert normalize_region("CN") == "cn"
    print("[OK] 区域端点")

    # 2) 模型映射
    assert map_model("claude-sonnet-4-6") == "gmodel"
    assert map_model("claude-opus-4-6-thinking") == "qmodel_38max"
    assert map_model("gpt-5") == "dmodel"
    assert map_model("Performance") == "performance"
    assert infer_agent("claude-sonnet-4-6") == "claude"
    assert infer_agent("gemini-3.8-flash") == "gemini"
    assert infer_agent("qmodel_38max") == "codex"
    print("[OK] 模型映射/agent 推断")

    # 3) ParseOAuthSecret
    assert parse_oauth_secret("dt-abc") == ("dt-abc", "")
    assert parse_oauth_secret('{"device_token":"dt-x","refresh_token":"drt-y"}') == ("dt-x", "drt-y")
    assert parse_oauth_secret("pt-plain") == ("pt-plain", "")
    print("[OK] ParseOAuthSecret")

    # 4) ExtractDelta —— 正常内容帧
    import base64 as _b64
    inner = json.dumps({"choices": [{"delta": {"role": "assistant", "content": "hello"}}]})
    frame = json.dumps({"headers": {}, "body": inner, "statusCodeValue": 200})
    d = extract_delta(frame)
    assert d.content == "hello" and d.role == "assistant", d
    print("[OK] ExtractDelta 内容帧")

    # 5) ExtractDelta —— 信封错误帧（418）
    err_frame = json.dumps({"statusCodeValue": 418, "body": "rate limited"})
    d = extract_delta(err_frame)
    assert isinstance(d.err, UpstreamError) and d.err.status == 418, d
    print("[OK] ExtractDelta 信封错误帧")

    # 6) ExtractDelta —— 流内业务错误
    biz_inner = json.dumps({"code": "115", "message": "需要升级套餐"})
    biz_frame = json.dumps({"body": biz_inner, "statusCodeValue": 200})
    d = extract_delta(biz_frame)
    assert isinstance(d.err, UpstreamError) and d.err.status == 0, d
    print("[OK] ExtractDelta 业务错误帧")

    # 7) ExtractDelta —— usage + [DONE] 噪声帧
    usage_inner = json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    d = extract_delta(json.dumps({"body": usage_inner, "statusCodeValue": 200}))
    assert d.input_tokens == 10 and d.output_tokens == 20, d
    assert extract_delta("not-json").is_empty()
    assert extract_delta(json.dumps({"body": ""})).is_empty()
    print("[OK] ExtractDelta usage/噪声帧")

    # 8) 模型列表解析
    models = parse_qoder_models({"assistant": [
        {"key": "auto", "display_name": "Auto", "enable": True, "is_default": True},
        {"key": "dmodel", "display_name": "D", "enable": False},
    ]})
    assert len(models) == 2 and models[0]["key"] == "auto", models
    print("[OK] 模型列表解析")

    # 9) 请求模板可加载
    bp = load_base_prompt()
    assert "model_config" in bp and "messages" in bp
    print("[OK] baseprompt 模板加载")

    if fails:
        raise SystemExit(f"自测失败: {fails}")
    print("\n全部通过 ✅")