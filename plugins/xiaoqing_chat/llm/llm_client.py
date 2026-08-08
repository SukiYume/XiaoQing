"""OpenAI 兼容响应解析与旧导入名的轻量适配。

实际 HTTP、凭据读取、重试和模型 fallback 全部位于 :mod:`core.ai`。本模块只保留
xiaoqing_chat 多处会用到的响应提取函数，以及稳定的内部导入名。
"""

from __future__ import annotations

from typing import Any

from core.ai import AIError as LLMError

from ..utils.json_parsing import normalize_response_content
from .gateway import (
    chat_completions,
    chat_completions_raw_with_fallback_paths,
    chat_completions_with_fallback_paths,
)


def extract_response_choice(data: dict[str, Any]) -> dict[str, Any]:
    """提取第一项 choice，畸形响应返回空对象。"""

    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return {}
    choice = choices[0] or {}
    return choice if isinstance(choice, dict) else {}


def extract_response_content(data: dict[str, Any]) -> str:
    """提取并规范化第一段助手文本。"""

    content = normalize_response_content(data)
    return content if isinstance(content, str) else ""


def extract_response_finish_reason(data: dict[str, Any]) -> str:
    """提取有限的结束原因文本，缺失时返回空串。"""

    value = extract_response_choice(data).get("finish_reason")
    return str(value or "").strip()


__all__ = [
    "LLMError",
    "chat_completions",
    "chat_completions_raw_with_fallback_paths",
    "chat_completions_with_fallback_paths",
    "extract_response_choice",
    "extract_response_content",
    "extract_response_finish_reason",
]
