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
    """显示适合手机私聊阅读的管理与会话帮助。"""
    return """
🖥️ SSH 远程控制

连接
• /ssh
  查看服务器与快速用法
• /ssh <服务器名>
• /ssh <用户名>@<服务器名>

服务器
• /ssh list
• /ssh add [名称 主机 [端口] [用户名]]
• /ssh remove <名称>
• /ssh import [Host名|all]
• /ssh config

连接状态
• /ssh status
• /ssh disconnect [服务器名]

会话内
• 直接输入 Shell 命令
• cd <目录>：切换目录
• showimg <路径或通配符> [--page N]
  支持 ./、相对/绝对目录；每页 5 张
• help / 帮助：会话帮助
• 停止：中断当前命令
• 退出 / 取消：结束会话

安全：优先使用密钥；密码只进入插件密钥存储。
默认命令超时 30 秒，会话空闲超时 10 分钟。
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
