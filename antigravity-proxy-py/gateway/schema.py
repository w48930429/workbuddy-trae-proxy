"""JSON Schema -> Google CloudCode / Gemini proto-schema sanitization."""
from typing import Any, Dict, List

ALLOWED_SCHEMA_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "properties",
    "required",
    "items",
    "title",
    "maxItems",
    "minItems",
    "maxLength",
    "minLength",
    "pattern",
    "maximum",
    "minimum",
    "default",
}

def is_json_object(val: Any) -> bool:
    return isinstance(val, dict)

def sanitize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if not is_json_object(value):
        return value
    return sanitize_object(value)

def sanitize_object(schema: Dict[str, Any]) -> Dict[str, Any]:
    s = dict(schema)

    # 1. anyOf / oneOf -> 取第一个非 null 分支合并
    union = s.get("anyOf") or s.get("oneOf")
    if isinstance(union, list):
        branches = [b for b in union if is_json_object(b)]
        if any(b.get("type") in ("null", "NULL") for b in branches):
            s["nullable"] = True
        non_null = next((b for b in branches if b.get("type") not in ("null", "NULL")), None)
        s.pop("anyOf", None)
        s.pop("oneOf", None)
        if non_null:
            s.update(non_null)

    # 2. allOf -> 分支依次浅合并
    all_of = s.get("allOf")
    if isinstance(all_of, list):
        branches = [b for b in all_of if is_json_object(b)]
        s.pop("allOf", None)
        for branch in branches:
            s.update(branch)

    # 3. type 列表 (['string', 'null']) -> 单个类型 + nullable
    type_val = s.get("type")
    if isinstance(type_val, list):
        types = [t for t in type_val if isinstance(t, str)]
        if "null" in types:
            s["nullable"] = True
        first_non_null = next((t for t in types if t != "null"), "string")
        s["type"] = first_non_null

    # 4. 草案 7 数字型 exclusiveMinimum / exclusiveMaximum -> minimum / maximum
    if isinstance(s.get("exclusiveMinimum"), (int, float)):
        s["minimum"] = s.pop("exclusiveMinimum")
    if isinstance(s.get("exclusiveMaximum"), (int, float)):
        s["maximum"] = s.pop("exclusiveMaximum")

    # 5. const -> enum
    if "const" in s and s["const"] is not None:
        s["enum"] = [s.pop("const")]

    # 6. 补全缺失的 type
    if s.get("type") is None:
        if s.get("properties"):
            s["type"] = "object"
        elif s.get("items"):
            s["type"] = "array"
        else:
            s["type"] = "object"

    # 7. 白名单过滤 + 递归清洗
    result: Dict[str, Any] = {}
    for key, value in s.items():
        if key not in ALLOWED_SCHEMA_KEYS:
            continue

        if key == "properties" and is_json_object(value):
            clean_props = {}
            for prop_key, prop_val in value.items():
                clean_props[prop_key] = sanitize_value(prop_val)
            result["properties"] = clean_props
        elif key == "items":
            result["items"] = sanitize_value(value)
        elif key == "required" and isinstance(value, list):
            result["required"] = [entry for entry in value if isinstance(entry, str)]
        else:
            result[key] = value

    if result.get("type") == "object" and "properties" not in result:
        result["properties"] = {}

    return result

def sanitize_google_schema(schema: Any) -> Dict[str, Any]:
    """清洗一个工具参数 schema。"""
    if not is_json_object(schema):
        return {"type": "object", "properties": {}}
    return sanitize_object(schema)
