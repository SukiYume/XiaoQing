"""影视飓风签到的命令路由与定时入口。"""

import logging
from typing import cast

from core.args import parse
from core.plugin_base import bounded_external_text
from core.public_errors import public_error_response

from . import yingshi
from .types import Context, MessageSegments, OneBotEvent, segments

logger = logging.getLogger(__name__)

_HELP_ALIASES = {"help", "帮助", "?"}
_YINGSHI_ALIASES = {"yingshi", "yingshijufeng", "y"}
_HELP_TEXT = (
    "📝 影视飓风远端签到\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "/signin yingshi - 立即签到\n"
    "/signin y - 立即签到（简写）\n"
    "/signin help - 显示帮助\n\n"
    "凭据配置路径: plugins.signin.yingshijufeng"
)


def init(context: Context | None = None) -> None:
    """记录插件初始化完成。"""

    logger.info("Signin plugin initialized")


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
            max_chars=32,
            max_bytes=128,
            default="未知",
        )
        return segments(f"❓ 未知平台: {visible_target}\n\n{_HELP_TEXT}")
    except Exception as exc:
        return cast(
            MessageSegments,
            public_error_response(
                context,
                exc,
                logger=logger,
                component="signin.handle",
            ),
        )


async def scheduled_yingshi(context: Context) -> MessageSegments:
    """Execute the manifest-targeted daily sign-in.

    The core scheduler supplies delivery targets from the schedule entry; this
    handler returns content only and must not invent a target in the payload.
    """

    return await yingshi.yingshi_sign(context)
