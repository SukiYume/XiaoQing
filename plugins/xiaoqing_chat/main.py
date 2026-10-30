"""拟人智能对话插件的生命周期、命令路由与 Core 观察入口。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from core.interfaces import COMMAND_RUNNER_EVENT_KEY
from core.plugin_base import segments
from core.public_errors import public_error_message, public_error_response
from core.router import (
    CommandCatalogNode,
    CommandInvocation,
    format_command_catalog,
    get_context_command_root,
    resolve_context_command_invocation,
)

from .chat_runtime import configured_bot_name
from .commands import (
    handle_config,
    handle_expression,
    handle_internal,
    handle_jargon,
    handle_memory,
    handle_provider,
    handle_review,
)
from .handler_context import validate_action_list
from .handlers import (  # noqa: F401 - Core 按名称调用这些插件入口
    call_bot_name_only,
    handle_smalltalk,
    observe_message,
    observe_outgoing_action,
)
from .runtime_state import get_state as _state

logger = logging.getLogger(__name__)

CommandResponse = list[dict[str, Any]]
CommandHandler  = Callable[[str, dict[str, Any], Any], Awaitable[CommandResponse]]


# 子命令只在这里绑定业务处理器；名称、别名、权限和帮助仍以 manifest 目录为准。
_HANDLERS: dict[str, CommandHandler] = {
    "reset"     : lambda rest, ev, ctx: handle_internal("重置", rest, ev, ctx),
    "stats"     : lambda rest, ev, ctx: handle_internal("统计", rest, ev, ctx),
    "brain"     : lambda rest, ev, ctx: handle_internal("深度对话", rest, ev, ctx),
    "config"    : handle_config,
    "memory"    : handle_memory,
    "expression": handle_expression,
    "jargon"    : handle_jargon,
    "model"     : handle_provider,
    "review"    : handle_review,
}


def _catalog_root(context: Any) -> CommandCatalogNode | None:
    """优先复用 Dispatcher 注入的快照，直接调用时再查同一 Core 目录。"""

    return get_context_command_root(context, "xiaoqing_chat.xc")


def _resolve_invocation(args: str, context: Any) -> CommandInvocation | None:
    return resolve_context_command_invocation(context, "xiaoqing_chat.xc", args)


def _help_text(context: Any) -> str:
    root  = _catalog_root(context)
    title = f"💬 {configured_bot_name(context)}智能对话"
    if root is None:
        return f"{title}\n\n完整命令目录暂不可用，请使用 /help xiaoqing_chat"
    return format_command_catalog(root, title=f"{title} · 完整命令目录")


async def init(context=None) -> None:
    """启动后台任务接收，并绑定、加载需要持久化的插件存储。"""
    state = _state()
    state.start_accepting_background_tasks()
    if context is not None:
        from .media.loading import run_media_blocking
        from .store_binding import bind_all_stores

        bind_all_stores(state, context.data_dir)
        await run_media_blocking(state.media_store.load)
        log = context.logger
        log.info("XiaoQing Chat plugin initialized")


def _route_subcommand(args: str, raw_args: str, context) -> tuple[str, str] | str:
    """解析 ``(子命令, 剩余参数)``；子命令为空表示当作聊天内容，参数错误时返回提示文本。"""
    raw_parts = raw_args.split(maxsplit=1)
    catalog_root = _catalog_root(context)
    first_node   = catalog_root.resolve_child(raw_parts[0]) if catalog_root else None
    if len(raw_parts) > 1 and (
        (first_node is not None and first_node.name == "help")
        or raw_parts[0].casefold() in {"help", "帮助", "?"}
    ):
        usage = first_node.usage if first_node is not None else "/xc help"
        return f"❌ help 子命令不接受额外参数\n用法: {usage}"

    invocation = _resolve_invocation(args, context)
    if invocation is None:
        candidate = raw_parts[0].casefold()
        action    = candidate if candidate in {*_HANDLERS, "help"} else ""
        return action, raw_parts[1] if len(raw_parts) > 1 else ""
    if len(invocation.chain) > 1:
        return invocation.chain[1].name, invocation.remainder_after(1)
    candidate_node = invocation.root.resolve_child(raw_parts[0])
    if (
        candidate_node is not None
        and candidate_node.name != "help"
        and candidate_node.match_mode == "exact"
        and len(raw_parts) > 1
    ):
        return f"❌ 用法: {candidate_node.usage}"
    return "", ""


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context,
) -> list[dict[str, Any]]:
    """命令处理入口；``command`` 由插件协议传入，实际子命令从 ``args`` 解析。"""
    del command  # 保留框架按关键字调用的公开参数名；子命令事实源是 Core 解析后的 args。
    try:
        raw_args = str(args or "").strip()
        if not raw_args:
            return segments(_help_text(context))
        routed = _route_subcommand(args, raw_args, context)
        if isinstance(routed, str):
            return segments(routed)
        action, rest = routed
        if action == "help":
            return segments(_help_text(context))

        handler = _HANDLERS.get(action)
        if handler is not None:
            return validate_action_list(
                await handler(rest, event, context), source=f"command:{action}"
            )

        # 未匹配子命令 → 当作聊天内容（使用 args 而非 raw_message，避免带上 /xc 前缀）
        text = raw_args
        if not text:
            return []
        if event.get(COMMAND_RUNNER_EVENT_KEY):
            # 插件调用命令不是有人在说话：不能写进聊天记忆，也不能再转回 agent 形成循环。
            return segments("/xc 的聊天入口只接受真人消息，不能作为工具调用。")
        # 显式 /xc 命令 → 标记强制回复，跳过概率判断
        event["_xc_command_forced"] = True
        return await handle_smalltalk(text, event, context)

    except Exception as exc:
        log = context.logger
        return public_error_response(
            context,
            exc,
            logger    = log,
            component = "xiaoqing_chat.handle",
        )


async def _flush_shutdown_state(context: Any, state: Any, log: logging.Logger) -> None:
    """把所有可能仍在防抖窗口内的数据完整落盘。"""
    flushers = (
        ("memory_store", state.memory_store.persist_all),
        ("pfc_state", state.pfc_state_store.flush),
        ("action_history", state.action_history.flush),
        ("media_store", state.media_store.flush),
    )
    for component, flush in flushers:
        try:
            await asyncio.to_thread(flush)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = log,
                component = f"xiaoqing_chat.shutdown.{component}",
            )

    try:
        if state.memory_db.is_dirty():
            await asyncio.to_thread(state.memory_db.save)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = log,
            component = "xiaoqing_chat.shutdown.memory_db",
        )


async def shutdown(context) -> None:
    """停止新后台工作，完整落盘，再取消后台任务并做最终落盘。"""

    log = context.logger
    log.info("XiaoQing Chat plugin shutting down")
    state = _state()
    state.stop_accepting_background_tasks()

    # Core 的总关闭预算由所有插件共享，防抖和后台学习直接进入取消收尾。
    pending = state.background_tasks()

    # 必须在取消任务前保存：防抖写入本身也可能位于 pending 集合中。
    await _flush_shutdown_state(context, state, log)
    if pending:
        log.warning("XiaoQing Chat: cancelling %d unfinished tasks", len(pending))
        for task in pending:
            task.cancel()
        _done, pending = await asyncio.wait(pending, timeout=2.0)
        if pending:
            # 清理任务仍持有旧模块状态时，生命周期调用必须向上报告失败。
            raise TimeoutError(f"XiaoQing Chat shutdown: {len(pending)} tasks still running")
        # 任务的 finally 块可能在取消过程中产生最后一批状态变更。
        await _flush_shutdown_state(context, state, log)

    log.info("XiaoQing Chat plugin shutdown complete")
