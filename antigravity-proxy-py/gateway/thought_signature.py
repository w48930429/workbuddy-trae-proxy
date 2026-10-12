"""Gemini thought_signature LRU cache."""
from collections import OrderedDict
from typing import Optional

MAX_CACHE_ENTRIES = 1000
_thought_signature_cache: OrderedDict[str, str] = OrderedDict()

def remember_thought_signature(call_id: Optional[str], signature: Optional[str]) -> None:
    if not call_id or not signature:
        return
    _thought_signature_cache[call_id] = signature
    _thought_signature_cache.move_to_end(call_id)
    if len(_thought_signature_cache) > MAX_CACHE_ENTRIES:
        _thought_signature_cache.popitem(last=False)

def resolve_thought_signature_for_replay(call_id: Optional[str]) -> str:
    """优先真实签名；缺失时回退到 Google 绕过哨兵。"""
    if call_id and call_id in _thought_signature_cache:
        return _thought_signature_cache[call_id]
    return "skip_thought_signature_validator"
