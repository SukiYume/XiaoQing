"""跨测试模块复用的领域无关断言。"""

from collections.abc import Callable
from typing import Any

import pytest


def text_segments_text(
    segments: list[dict],
    *,
    separator: str = "",
) -> str:
    """按顺序拼接 OneBot 文本消息段，并允许调用方保留段间分隔。"""
    return separator.join(
        str(segment.get("data", {}).get("text", ""))
        for segment in segments
        if segment.get("type") == "text"
    )


def assert_http_error(status_code: int, call: Callable[[], Any]) -> Any:
    """执行调用并返回指定状态码的 FastAPI HTTP 异常。"""

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as captured:
        call()
    assert captured.value.status_code == status_code
    return captured.value
