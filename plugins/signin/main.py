"""影视飓风签到的命令路由与定时入口。"""

import logging
from typing import cast

from core.args import parse
from core.plugin_base import bounded_external_text
from core.public_errors import public_error_response

from . import yingshi
from .types import Context, MessageSegments, OneBotEvent, segments

logger = logging.getLogger(__name__)

_HELP_ALIASES    = {"help", "帮助", "?"}
_YINGSHI_ALIASES = {"yingshi", "yingshijufeng", "y"}
_HELP_TEXT       = (
    "📝 影视飓风远端签到\n"
    "• /signin yingshi\n"
    "  立即签到（简写：/signin y）\n"
    "• /signin help\n"
    "凭据：plugins.signin.yingshijufeng"
)


async def handle(
    command: str,
    args: str,
    event: OneBotEvent,
    context: Context,
) -> MessageSegments:
    """分派帮助和影视飓风签到命令。"""

    try:
        parsed = parse(args)
        if not parsed:
            return segments(_HELP_TEXT)

        target = parsed.first.casefold()
        if target in _HELP_ALIASES:
            if len(parsed) != 1 or parsed.options:
                return segments("❌ 用法: /signin help")
            return segments(_HELP_TEXT)
        if target in _YINGSHI_ALIASES:
            if len(parsed) != 1 or parsed.options:
                return segments("❌ 用法: /signin yingshi")
            return await yingshi.yingshi_sign(context)
        visible_target = bounded_external_text(
            target,
            max_chars = 32,
            max_bytes = 128,
            default   = "未知",
        )
        return segments(f"❓ 未知平台: {visible_target}\n\n{_HELP_TEXT}")
    except Exception as exc:
        return cast(
            MessageSegments,
            public_error_response(
                context,
                exc,
                logger    = logger,
                component = "signin.handle",
            ),
        )


async def scheduled_yingshi(context: Context) -> MessageSegments:
    """执行 manifest 指定的每日签到。

    Core 调度器根据计划项补充投递目标；本处理器只返回内容，不能在载荷中自行
    构造群号或用户号。
    """

    return await yingshi.yingshi_sign(context)
