"""提供有界文本回显和简单问候，用作最小插件示例。"""

from __future__ import annotations

import logging
from typing import Any

from core.constants import MAX_MESSAGE_TEXT_LENGTH
from core.plugin_base import Segments, has_control_characters, segments
from core.public_errors import public_error_response

logger = logging.getLogger(__name__)

_ECHO_ALIASES = frozenset({"echo", "回显"})
_HELLO_ALIASES = frozenset({"hello", "你好"})
_MAX_USER_ID_DIGITS = 19
_MAX_USER_ID = 2**63 - 1

HELP_TEXT = """
📢 **Echo 插件**

**用法：**
• /echo <文本> - 原样回显去除首尾空白后的文本
• /hello - 使用当前 QQ 号打招呼

回显文本最多 3000 个字符；允许换行和制表符，不接受其他控制字符。
""".strip()


def _display_user_id(event: dict[str, Any]) -> str:
    """只显示可信的正整数 QQ 号，避免任意对象被隐式字符串化。"""

    raw_user_id: object = event.get("user_id")
    if type(raw_user_id) is int:
        user_id = raw_user_id
    elif (
        type(raw_user_id) is str
        and 0 < len(raw_user_id) <= _MAX_USER_ID_DIGITS
        and raw_user_id.isascii()
        and raw_user_id.isdecimal()
    ):
        user_id = int(raw_user_id)
    else:
        return "未知用户"
    return str(user_id) if 0 < user_id <= _MAX_USER_ID else "未知用户"


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: object,
) -> Segments:
    """按清单别名分发两个简单命令，并在边界处限制公开文本。"""

    try:
        if not isinstance(command, str) or not isinstance(args, str) or not isinstance(event, dict):
            raise TypeError("echo command, arguments and event have invalid types")
        if len(args) > MAX_MESSAGE_TEXT_LENGTH:
            return segments(f"命令参数不能超过 {MAX_MESSAGE_TEXT_LENGTH} 个字符")

        if command in _ECHO_ALIASES:
            cleaned = args.strip()
            if not cleaned:
                return segments(HELP_TEXT)
            if has_control_characters(
                cleaned,
                allow_formatting_whitespace=True,
                include_c1=True,
            ):
                return segments("回显文本不能包含不可显示的控制字符")
            logger.info("Echo command accepted: length=%d", len(cleaned))
            return segments(cleaned)

        if command in _HELLO_ALIASES:
            if args.strip():
                return segments("用法：/hello")
            return segments(f"你好，{_display_user_id(event)}！👋")

        return segments("未知命令；请使用 /echo 或 /hello")
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="echo.handle")
