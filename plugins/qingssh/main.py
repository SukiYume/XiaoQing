"""QingSSH 命令路由、会话入口及生命周期钩子。"""

import logging
from collections.abc import Awaitable, Callable

from core.router import resolve_context_command_invocation

from . import session_handlers
from .audit import audit_error_type
from .config import SessionKeys
from .handlers import (
    handle_ssh_add,
    handle_ssh_config_list,
    handle_ssh_disconnect,
    handle_ssh_import,
    handle_ssh_list,
    handle_ssh_main,
    handle_ssh_remove,
    handle_ssh_status,
)
from .ssh_manager import SSHManager, get_manager
from .types import Context, MessageSegments, OneBotEvent, Session, segments

logger = logging.getLogger(__name__)

CommandHandler = Callable[
    [str, OneBotEvent, Context, SSHManager],
    Awaitable[MessageSegments],
]

_MAIN_COMMANDS = {"ssh"}
_SUBCOMMAND_ROUTES: dict[str, CommandHandler | None] = {
    "help": None,
    "list": handle_ssh_list,
    "add": handle_ssh_add,
    "remove": handle_ssh_remove,
    "import": handle_ssh_import,
    "config": handle_ssh_config_list,
    "status": handle_ssh_status,
    "disconnect": handle_ssh_disconnect,
}
_LEGACY_ROUTES: dict[str, CommandHandler] = {
    "ssh断开": handle_ssh_disconnect,
    "ssh列表": handle_ssh_list,
    "ssh添加": handle_ssh_add,
    "ssh删除": handle_ssh_remove,
    "ssh导入": handle_ssh_import,
    "sshconfig": handle_ssh_config_list,
    "ssh状态": handle_ssh_status,
}


def init(context: Context | None = None) -> None:
    """插件初始化"""
    logger.info("QingSSH plugin initialized")


async def handle(command: str, args: str, event: OneBotEvent, context: Context) -> MessageSegments:
    """把统一 ``/ssh`` 子命令和旧独立命令分派到同一组处理器。"""

    try:
        manager = await get_manager(context)
        if command in _MAIN_COMMANDS:
            # `/ssh add` 后面全是位置参数；Core 的通用选项解析器会把以 `-`
            # 开头的主机名吞成选项，继而让后续字段错位。这里只拆一级子命令，
            # 剩余原文完整交给对应处理器验证。
            invocation = resolve_context_command_invocation(context, "qingssh.ssh", args)
            command_parts = str(args or "").strip().split(maxsplit=1)
            if invocation is not None and len(invocation.chain) > 1:
                subcommand = invocation.chain[1].name
                route_args = invocation.remainder_after(1)
            else:
                subcommand = command_parts[0].casefold() if command_parts else ""
                route_args = command_parts[1] if len(command_parts) > 1 else ""
            if not command_parts or subcommand not in _SUBCOMMAND_ROUTES:
                return await handle_ssh_main(args, event, context, manager)
            route = _SUBCOMMAND_ROUTES[subcommand]
            if route is None:
                if route_args.strip():
                    return segments("❌ 用法: /ssh help")
                return segments(_show_help())
            return await route(route_args, event, context, manager)

        legacy_route = _LEGACY_ROUTES.get(command)
        if legacy_route is not None:
            return await legacy_route(args, event, context, manager)

        return segments("❓ 未知命令")
    except Exception as exc:
        logger.error("QingSSH handle failed error_type=%s", audit_error_type(exc))
        return segments("处理请求时出错，请查看日志")


def _show_help() -> str:
    """显示帮助信息"""
    return """
🖥️ **SSH 远程控制**

**基本命令:**
• /ssh - 显示已保存的服务器和帮助
• /ssh help - 显示此帮助
• /ssh <服务器名> - 连接到服务器
• /ssh <用户名>@<服务器名> - 以指定用户连接

**服务器管理:**
• /ssh list - 查看已保存的服务器列表
• /ssh add - 添加服务器（引导式）
• /ssh add <名称> <主机> [端口] [用户名] - 快速添加
• /ssh remove <名称> - 删除服务器
• /ssh import - 从 ~/.ssh/config 导入配置
• /ssh config - 查看 ~/.ssh/config 中的 Host

**连接管理:**
• /ssh status - 查看当前连接状态
• /ssh disconnect - 断开当前连接
• /ssh disconnect <服务器名> - 断开指定连接

**SSH 会话中:**
• 直接输入命令 - 执行 Shell 命令
• cd <目录> - 切换工作目录
• help / 帮助 - 查看会话中的命令
• 输入「退出」/「取消」- 结束会话

**特性:**
• 支持多服务器管理
• 支持密钥和密码认证
• 命令历史记录
• 自动补全工作目录
• 10 分钟无操作自动断开

**安全提示:**
• 建议使用 SSH 密钥而非密码
• 密码只写入插件密钥存储，不写入服务器配置文件
• 命令默认 30 秒超时，可在插件配置中调整；设为 0 可不限制
""".strip()


async def handle_session(
    text: str,
    event: OneBotEvent,
    context: Context,
    session: Session,
) -> MessageSegments:
    """把活跃会话消息交给会话状态机。"""

    return await session_handlers.handle_session(text, event, context, session)


async def close_session(event: OneBotEvent, context: Context, session: Session) -> None:
    """结束会话时终止该代后台命令并断开对应连接。"""

    await session_handlers.close_session(context, session)


async def cleanup(context: Context) -> None:
    """先排空后台任务，再关闭已经创建的管理器。"""

    try:
        await session_handlers.shutdown_tasks()
    except Exception as exc:
        logger.error("SSH job cleanup failed error_type=%s", audit_error_type(exc))

    state = getattr(context, "state", None)
    manager = state.get("ssh_manager") if isinstance(state, dict) else None
    if not isinstance(manager, SSHManager):
        return
    try:
        await manager.shutdown()
        logger.info("SSH plugin cleaned up successfully")
    except Exception as exc:
        logger.error("SSH plugin cleanup failed error_type=%s", audit_error_type(exc))


async def shutdown(context: Context) -> None:
    """兼容框架 shutdown 生命周期名称。"""

    await cleanup(context)


async def cleanup_orphans(context: Context) -> None:
    """断开已经没有对应活跃会话的连接。"""

    manager = await get_manager(context)
    if not manager.connections:
        return

    try:
        if not context.session_manager:
            return

        sessions = await context.session_manager.get_all_sessions("qingssh")
        active_keys: set[str] = set()
        for session in sessions:
            server_name = session.get(SessionKeys.SERVER_NAME)
            if isinstance(server_name, str) and server_name:
                key = manager.build_connection_key(
                    session.user_id,
                    session.group_id,
                    server_name,
                )
                active_keys.add(key)

        count = 0
        for key in set(manager.connections) - active_keys:
            try:
                user_id, group_id, server_name = manager.parse_connection_key(key)
                if manager.disconnect(user_id, group_id, server_name):
                    count += 1
            except Exception as exc:
                logger.warning(
                    "SSH orphan cleanup failed error_type=%s",
                    audit_error_type(exc),
                )
        if count:
            logger.info("Cleaned up %d orphan SSH connections", count)
    except Exception as exc:
        logger.error("SSH orphan scan failed error_type=%s", audit_error_type(exc))
