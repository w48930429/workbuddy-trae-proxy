"""Gemini thought_signature 缓存 + id 内嵌。

Google CCA 对 Gemini 3.x 的多轮 functionCall 有 thought_signature 校验：
回放的历史 assistant functionCall 必须带签名（真实签名或
`skip_thought_signature_validator` 哨兵），否则上游 HTTP 400。

签名两条回传路径：
 1) 服务端内存缓存，按 call id 查找（原实现，保留作兜底）；
 2) 编码进返回给客户端的 call id：`<id>__ts<len>__<sig>`，客户端原样回传
    即带回签名 —— 跨进程重启 / 多实例 / 客户端改写 id 时更可靠。
    超长签名存本地 store，id 里只放 `<id>__tsk<len>__<key>` 短引用。
长度前缀 <len> 用于识别被客户端截断的残缺签名：残缺的一律当作无签名。
"""
from __future__ import annotations

import base64
import secrets
from collections import OrderedDict
from typing import Optional, Tuple

MAX_CACHE_ENTRIES = 1000
# 超过则改走 store 引用，避免 id 过长撑爆客户端存储
MAX_INLINE_SIGNATURE = 512

_thought_signature_cache: OrderedDict[str, str] = OrderedDict()
_signature_store: OrderedDict[str, str] = OrderedDict()

SIG_MARK = "__ts"
SIG_END = "__"


def _store_put(signature: str) -> str:
    key = "s" + secrets.token_hex(6)
    while key in _signature_store:
        key = "s" + secrets.token_hex(6)
    _signature_store[key] = signature
    if len(_signature_store) > MAX_CACHE_ENTRIES:
        _signature_store.popitem(last=False)
    return key


def signed_id(call_id: Optional[str], signature: Optional[str]) -> Optional[str]:
    """把签名编码进 call id；客户端原样回传 id 时签名即随之带回。"""
    if not signature:
        return call_id
    base = call_id or ("call_" + secrets.token_hex(6))
    if len(signature) > MAX_INLINE_SIGNATURE:
        key = _store_put(signature)
        return f"{base}{SIG_MARK}k{len(key)}{SIG_END}{key}"
    enc = base64.urlsafe_b64encode(signature.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{base}{SIG_MARK}{len(enc)}{SIG_END}{enc}"


def unsigned_id(call_id: Optional[str]) -> Tuple[str, str]:
    """从 call id 拆出真实 id 与签名。无签名、或签名被截断时，原样返回 id、签名空串。"""
    if not call_id:
        return "", ""
    # 正向扫描：找第一个格式合法的标记（payload 自身含 __ts 也不会误命中）
    at = 1
    while at < len(call_id):
        i = call_id.find(SIG_MARK, at)
        if i < 0:
            break
        at = i + 1
        rest = call_id[i + len(SIG_MARK):]
        kept = False
        if rest.startswith("k"):
            kept = True
            rest = rest[1:]
        d = 0
        while d < len(rest) and rest[d].isdigit():
            d += 1
        tail = rest[d:]
        if not tail.startswith(SIG_END):
            continue  # 不是标记：继续找下一个
        if kept and d == 0:
            continue
        payload = tail[len(SIG_END):]
        if not payload:
            return call_id, ""  # 空 payload：当作无签名，原样返回 id
        # 第一个格式合法的标记即决定：残缺（长度不符）视为无签名
        if d > 0 and rest[:d] != str(len(payload)):
            return call_id, ""
        if kept:
            sig = _signature_store.get(payload)
            return (call_id[:i], sig) if sig else (call_id, "")
        try:
            pad = "=" * (-len(payload) % 4)
            sig = base64.urlsafe_b64decode(payload + pad).decode("utf-8")
            return call_id[:i], sig
        except Exception:
            return call_id, ""
    return call_id, ""


def remember_thought_signature(call_id: Optional[str], signature: Optional[str]) -> None:
    """缓存真实签名（响应侧调用），按 call id 索引，作为回传兜底。"""
    if not call_id or not signature:
        return
    _thought_signature_cache[call_id] = signature
    _thought_signature_cache.move_to_end(call_id)
    if len(_thought_signature_cache) > MAX_CACHE_ENTRIES:
        _thought_signature_cache.popitem(last=False)


def resolve_thought_signature_for_replay(call_id: Optional[str]) -> str:
    """回放时取签名：优先用 id 内嵌签名，其次查内存缓存，都拿不到时回退哨兵。"""
    if not call_id:
        return "skip_thought_signature_validator"
    own, sig = unsigned_id(call_id)
    if sig:
        return sig
    return (
        _thought_signature_cache.get(own)
        or _thought_signature_cache.get(call_id)
        or "skip_thought_signature_validator"
    )