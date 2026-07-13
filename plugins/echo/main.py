"""
示例插件：echo
展示插件基本编写规范和命令处理流程
"""
import logging

from core.plugin_base import segments
from core.public_errors import public_error_response

logger = logging.getLogger(__name__)


def init(context=None) -> None:
    """插件初始化"""
    pass


async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        # echo 命令：回显用户输入
        if command == "echo" or command == "回显":
            if not args.strip():
                return segments(_show_echo_help())

            logger.info("Echo command accepted: length=%d", len(args))
            return segments(args.strip())

        # hello 命令：打招呼
        if command == "hello" or command == "你好":
            user_id = event.get("user_id", "未知用户")
            return segments(f"你好，{user_id}！👋")

        return segments(f"未知命令: {command}")

    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="echo.handle")


def _show_echo_help() -> str:
    """显示 echo 命令帮助信息"""
    return """
📢 **Echo 插件**

**基本用法:**
• /echo <文本> - 回显任意文本内容
• /hello - 向你打招呼

**示例:**
• /echo 你好世界
• /echo 这是一个测试消息

输入 /echo 查看此帮助
""".strip()
