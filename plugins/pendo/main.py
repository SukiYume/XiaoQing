"""
Pendo Plugin - 个人时间与信息管理中枢
支持日程管理、待办事项、笔记、日记等功能

主要职责:
1. 插件生命周期管理（init/cleanup）
2. 命令入口路由（handle）
3. 定时任务入口（scheduled_*）
4. 辅助函数（_get_logger, _get_database等）

具体功能实现已拆分到:
- commands/ - 命令处理模块
- handlers/ - 业务处理器
- services/ - 核心服务
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from core.interfaces import PluginContextProtocol
from core.models import PluginManifest
from core.plugin_base import run_sync, segments
from core.public_errors import public_error_message
from core.router import (
    CommandCatalogNode,
    build_command_catalog_node,
    get_context_command_root,
    resolve_catalog_invocation,
    resolve_context_command_invocation,
)

from .commands.operations import handle_confirm, handle_snooze, handle_undo
from .commands.scheduled import (
    check_diary_reminders,
    check_reminders,
    cleanup_expired_demo_data,
    cleanup_reminder_singleton,
    migrate_undone_todos,
    prune_operation_logs,
    send_daily_briefings,
    send_month_end_finance_summaries,
    send_weekly_finance_summaries,
)
from .commands.session import handle_session_message
from .commands.settings import handle_settings
from .config import PendoConfig, PendoRuntimeSettings
from .core.exceptions import (
    InvalidDateRangeException,
    InvalidFieldValueException,
    InvalidTimeFormatException,
    MissingRequiredFieldException,
    NaturalLanguageParseException,
    PastTimeException,
)
from .core.router import CommandRouter
from .core.runtime import (
    get_cached_services,
    get_plugin_runtime_state,
)
from .core.types import PendoContext, PendoServices, SessionData
from .handlers.diary import DiaryHandler
from .handlers.event import EventHandler
from .handlers.ledger import LedgerHandler
from .handlers.note import NoteHandler
from .handlers.search import SearchHandler
from .handlers.task import TaskHandler
from .handlers.web import WebHandler
from .services.ai_parser import AIParser
from .services.db import Database
from .services.exporter import ExporterService
from .services.reminder import ReminderService
from .services.runtime import PendoRuntimeService
from .utils.error_handlers import handle_command_errors, success_result
from .utils.session_utils import safe_end_session
from .utils.settings_utils import PLUGIN_SETTINGS_HELP_LINES

logger = logging.getLogger(__name__)
_runtime_service = PendoRuntimeService()
_SESSION_INPUT_EXCEPTIONS = (
    InvalidDateRangeException,
    InvalidFieldValueException,
    InvalidTimeFormatException,
    MissingRequiredFieldException,
    NaturalLanguageParseException,
    PastTimeException,
)
TRIGGER_SUBCOMMAND_MAP = {
    "日程": "event",
    "待办": "todo",
    "日记": "diary",
}

# ============================================================
# 插件初始化
# ============================================================


def _get_runtime_service(context: PluginContextProtocol | None) -> PendoRuntimeService:
    runtime_state = get_plugin_runtime_state(context, create=False)
    service = runtime_state.get("lifecycle_service")
    if isinstance(service, PendoRuntimeService):
        return service
    return _runtime_service


def _apply_runtime_config(context: PluginContextProtocol) -> bool:
    settings = context.get_settings_snapshot()
    PendoConfig.validate()
    return bool(
        PendoConfig.configure(
            settings.plugin_config("pendo"),
            settings_revision=settings.revision,
        )
    )


def _register_config_reload_hook(context: PluginContextProtocol | None) -> None:
    # 无上下文时没有可订阅的配置能力；先收窄类型，避免回调闭包捕获 Optional。
    if context is None:
        return
    subscription = context.capabilities.config_subscription
    if subscription is None:
        return

    runtime_service = _get_runtime_service(context)
    if runtime_service.has_config_subscription:
        return

    async def _on_reload(_config: Mapping[str, Any]) -> None:
        before = PendoConfig.runtime()
        if not _apply_runtime_config(context):
            return
        after = PendoConfig.runtime()
        database = runtime_service.database
        if database is None:
            raise RuntimeError("Pendo runtime database is unavailable during config reload")
        await asyncio.to_thread(
            _reconfigure_web_server,
            database,
            before,
            after,
        )

    unsubscribe = subscription.subscribe(_on_reload)
    if not runtime_service.bind_config_subscription(unsubscribe):
        unsubscribe()


def init(context: PluginContextProtocol) -> None:
    """插件初始化"""

    _apply_runtime_config(context)
    runtime_state = get_plugin_runtime_state(context)
    runtime_state["lifecycle_service"] = _runtime_service
    database_opened = False
    try:
        db = _runtime_service.open_database(context, Database)
        database_opened = True
        _register_config_reload_hook(context)
        log = _get_logger(context)
        log.info("Pendo plugin initialized, database at %s", _runtime_service.database_path)

        if PendoConfig.runtime().web_enabled:
            _start_web_server(db)
    except BaseException:
        try:
            _runtime_service.unsubscribe_config()
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger=getattr(context, "logger", None) or logger,
                component="pendo.init.config_subscription_rollback",
            )
        if database_opened:
            for failure in _runtime_service.close_databases():
                public_error_message(
                    context,
                    failure.error,
                    logger=getattr(context, "logger", None) or logger,
                    component=f"pendo.init.{failure.component}_rollback",
                )
        if runtime_state.get("lifecycle_service") is _runtime_service:
            runtime_state.pop("lifecycle_service", None)
        raise


def _start_web_server(db: Database) -> bool:
    """Start the Pendo Web UI, replacing an existing in-process server if needed."""
    return bool(_runtime_service.start_web(db))


def _stop_web_server() -> bool:
    return bool(_runtime_service.stop_web())


def _reconfigure_web_server(
    db: Database,
    before: PendoRuntimeSettings,
    after: PendoRuntimeSettings,
) -> None:
    """Apply an already-published Web endpoint generation without dual state."""

    _runtime_service.reconfigure_web(db, before, after)


async def _stop_web_server_async() -> None:
    await _runtime_service.stop_web_async()


def _cleanup_resources(context=None, *, stop_web: bool) -> None:
    """Release Pendo resources shared by cleanup and shutdown hooks."""
    runtime_service = _get_runtime_service(context)

    try:
        runtime_service.unsubscribe_config()
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=getattr(context, "logger", None) or logger,
            component="pendo.cleanup.config_subscription",
        )

    if stop_web:
        _stop_web_server()

    for failure in runtime_service.close_databases():
        public_error_message(
            context,
            failure.error,
            logger=getattr(context, "logger", None) or logger,
            component=f"pendo.cleanup.{failure.component}",
        )

    try:
        cleanup_reminder_singleton()  # 插件卸载后不得保留绑定旧数据库的服务单例。
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=getattr(context, "logger", None) or logger,
            component="pendo.cleanup.reminders",
        )

    try:
        runtime_state = get_plugin_runtime_state(context, create=False)
        runtime_state.clear()
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=getattr(context, "logger", None) or logger,
            component="pendo.cleanup.runtime_state",
        )

    log = _get_logger(context)
    log.info("Pendo plugin cleanup completed")


def cleanup(context=None) -> None:
    """插件清理函数 - 在插件卸载时调用"""
    _cleanup_resources(context, stop_web=True)


async def shutdown(context=None) -> None:
    """插件异步卸载钩子，供应用在 Ctrl+C 时优雅关闭。"""
    await _stop_web_server_async()
    _cleanup_resources(context, stop_web=False)


# ============================================================
# 主处理函数
# ============================================================


@handle_command_errors(return_segments=True)
async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol | None,
) -> list[dict[str, Any]]:
    """命令处理入口

    使用统一的错误处理装饰器，提供一致的异常处理和日志记录。

    Args:
        command: 命令名称（通常是'pendo'）
        args: 命令参数
        event: 消息事件
        context: 上下文对象

    Returns:
        消息列表
    """
    log = _get_logger(context)
    user_id = str(event.get("user_id", ""))
    group_id = event.get("group_id")
    raw_message = event.get("raw_message") or f"{command} {args}".strip()
    route_args = _normalize_trigger_args(command, args)

    # 1. 优先检查是否存在活跃会话 (多轮对话)
    # 注意：只处理属于 pendo 的会话，忽略其他插件的会话
    if await _has_active_session(context, plugin_name="pendo"):
        if _is_explicit_pendo_command(raw_message):
            await safe_end_session(context)

    # 2. 解析并路由命令
    return await _handle_command_routing(user_id, route_args, context, group_id, log)


def _is_explicit_pendo_command(raw_message: str) -> bool:
    text = str(raw_message or "").strip().lower()
    if (
        text == "/pendo"
        or text.startswith("/pendo ")
        or text == "pendo"
        or text.startswith("pendo ")
    ):
        return True
    stripped = text.lstrip("/")
    return any(
        stripped == trigger.lower() or stripped.startswith(f"{trigger.lower()} ")
        for trigger in TRIGGER_SUBCOMMAND_MAP
    )


def _normalize_trigger_args(command: str, args: str) -> str:
    command_name = str(command or "").strip().lstrip("/")
    subcommand = TRIGGER_SUBCOMMAND_MAP.get(command_name)
    if not subcommand:
        return args
    return f"{subcommand} {str(args or '').strip()}".strip()


async def _has_active_session(context, plugin_name: str | None = None) -> bool:
    """检查是否存在活跃会话，优先使用不续命的只读快照。

    Args:
        context: 上下文对象
        plugin_name: 插件名称，如果提供则只检查该插件的会话

    Returns:
        是否存在（匹配的）活跃会话
    """
    session = None
    session_manager = getattr(context, "session_manager", None)
    current_user_id = getattr(context, "current_user_id", None)
    peek = getattr(session_manager, "peek", None)
    if callable(peek) and current_user_id is not None:
        session = await peek(current_user_id, getattr(context, "current_group_id", None))
    else:
        get_session = getattr(context, "get_session", None)
        if callable(get_session):
            session = await get_session()

    if session is None:
        return False
    if plugin_name is not None:
        return bool(session.plugin_name == plugin_name)
    return True


async def handle_session(
    text: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
    session: SessionData,
) -> list[dict[str, Any]]:
    """Handle one continuation using the transaction-local Session supplied by core.

    This entrypoint deliberately has no catch-all command decorator.  Expected
    Pendo business and input errors are converted to user-facing messages;
    unexpected failures escape to Dispatcher so SessionManager can roll back
    every mutation made to its isolated working copy.
    """

    user_id = str(event.get("user_id", ""))
    group_id = event.get("group_id")
    try:
        return await _handle_active_session(user_id, text, context, session, group_id)
    except _SESSION_INPUT_EXCEPTIONS as exc:
        _get_logger(context).warning(
            "Pendo session business error error_code=%s error_type=%s",
            exc.error_code,
            type(exc).__name__,
        )
        return segments(exc.get_user_message())


async def _handle_active_session(
    user_id: str,
    raw_message: str,
    context: PluginContextProtocol,
    session: SessionData,
    group_id: int | None = None,
) -> list[dict[str, Any]]:
    """处理活跃会话的消息

    重构后：使用commands.session模块

    Args:
        user_id: 用户ID
        raw_message: 原始消息
        context: 上下文对象
        session: Dispatcher 事务传入的隔离会话副本
        group_id: 群组ID（可选）

    Returns:
        消息列表
    """
    if session.plugin_name != "pendo":
        raise RuntimeError("Pendo received a session owned by another plugin")

    # 检查是否是退出命令
    raw_message = str(raw_message or "")
    if raw_message.strip() in PendoConfig.SESSION_EXIT_COMMANDS:
        await safe_end_session(context)
        return segments("✅ 已退出当前会话")

    # 确保 services 已初始化（session 分支不经过 _build_command_router）
    _get_services(context)

    # 将消息传递给会话处理器（使用commands模块）
    result = await handle_session_message(user_id, raw_message, session, context)

    if result and result.get("status") != "error":
        return _format_result(result)
    else:
        # 会话处理失败，清除会话
        await safe_end_session(context)
        if result and result.get("message"):
            return segments(result["message"])
        return []


async def _handle_command_routing(
    user_id: str, args: str, context, group_id: int | None = None, log=None
) -> list[dict[str, Any]]:
    """处理命令路由

    Args:
        user_id: 用户ID
        args: 命令参数
        context: 上下文对象
        group_id: 群组ID（可选）
        log: 日志记录器

    Returns:
        消息列表
    """
    log = log or logger

    router = _build_command_router(context, group_id)

    catalog_root = getattr(router, "root", None) or _catalog_root(context)
    invocation = resolve_context_command_invocation(context, catalog_root.code, args)
    if invocation is None:
        invocation = resolve_catalog_invocation(catalog_root, args)
    if len(invocation.chain) > 1:
        subcommand = invocation.chain[1].name
        rest_args = invocation.remainder_after(1)
    else:
        subcommand, rest_args = _split_subcommand_preserve_rest(args)
    if not subcommand:
        return segments(router.get_help_message())

    # 使用 CommandRouter 路由子命令

    start_time = time.perf_counter()
    is_error = False
    try:
        result = await router.route(subcommand, user_id, rest_args, context)
        if isinstance(result, dict) and result.get("status") == "error":
            is_error = True
    except Exception:
        is_error = True
        raise
    finally:
        cmd_name = f"subcommand.{router.alias_map.get(subcommand.lower(), subcommand)}"
        await _record_metric(context, cmd_name, time.perf_counter() - start_time, is_error=is_error)

    return _format_result(result)


def _split_subcommand_preserve_rest(args: str) -> tuple[str, str]:
    """拆分一级子命令，同时保留剩余参数中的原始换行。"""
    raw = args or ""
    stripped = raw.strip()
    if not stripped:
        return "", ""

    parts = stripped.split(maxsplit=1)
    subcommand = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    return subcommand, rest


async def scheduled(context) -> list[dict[str, Any]]:
    """定时任务入口 - 每分钟执行一次，只处理提醒

    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)

    tasks = [
        ("reminders", lambda: check_reminders(context)),
    ]

    messages = []
    for task_name, task_func in tasks:
        result = await _run_scheduled_task(context, task_name, task_func, log)
        messages.extend(result)

    return messages


async def scheduled_daily_briefing(context) -> list[dict[str, Any]]:
    """每日简报定时任务 - 每分钟检查用户本地时间

    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    db = _get_database(context)

    result = await _run_scheduled_task(
        context, "daily_briefings", lambda: send_daily_briefings(context, db), log
    )
    return result


async def scheduled_diary_reminder(context) -> list[dict[str, Any]]:
    """日记提醒定时任务 - 每分钟检查用户本地时间

    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    db = _get_database(context)

    result = await _run_scheduled_task(
        context, "diary_reminders", lambda: check_diary_reminders(context, db), log
    )
    return result


async def scheduled_migrate_todos(context) -> list[dict[str, Any]]:
    """待办迁移定时任务 - 每天00:05执行

    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    db = _get_database(context)

    result = await _run_scheduled_task(
        context, "migrate_todos", lambda: migrate_undone_todos(context, db), log
    )
    return result


async def scheduled_prune_operation_logs(context) -> None:
    """Daily operation-log privacy retention task."""
    log = _get_logger(context)
    db = _get_database(context)
    await _run_scheduled_task(
        context, "prune_operation_logs", lambda: prune_operation_logs(context, db), log
    )


async def scheduled_weekly_finance_summary(context) -> list[dict[str, Any]]:
    """每周财务总结定时任务。"""
    log = _get_logger(context)
    db = _get_database(context)

    result = await _run_scheduled_task(
        context,
        "weekly_finance_summary",
        lambda: send_weekly_finance_summaries(context, db),
        log,
    )
    return result


async def scheduled_month_end_finance_summary(context) -> list[dict[str, Any]]:
    """月底财务总结定时任务。"""
    log = _get_logger(context)
    db = _get_database(context)

    result = await _run_scheduled_task(
        context,
        "month_end_finance_summary",
        lambda: send_month_end_finance_summaries(context, db),
        log,
    )
    return result


async def scheduled_cleanup_demo_data(context) -> None:
    """Pendo Web demo 数据清理定时任务。"""
    log = _get_logger(context)
    db = _get_database(context)

    await _run_scheduled_task(
        context,
        "cleanup_demo_data",
        lambda: cleanup_expired_demo_data(context, db),
        log,
    )


async def _run_scheduled_task(
    context,
    task_name: str,
    task_func: Callable[[], Any],
    log: logging.Logger,
) -> list[dict[str, Any]]:
    """运行单个定时任务并记录指标

    统一的任务执行模式，包含：
    - 性能监控
    - 异常处理
    - 指标记录

    Args:
        context: 上下文对象
        task_name: 任务名称
        task_func: 任务函数
        log: 日志记录器

    Returns:
        任务产生的消息列表
    """
    start = time.perf_counter()
    try:
        result = await task_func()
        await _record_metric(context, f"scheduled.{task_name}", time.perf_counter() - start)
        return result if result else []
    except asyncio.CancelledError:
        await _record_metric(context, f"scheduled.{task_name}", time.perf_counter() - start)
        log.info("Scheduled task '%s' cancelled during shutdown", task_name)
        return []
    except Exception as exc:
        await _record_metric(
            context, f"scheduled.{task_name}", time.perf_counter() - start, is_error=True
        )
        public_error_message(
            context,
            exc,
            logger=log,
            component=f"pendo.scheduled.{task_name}",
        )
        return []


def _format_result(result: Any) -> list[dict[str, Any]]:
    """把命令层返回值统一转换为消息段。"""

    if isinstance(result, dict):
        message = result.get("message", "")
    else:
        message = str(result)
    return segments(message)


def _success_message(message: str) -> dict[str, Any]:
    """收窄跨模块成功响应的动态类型，供本入口的命令闭包复用。"""

    return cast(dict[str, Any], success_result(message))


# ============================================================
# 命令处理
# ============================================================


@lru_cache(maxsize=1)
def _local_catalog_root() -> CommandCatalogNode:
    """直接单测没有 Core 上下文时，仍读取生产 manifest 的同一命令目录。"""

    manifest_path = Path(__file__).with_name("plugin.json")
    manifest = PluginManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    command = next(item for item in manifest.commands if item.name == "pendo")
    return build_command_catalog_node(
        manifest.name,
        command.model_dump(),
        root=True,
    )


def _catalog_root(context: Any) -> CommandCatalogNode:
    """生产使用 Core 原子快照；无上下文的直接测试才回退到本地 manifest。"""

    return get_context_command_root(context, "pendo.pendo") or _local_catalog_root()


def _build_command_router(context, group_id: int | None = None) -> CommandRouter:
    """构建命令路由器

    注意：不再按 group_id 做全局缓存，避免闭包捕获过期上下文。
    """

    services = _get_services(context)
    db = services["db"]
    reminder_service = services["reminder_service"]
    exporter = services["exporter"]
    event_handler = services["event_handler"]
    task_handler = services["task_handler"]
    note_handler = services["note_handler"]
    diary_handler = services["diary_handler"]
    search_handler = services["search_handler"]
    ledger_handler = services["ledger_handler"]
    web_handler = services["web_handler"]

    async def _export_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        if not args or not args.strip():
            return _success_message(_show_help("export"))

        result = cast(
            dict[str, Any],
            await run_sync(exporter.export_markdown, user_id, args, {}),
        )
        if result.get("status") != "success":
            return result

        file_path = result.get("file_path")
        file_name = result.get("file_name")
        if not file_path or not file_name:
            return result

        if ctx is None or not hasattr(ctx, "send_action"):
            result["message"] = (
                f"{result.get('message', '导出完成')}\n文件已保存在本地: {file_path}"
            )
            return result

        try:
            target_user: int | str = int(user_id)
        except (TypeError, ValueError):
            target_user = user_id

        outcome = await ctx.send_action(
            {
                "action": "upload_private_file",
                "params": {
                    "user_id": target_user,
                    "file": file_path,
                    "name": file_name,
                },
            }
        )

        if outcome is True:
            delivery_message = "已通过 QQ 私聊文件发送给你"
        elif outcome is None:
            delivery_message = "已尝试通过 QQ 私聊发送文件，但未收到最终回执"
        else:
            delivery_message = "QQ 私聊文件发送失败；导出文件仍保留在服务器"
        result["message"] = f"{result.get('message', '导出完成')}\n{delivery_message}"
        return result

    async def _search_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        if not args or not args.strip():
            return _success_message(_show_help("search"))
        return cast(dict[str, Any], await search_handler.search(user_id, args, ctx))

    async def _import_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        requested = args.strip()
        if requested.lower() in {"help", "h", "?"}:
            return _success_message(_show_help("import"))
        if requested:
            return {
                "status": "error",
                "message": "❌ /pendo import 不接收文件路径或其他参数\n用法: /pendo import",
            }
        runtime = PendoConfig.runtime()
        return _success_message(
            "\n".join(
                [
                    "📥 **导入 (Import)**",
                    "",
                    "命令聊天入口不能安全上传 `.pendo.zip` 文件；请使用 Web 数据迁移页完成预检和导入。",
                    "",
                    "1. 发送 `/pendo web token` 获取一次性登录 Code",
                    f"2. 打开 Web UI: http://{runtime.web_host}:{runtime.web_port}/#/transfer",
                    "3. 在“导入”页上传 `.pendo.zip`，先预检，再执行导入",
                    "",
                    "聊天命令不接收本地文件路径，避免误读服务器文件或路径穿越风险。",
                    "支持类型: event、task/todo、note、diary、ledger，以及多节点/重复日程的 event_collection。",
                    "冲突策略: 跳过同 ID、覆盖同 ID、生成副本；重复 bundle 默认阻止二次导入，可在 Web 勾选强制重新导入。",
                ]
            )
        )

    async def _settings_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        message = await handle_settings(user_id, args, db)
        return _success_message(message)

    async def _confirm_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        # confirm 返回结构化状态，直接透传，不能从展示文本前缀推断成功与否。
        return cast(
            dict[str, Any],
            await handle_confirm(user_id, args, reminder_service, db),
        )

    async def _snooze_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await handle_snooze(user_id, args, reminder_service),
        )

    async def _undo_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await handle_undo(user_id, args, db))

    def _help_or_exec(handler_method, help_key):
        """Helper to return help if args is empty, otherwise execute handler"""

        async def _wrapper(user_id, args, ctx):
            if not args or not args.strip():
                return _success_message(_show_help(help_key))
            return await handler_method(user_id, args, ctx, group_id=group_id)

        return _wrapper

    handlers = {
        "event": _help_or_exec(event_handler.handle, "event"),
        "todo": _help_or_exec(task_handler.handle, "todo"),
        "note": _help_or_exec(note_handler.handle, "note"),
        "diary": _help_or_exec(diary_handler.handle, "diary"),
        "search": _search_cmd,
        "ledger": _help_or_exec(ledger_handler.handle, "ledger"),
        "export": _export_cmd,
        "import": _import_cmd,
        "settings": _settings_cmd,
        "confirm": _confirm_cmd,
        "snooze": _snooze_cmd,
        "undo": _undo_cmd,
        "web": _help_or_exec(web_handler.handle, "web"),
    }

    router = CommandRouter(_catalog_root(context), handlers, help_provider=_show_help)
    return router


# ============================================================
# 帮助信息定义
# ============================================================

HELP_MAP = {
    "header": "🗓️ **Pendo · 个人时间与信息管理中枢**",
    "quick": [
        "⚡ **快速记录**",
        "• /pendo event add <内容> - 添加单次/重复/多节点日程(AI解析)",
        "  - 例: /pendo event add 明天9点组会，提前30分钟提醒",
        "• /pendo todo add - 交互式添加待办(内容 + 计划日期)",
        "• /pendo todo add <内容> - 快捷添加待办(默认计划到今天)",
        "  - 例: /pendo todo add 写周报 cat:工作 p:2 plan:2026-05-01",
        "• /pendo todo view <id> - 查看待办详情",
        "• /pendo note add <内容> - 记录笔记(支持 title:/cat:/#标签/ref:ID)",
        "  - 例: /pendo note add title:读书摘录 content 费曼技巧 cat:学习 #方法",
        "• /pendo diary add <内容> - 写一篇日记(同一天可多篇)",
        "  - 例: /pendo diary add 今天跑步5公里 mood:happy score:8",
        "• /pendo ledger quick <金额> <描述> - 快速记账",
        "  - 例: /pendo ledger quick 35.5 午饭 cat:餐饮 account:微信",
    ],
    "event": [
        "🗓️ **日程管理 (Event)**",
        "  - 别名: `e`, `calendar`, `日程`, `事件`",
        "• /pendo event add <内容> - 添加日程(AI解析单次/重复/多节点)",
        "  - 例: /pendo event add 3月8日下午两点，国自然截止，提前一周和一天提醒",
        "  - 例: /pendo event add 每月18号上午十点，公积金提取，重复7个月",
        "  - 例: /pendo event add 5月1日9点出发、14点入住、5月3日18点返程，杭州团建，提前2小时提醒",
        "  - 多节点事件会生成一个日程集合和多个可单独查看/编辑/删除的节点",
        "• /pendo event view <id> - 查看日程详情；集合ID显示整体，节点ID显示单个节点",
        "• /pendo event list [范围] [cat:分类] [#标签] - 查看日程",
        "  - 范围: today, tomorrow, week(本周), month(本月), year(今年), YYYY-MM-DD, YYYY-MM, last7d/last30d, start..end",
        "  - 例: /pendo event list week cat:工作",
        "  - 例: /pendo event list 2026-05-01..2026-05-07 #会议",
        "• /pendo event delete <id> - 删除单个日程/节点；传集合ID会删除整组",
        "• /pendo event edit <id> <内容> - 编辑日程",
        "  - 节点ID可像单次日程一样改标题、时间、地点、备注；提醒建议用 reminders set/delete",
        "  - 多节点/重复日程先用 view 集合ID 查看节点ID，再编辑具体节点",
        "  - 集合ID只编辑整体标题、分类、地点、备注，不修改某个节点时间",
        "  - 例: /pendo event edit 80efbef6_m03 改到4月22日12:43",
        "  - 例: /pendo event edit 80efbef6_m03 备注为从北京南坐G123去会场",
        "  - 例: /pendo event edit 80efbef6_m03 地点改到北京南",
        "  - 例: /pendo event edit 80efbef6 标题改为FAST会议行程",
        "• /pendo event reminders [id|范围] - 查看提醒",
        "• /pendo event reminders list [范围] - 按范围查看提醒",
        "  - id 可为单次日程、重复/多节点集合ID，或单个节点ID",
        "• /pendo event reminders set <id> <描述> - 重置单个日程/节点或整组集合的提醒",
        "• /pendo event reminders delete <id> <all|today|future|提醒时间> - 删除某个或某些提醒",
        "• /pendo event reminders confirm <id> [today|future|all|提醒时间] - 提前确认某个或某些提醒",
        "  - 提醒时间支持 YYYY-MM-DD HH:MM、MM-DD HH:MM、M月D日 HH:MM",
        "  - 例: /pendo event reminders list week",
        "  - 例: /pendo event reminders set abc12345 提前1天和2小时提醒",
        "  - 例: /pendo event reminders delete abc12345 2030-06-01 09:00",
        "  - 例: /pendo event reminders delete abc12345 all",
        "  - 例: /pendo event reminders confirm abc12345 today",
    ],
    "todo": [
        "✅ **待办事项 (Todo)**",
        "  - 别名: `task`, `t`, `待办`, `任务`",
        "• /pendo todo add - 交互式添加待办，只询问内容和计划日期",
        "• /pendo todo add <内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [remind:YYYY-MM-DDTHH:MM[,YYYY-MM-DDTHH:MM]] [cat:分类] [p:1-5] [#标签] - 快捷添加待办，可带高级参数",
        "  - 默认计划到今天，晚上8点后自动计划到明天",
        "  - cat: 只表示文字分类，不再作为日期桶",
        "  - p:1(紧急) p:2(高) p:3(中) p:4(低) p:5(最低)",
        "  - 例: /pendo todo add 写项目周报 cat:工作 p:2 plan:2026-05-01 deadline:2026-05-01T18:00 #周报",
        "  - 例: /pendo todo add 交材料 remind:2026-05-01T09:00,2026-05-01T17:00",
        "• /pendo todo view <id> - 查看待办详情",
        "• /pendo todo list [范围|状态|分类|cat:分类|#标签] [open|done|cancelled] [p:1-5] [all|page:n] - 查看待办",
        "  - 范围: today, tomorrow, week(本周), month(本月), year(今年), YYYY-MM-DD, YYYY-MM, last7d/last30d, start..end",
        "  - /pendo todo list today - 今日待办",
        "  - /pendo todo list 2026-05-01 - 指定日期待办",
        "  - /pendo todo list cat:工作 #周报 - 工作分类下的周报标签待办",
        "  - /pendo todo list 工作 done - 工作分类已完成",
        "  - /pendo todo list 工作 p:1 - 工作分类紧急待办",
        "  - /pendo todo list cancelled - 所有分类已取消",
        "  - /pendo todo list done all - 所有分类已完成(全部)",
        "  - /pendo todo list 工作 page:2 - 工作分类第2页",
        "• /pendo todo done <id> - 完成待办",
        "• /pendo todo cancel <id> - 取消待办",
        "• /pendo todo undone <id> - 重开待办",
        "• /pendo todo delete <id|cat:分类> - 删除待办",
        "  - 例: /pendo todo delete cat:临时",
        "• /pendo todo edit <id> <内容> [plan:/deadline:/remind:/cat:/p:/#标签] - 编辑待办",
        "  - 例: /pendo todo edit abc12345 标题改为写项目周报",
        "  - 例: /pendo todo edit abc12345 deadline:2026-05-01T24:00",
    ],
    "note": [
        "📝 **笔记 (Note)**",
        "  - 别名: `n`, `idea`, `笔记`, `想法`, `灵感`",
        "• /pendo note add <内容> [cat:分类] [#标签] [ref:条目ID] - 记录笔记",
        "  - 例: /pendo note add 直接折叠找脉冲星 cat:工作 #文章",
        "• /pendo note add title:<标题> content <正文> [cat:分类] [#标签] - 指定标题和正文",
        "• /pendo note add title:<标题> 后回车输入正文，再写 cat:/#标签 - 标题后直接换行写正文",
        "  - 例: /pendo note add title:我的标题 content 这里是详细正文 cat:工作 #学习",
        "  - 多行示例: /pendo note add title:会议纪要",
        "    1. 事项A",
        "    2. 事项B",
        "    cat:其他 #记录",
        "  - 例: /pendo note add 这条关联到日程 ref:80efbef6 cat:工作 #关联",
        "  - ref:条目ID 会保存为结构化 references，支持关联日程/待办/笔记/日记/账目",
        "• /pendo note list [范围|since:范围] [分类名|cat:分类] [#标签] [all|page:n] - 查看笔记",
        "  - /pendo note list - 显示所有分类概览",
        "  - 范围: today, week(本周), month(本月), year(今年), YYYY-MM, last7d/last30d, start..end",
        '  - /pendo note list 工作 - 查看"工作"分类(直接用分类名)',
        "  - /pendo note list month cat:密钥 #rustdesk - 查看本月密钥分类下的 RustDesk 笔记",
        "  - /pendo note list cat:工作 #文章 since:last30d - 查看最近30天工作文章",
        '  - /pendo note list 工作 all - 显示"工作"分类全部笔记',
        '  - /pendo note list 工作 page:2 - 显示"工作"分类第2页',
        "• /pendo note view <id> - 查看笔记详情",
        "• /pendo note edit <id> <新内容> [cat:分类] [#标签] - 编辑笔记",
        "  - 也支持 title:xxx content yyy 或 title:xxx 后换行正文 的方式重命名和修改大段内容",
        "  - 例: /pendo note edit abc12345 title:新标题 content 新正文 cat:工作 #复盘",
        "• /pendo note append <id> <追加内容> - 追加内容",
        "  - 例: /pendo note append abc12345 补充一条结论",
        "• /pendo note tag <id> #标签 - 添加标签，可一次写多个",
        "  - 例: /pendo note tag abc12345 #论文 #想法",
        "• /pendo note untag <id> #标签 - 移除标签，可一次写多个",
        "  - 例: /pendo note untag abc12345 #想法",
        "• /pendo note link <id> <关联条目ID> - 关联日程/待办/笔记等条目",
        "  - 例: /pendo note link abc12345 80efbef6",
        "• /pendo note delete <id|cat:分类> - 删除笔记",
        "  - 例: /pendo note delete cat:临时",
    ],
    "diary": [
        "📔 **日记 (Diary)**",
        "  - 别名: `d`, `journal`, `日记`",
        "• /pendo diary add [日期] <内容> [weather:xxx] [location:xxx] [mood:happy] [score:1-10] [tags:a,b] [favorite:true]",
        "  - 无日期则写今天；同一天可写多篇独立条目，按记录时间排序",
        "  - 心情可用: happy/calm/excited/sad/angry/tired/anxious/grateful/neutral",
        "  - weather/location 可加引号写空格；favorite 也可写 fav",
        "  - 例: /pendo diary add 2026-05-01 今天跑步5公里 weather:晴 location:操场 mood:happy score:8 tags:运动,复盘 favorite:true",
        "• /pendo diary template [编号|名称] - 模板引导写日记",
        "  - 1.三件好事 2.今日总结 3.情绪记录",
        "• /pendo diary list [范围] [mood:情绪] [cat:分类] [#标签] - 查看日记列表(默认本月)",
        "  - 范围: today, tomorrow, week(本周), month(本月), year(今年), YYYY-MM, last7d/last30d, start..end",
        "  - 例: /pendo diary list 2026-05 mood:happy",
        "  - 例: /pendo diary list 2026-05 cat:日记 #复盘",
        "• /pendo diary view [日期|ID] - 日期查看当天所有条目，ID查看单篇",
        "• /pendo diary delete <日期|ID> - 当天多篇时需按ID删除，避免误删",
        "  - 例: /pendo diary view 2026-05-01",
        "  - 例: /pendo diary delete abc12345",
    ],
    "ledger": [
        "💰 **记账 (Ledger)**",
        "  - 别名: `bill`, `finance`, `记账`, `账单`",
        "• /pendo ledger add - 交互式记账；金额和描述手动输入，类型/账户/分类可选编号",
        "• /pendo ledger add <金额> <描述> [cat:分类] [in|out|transfer|type:expense/income/transfer] [account:账户] [to:账户] [merchant:商户] [date:YYYY-MM-DD] [remark:备注] - 快捷记账",
        "• /pendo ledger quick <金额> <描述> [cat:分类] [in|out|transfer|type:expense/income/transfer] [account:账户] [to:账户] [merchant:商户] [date:YYYY-MM-DD] [remark:备注] - 快速记账",
        "  - 默认支出；in 标记收入，out/expense 标记支出，transfer 标记账户间转账",
        "  - account/from 表示账户，to/counter 表示转入账户，merchant/payee 表示商户",
        '  - 带空格的值可加英文双引号，例如 merchant:"星巴克 人民广场店"',
        "  - 例: /pendo ledger quick 35.5 午饭 cat:餐饮 account:微信 merchant:食堂",
        "  - 例: /pendo ledger quick 5000 工资 cat:工资 in account:招行",
        "  - 例: /pendo ledger quick 1000 还款 transfer account:微信 to:招行 date:2026-05-01",
        "• /pendo ledger list [范围] [筛选] - 查看账目",
        "  - 范围: today, week(本周), month(本月), year(今年), YYYY-MM, last7d/last30d, start..end",
        "  - 筛选: type:expense/income/transfer account:账户 to:账户 merchant:商户 cat:分类 amount:N或N..M ex all page:N",
        "  - 例: /pendo ledger list 2026-03 type:expense cat:餐饮 amount:20..100 ex",
        "  - 例: /pendo ledger list month account:微信 page:2",
        "• /pendo ledger view <id> - 查看账目详情",
        "• /pendo ledger edit <id> <字段:值> ... - 编辑账目",
        "  - 字段: amount: title: cat/category: type: account/from: to/counter: merchant/payee: date: remark:",
        "  - 例: /pendo ledger edit abc123 amount:50 cat:交通 account:微信",
        "• /pendo ledger delete <id> - 删除账目",
        "• /pendo ledger summary [范围] - 收支汇总统计",
        "  - 例: /pendo ledger summary 2026-03",
    ],
    "search": [
        "🔎 **搜索 (Search)**",
        "• /pendo search <关键词> [#标签] - 全文搜索(标题/内容/备注/分类)",
        "• 筛选: type=event/task/note/diary/ledger range=today/week(本周)/month(本月)/year(今年)/last7d/last30d/YYYY-MM/start..end",
        "• 待办筛选: status=open/done/cancelled",
        "• 记账筛选: transaction_type=income/expense/transfer account=<账户> merchant=<商户>",
        "• 通用筛选: category=<分类> tag=<标签>；type=ledger 时 category 按账目分类筛选",
        "  - 例: /pendo search 组会 type=event range=last30d",
        "  - 例: /pendo search 午饭 type=ledger transaction_type=expense account=微信",
        "  - 例: /pendo search 周报 type=task status=open category=工作",
        "  - 例: /pendo search 周报 #复盘",
    ],
    "reminder": [
        "⏰ **提醒操作**",
        "• /pendo confirm <id> - 确认刚收到的那条提醒",
        "• /pendo snooze <id> <时间> - 延后刚收到的提醒",
        "  - 时间格式: 10m, 10min, 1h, 1d, 19:00",
        "  - 例: /pendo confirm abc12345",
        "  - 例: /pendo snooze abc12345 10m",
        "• 管理未来提醒: /pendo event reminders set/delete/confirm <id> ...",
    ],
    "export": [
        "📤 **导出 (Export)**",
        "• /pendo export <文件名> [范围] [类型] - 导出 Markdown 并私聊发送文件",
        "  - 范围: all, today, tomorrow, week(本周), month(本月), year(今年), YYYY, YYYY-MM, YYYY-MM-DD, last7d/last30d, start..end",
        "  - 类型: all, event, todo/task, note, ledger, diary，可用逗号组合",
        "  - 文件名会自动加 .md；含空格请加英文引号",
        "  - 例: /pendo export 我的档案",
        "  - 例: /pendo export 工作回顾 last30d event,todo",
        '  - 例: /pendo export "三月 账本" 2026-03 ledger',
        "  - 例: /pendo export 账本快照 2026-03 ledger",
    ],
    "import": [
        "📥 **导入 (Import)**",
        "• /pendo import - 查看 Web 导入入口和支持格式",
        "  - 聊天命令不接收本地文件路径，避免误读服务器文件或路径穿越风险",
        "  - 请使用 Web 数据迁移页上传 .pendo.zip，页面会先做预检再写入数据库",
        "  - 支持类型: event、task/todo、note、diary、ledger、event_collection",
        "  - 支持策略: 跳过同 ID、覆盖同 ID、生成副本；重复 bundle 默认阻止二次导入",
        "  - 例: /pendo import",
        "  - 例: /pendo web token",
    ],
    "settings": [
        "⚙️ **设置 (Settings)**",
        *PLUGIN_SETTINGS_HELP_LINES,
    ],
    "web": [
        "🌐 **Web UI 管理 (Web)**",
        "• /pendo web token  - 生成一次性登录 Code（Code 单独发送）",
        "• /pendo web widget-token - 生成 Scriptable 小组件令牌",
        "• /pendo web start  - 启动 Web 服务",
        "• /pendo web stop   - 停止 Web 服务",
        "• /pendo web status - 查看服务状态",
        "  - 例: /pendo web status",
    ],
    "common": [
        "↩️ **其他操作**",
        f"• /pendo undo [分钟] - 撤销最近一次删除或编辑 (最多{PendoConfig.UNDO_WINDOW_MINUTES}分钟)",
        "  - 例: /pendo undo",
        "  - 例: /pendo undo 3",
    ],
}


def _render_help_section(key: str) -> list[str]:
    section = HELP_MAP[key]
    if not isinstance(section, list) or not section:
        return []

    title = str(section[0]).rstrip(":")
    body = [str(line) for line in section[1:]]
    return [f"━━ {title}", *body]


def _show_help_overview() -> str:
    lines = [str(HELP_MAP["header"]), "", "🧭 **可用命令**"]
    lines.extend(
        f"• /pendo {command.name} - {command.help_text}"
        for command in _local_catalog_root().children
    )
    lines.extend(
        [
            "",
            "💡 查看详细用法：`/pendo help <命令>`；完整命令码：`/help pendo`",
            "例: `/pendo event`、`/pendo todo`、`/pendo export`",
        ]
    )
    return "\n".join(lines)


def _show_help(subcommand: str = "") -> str:
    """显示帮助信息

    Args:
        subcommand: 子命令名称，如果提供则显示特定部分的帮助
    """
    subcommand = subcommand.lower().strip() if subcommand else ""

    # 别名映射
    aliases = {
        "task": "todo",
        "calendar": "event",
        "idea": "note",
        "journal": "diary",
        "config": "settings",
        "bill": "ledger",
        "finance": "ledger",
        "confirm": "reminder",
        "snooze": "reminder",
        "undo": "common",
    }
    target_key = aliases.get(subcommand, subcommand)

    # 如果请求特定部分的帮助
    if target_key in HELP_MAP:
        parts: list[str] = [
            str(HELP_MAP["header"]),
            "",
            "🧭 输入 `/pendo` 查看完整总览，输入 `/pendo help <模块>` 查看对应帮助",
            "",
        ]
        parts.extend(_render_help_section(target_key))
        return "\n".join(parts)

    if subcommand:
        return f"❌ 未知命令: {subcommand}\n\n使用 /pendo help 查看所有命令"

    return _show_help_overview()


def _get_logger(context: PluginContextProtocol | None) -> logging.Logger:
    if context is not None:
        return cast(logging.Logger, context.logger)
    return logger


async def _record_metric(
    context: PluginContextProtocol | None,
    name: str,
    duration: float,
    is_error: bool = False,
) -> None:
    metrics = getattr(context, "metrics", None) if context is not None else None
    if metrics is not None:
        await metrics.record_plugin_execution("pendo", name, duration, is_error=is_error)


def _get_database(context: PluginContextProtocol | None) -> Database:
    """获取数据库实例（使用共享函数）"""
    from .utils.db_ops import get_database

    return get_database(context)


def _get_services(context: PendoContext) -> PendoServices:
    """获取共享服务实例（绑定到 PluginContext.state）。"""
    cached_services = get_cached_services(context)
    if cached_services is not None:
        return cached_services

    db = _get_database(context)
    ai_parser = AIParser(context)
    ai_parser.db = db
    reminder_service = ReminderService(db)
    exporter = ExporterService(db, Path(context.data_dir) / "exports")

    # Event/Diary 需要 AI 能力
    event_handler = EventHandler(db, ai_parser, reminder_service)
    task_handler = TaskHandler(db)
    note_handler = NoteHandler(db)
    diary_handler = DiaryHandler(db, ai_parser=ai_parser)
    search_handler = SearchHandler(db)
    ledger_handler = LedgerHandler(db)
    web_handler = WebHandler(db)

    services: PendoServices = {
        "db": db,
        "ai_parser": ai_parser,
        "reminder_service": reminder_service,
        "exporter": exporter,
        "event_handler": event_handler,
        "task_handler": task_handler,
        "note_handler": note_handler,
        "diary_handler": diary_handler,
        "search_handler": search_handler,
        "ledger_handler": ledger_handler,
        "web_handler": web_handler,
    }

    get_plugin_runtime_state(context)["services"] = services

    return services
