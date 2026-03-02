"""
自动签到插件

支持 Sony、影视飓风等平台的自动签到。
"""

import logging

from core.args import parse
from core.plugin_base import segments

from . import sony
from . import yingshi

logger = logging.getLogger(__name__)


def init(context=None) -> None:
    logger.info("Signin plugin initialized")


async def handle(command: str, args: str, event: dict, context) -> list:
    try:
        parsed = parse(args)
        if not parsed:
            return segments(_show_help())

        target = parsed.first.lower()

        if target in {"help", "帮助", "?"}:
            return segments(_show_help())
        if target in {"sony", "s"}:
            return await sony.sony_sign(context)
        if target in {"yingshi", "yingshijufeng", "y"}:
            return await yingshi.yingshi_sign(context)

        return segments(f"未知平台: {target}\n\n{_show_help()}")
    except Exception as e:
        logger.exception("Signin handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


def _show_help() -> str:
    return """
📝 **自动签到**

**基本用法:**
• /signin sony - Sony 官网签到
• /signin s - Sony 官网签到（简写）
• /signin yingshi - 影视飓风签到
• /signin y - 影视飓风签到（简写）
• /signin help - 显示帮助

**示例:**
• /signin sony
• /signin yingshi

**配置说明:**
需要在 secrets.json 中配置相应平台的账号信息

输入 /signin <平台> 进行签到
""".strip()


async def scheduled_yingshi(context) -> list[dict]:
    return await yingshi.yingshi_sign(context)
