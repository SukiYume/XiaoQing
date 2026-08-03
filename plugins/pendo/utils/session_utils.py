"""兼容不同消息上下文的 Pendo 会话创建与结束工具。"""

from __future__ import annotations

from typing import Any


async def safe_end_session(context: Any) -> bool:
    """上下文支持会话接口时结束当前会话，否则返回 ``False``。"""
    if context is None:
        return False

    end_session = getattr(context, "end_session", None)
    if not callable(end_session):
        return False

    result = await end_session()
    return bool(result)


async def safe_create_session(
    context: Any,
    initial_data: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> bool:
    """上下文支持会话接口时创建会话，否则返回 ``False``。"""
    if context is None:
        return False

    create_session = getattr(context, "create_session", None)
    if not callable(create_session):
        return False

    await create_session(initial_data=initial_data, timeout=timeout)
    return True
