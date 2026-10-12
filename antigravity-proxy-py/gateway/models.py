"""Model definitions and dynamic routing resolution."""
from typing import Dict, List, Literal, Optional, Tuple, TypedDict

ThinkingEffort = Literal["low", "medium", "high", "none"]

class ModelDefinition(TypedDict, total=False):
    id: str
    name: str
    pool: Literal["cli", "sandbox"]
    description: str
    contextLength: int
    maxOutputTokens: int

SUPPORTED_MODELS: List[ModelDefinition] = [
    # 1. Anthropic 官方正统版本
    {
        "id": "claude-3-7-sonnet",
        "name": "Claude 3.7 Sonnet",
        "pool": "sandbox",
        "description": "Anthropic 官方最新旗舰编程模型 (映射 claude-sonnet-4-6)",
        "contextLength": 650000,
        "maxOutputTokens": 8192,
    },
    {
        "id": "claude-3-5-sonnet",
        "name": "Claude 3.5 Sonnet",
        "pool": "sandbox",
        "description": "Anthropic 官方主力编程模型 (CC Switch 推荐标准名，映射 claude-sonnet-4-6)",
        "contextLength": 650000,
        "maxOutputTokens": 8192,
    },
    {
        "id": "claude-3-opus",
        "name": "Claude 3 Opus",
        "pool": "sandbox",
        "description": "Anthropic 官方 Claude 3 Opus 旗舰模型 (映射 claude-opus-4-6-thinking)",
        "contextLength": 650000,
        "maxOutputTokens": 8192,
    },
    {
        "id": "claude-3-5-haiku",
        "name": "Claude 3.5 Haiku",
        "pool": "sandbox",
        "description": "Anthropic 官方轻量快速模型 (映射高速 Sonnet 底座)",
        "contextLength": 650000,
        "maxOutputTokens": 8192,
    },
    # 2. Google Gemini 纯净模型
    {
        "id": "gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "pool": "cli",
        "description": "Google 官方 Gemini 3.8 Flash (思考强度由 CC/Codex 框架动态控制)",
        "contextLength": 650000,
        "maxOutputTokens": 65536,
    },
    {
        "id": "gemini-3.7-flash",
        "name": "Gemini 3.7 Flash",
        "pool": "cli",
        "description": "Google 官方 Gemini 3.7 Flash (思考强度由 CC/Codex 框架动态控制)",
        "contextLength": 650000,
        "maxOutputTokens": 65536,
    },
    {
        "id": "gemini-3.6-flash",
        "name": "Gemini 3.6 Flash",
        "pool": "cli",
        "description": "Google 官方 Gemini 3.6 Flash (思考强度由 CC/Codex 框架动态控制)",
        "contextLength": 650000,
        "maxOutputTokens": 65536,
    },
    {
        "id": "gemini-3.1-pro",
        "name": "Gemini 3.1 Pro",
        "pool": "cli",
        "description": "Google 官方 Gemini 3.1 Pro 深度推理与 Agent 底座",
        "contextLength": 650000,
        "maxOutputTokens": 65536,
    },
    {
        "id": "gemini-2.5-pro",
        "name": "Gemini 2.5 Pro",
        "pool": "cli",
        "description": "Google 官方 Gemini 2.5 Pro 经典模型",
        "contextLength": 650000,
        "maxOutputTokens": 65536,
    },
    {
        "id": "gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "pool": "cli",
        "description": "Google 官方 Gemini 2.5 Flash 极速模型",
        "contextLength": 650000,
        "maxOutputTokens": 65536,
    },
    # 3. OpenAI / 开源旗舰标准名
    {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "pool": "sandbox",
        "description": "OpenAI 官方 GPT-4o 旗舰标识 (映射 Google 120B 开源底座)",
        "contextLength": 128000,
        "maxOutputTokens": 8192,
    },
    {
        "id": "gpt-4o-mini",
        "name": "GPT-4o Mini",
        "pool": "sandbox",
        "description": "OpenAI 官方 GPT-4o Mini 轻量标识",
        "contextLength": 128000,
        "maxOutputTokens": 8192,
    },
    {
        "id": "gpt-oss-120b",
        "name": "GPT-OSS 120B",
        "pool": "sandbox",
        "description": "Google Antigravity 官方开源 120B 旗舰模型",
        "contextLength": 128000,
        "maxOutputTokens": 8192,
    },
    # 4. Google Antigravity 原生内部底座名
    {
        "id": "claude-opus-4-6-thinking",
        "name": "Claude Opus 4.6 (Thinking)",
        "pool": "sandbox",
        "description": "Google Antigravity 内部 Claude Opus 原生底座名",
        "contextLength": 650000,
        "maxOutputTokens": 8192,
    },
    {
        "id": "claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "pool": "sandbox",
        "description": "Google Antigravity 内部 Claude Sonnet 原生底座名",
        "contextLength": 650000,
        "maxOutputTokens": 8192,
    },
]

import re

def resolve_dynamic_upstream_model(input_model: str, effort: ThinkingEffort = "high") -> Tuple[str, Literal["cli", "sandbox"]]:
    """根据客户端请求的模型与框架传入的思考预算，动态路由至 Google 上游."""
    clean = re.sub(r"\[.*?\]", "", input_model).strip().lower()
    if clean.startswith("models/"):
        clean = clean[len("models/"):]

    # 1. Claude Opus 系列
    if "opus" in clean:
        return "claude-opus-4-6-thinking", "sandbox"

    # 2. Claude Sonnet & Haiku
    if "sonnet" in clean or "haiku" in clean:
        return "claude-sonnet-4-6", "sandbox"

    # 3. Gemini 3.8
    if "3.8" in clean:
        if effort == "low" or clean.endswith("-low"):
            return "gemini-3.8-flash-low", "cli"
        if effort == "medium" or clean.endswith("-medium"):
            return "gemini-3.8-flash-medium", "cli"
        return "gemini-3.8-flash-high", "cli"

    # 4. Gemini 3.7
    if "3.7" in clean:
        if effort == "low" or clean.endswith("-low"):
            return "gemini-3.7-flash-low", "cli"
        if effort == "medium" or clean.endswith("-medium"):
            return "gemini-3.7-flash-medium", "cli"
        return "gemini-3.7-flash-high", "cli"

    # 5. Gemini 3.6
    if "3.6" in clean:
        if effort == "low" or clean.endswith("-low"):
            return "gemini-3.6-flash-low", "cli"
        if effort == "medium" or clean.endswith("-medium"):
            return "gemini-3.6-flash-medium", "cli"
        return "gemini-3.6-flash-high", "cli"

    # 6. Gemini 3.1 / Pro
    if "3.1" in clean or "pro" in clean:
        if "2.5" in clean:
            return "gemini-2.5-pro", "cli"
        if effort == "low" or clean.endswith("-low"):
            return "gemini-3.1-pro-low", "cli"
        return "gemini-pro-agent", "cli"

    # 7. Gemini 2.5 Flash
    if "2.5" in clean:
        return "gemini-2.5-flash", "cli"

    # 8. GPT 全系列 -> 映射 Google 原生开源 120B 旗舰底座
    if "gpt" in clean or "oss" in clean:
        return "gpt-oss-120b-medium", "sandbox"

    return clean, "cli"
