"""
小青智能对话插件
提供 AI 对话、上下文记忆、表达学习等高级功能
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.plugin_base import segments
from core.public_errors import public_error_message, public_error_response
from core.router import (
    CommandCatalogNode,
    format_command_catalog,
    get_context_command_root,
    resolve_context_command_invocation,
)

from .handlers import (
    call_bot_name_only_internal,
    handle_config,
    handle_expression,
    handle_internal,
    handle_jargon,
    handle_memory,
    handle_provider,
    handle_review,
    handle_smalltalk,
)
from .handlers import (
    observe_message as observe_message_internal,
)
from .handlers import (
    observe_outgoing_action as observe_outgoing_action_internal,
)
from .runtime_state import get_state as _state

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, Any] = {
    "reset": lambda rest, ev, ctx: handle_internal("重置", rest, ev, ctx),
    "stats": lambda rest, ev, ctx: handle_internal("统计", rest, ev, ctx),
    "brain": lambda rest, ev, ctx: handle_internal("深度对话", rest, ev, ctx),
    "config": handle_config,
    "memory": handle_memory,
    "expression": handle_expression,
    "jargon": handle_jargon,
    "model": handle_provider,
    "review": handle_review,
}


def _catalog_root(context: Any) -> CommandCatalogNode | None:
    """优先复用 Dispatcher 注入的快照，直接调用时再查同一 Core 目录。"""

    return get_context_command_root(context, "xiaoqing_chat.xc")


def _resolve_invocation(args: str, context: Any):
    return resolve_context_command_invocation(context, "xiaoqing_chat.xc", args)


def _help_text(context: Any) -> str:
    root = _catalog_root(context)
    if root is None:
        return "💬 小青智能对话\n\n完整命令目录暂不可用，请使用 /help xiaoqing_chat"
    return format_command_catalog(root, title="💬 小青智能对话 · 完整命令目录")


async def init(context=None) -> None:
    """插件初始化"""
    state = _state()
    state.start_accepting_background_tasks()
    if context is not None:
        from .media.event_media_common import _run_media_blocking
        from .store_binding import _bind_all_stores

        _bind_all_stores(state, context.data_dir)
        await _run_media_blocking(state.media_store.load)
        log = getattr(context, "logger", logger)
        log.info("XiaoQing Chat plugin initialized")


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context,
) -> list[dict[str, Any]]:
    """命令处理入口；``command`` 由插件协议传入，实际子命令从 ``args`` 解析。"""
    try:
        raw_args = str(args or "").strip()
        raw_parts = raw_args.split(maxsplit=1)
        catalog_root = _catalog_root(context)
        first_node = (
            catalog_root.resolve_child(raw_parts[0]) if raw_parts and catalog_root else None
        )
        first_is_help = bool(first_node is not None and first_node.name == "help") or bool(
            raw_parts and raw_parts[0].casefold() in {"help", "帮助", "?"}
        )
        if first_is_help and len(raw_parts) > 1:
            usage = first_node.usage if first_node is not None else "/xc help"
            return segments(f"❌ help 子命令不接受额外参数\n用法: {usage}")

        invocation = _resolve_invocation(args, context)
        root = invocation.root if invocation is not None else None
        if not raw_args:
            return segments(_help_text(context))

        action = (
            invocation.chain[1].name if invocation is not None and len(invocation.chain) > 1 else ""
        )
        rest = invocation.remainder_after(1) if invocation is not None and action else ""
        if not action and root is None:
            parts = raw_parts
            candidate = parts[0].casefold() if parts else ""
            action = candidate if candidate in {*_HANDLERS, "help"} else ""
            rest = parts[1] if len(parts) > 1 else ""

        if action == "help":
            return segments(_help_text(context))

        if not action and root is not None:
            parts = raw_parts
            candidate = root.resolve_child(parts[0]) if parts else None
            if (
                candidate is not None
                and candidate.name != "help"
                and candidate.match_mode == "exact"
                and len(parts) > 1
            ):
                return segments(f"❌ 用法: {candidate.usage}")

        handler = _HANDLERS.get(action)
        if handler:
            return await handler(rest, event, context)

        # 未匹配子命令 → 当作聊天内容（使用 args 而非 raw_message，避免带上 /xc 前缀）
        text = raw_args
        if not text:
            return []
        # 显式 /xc 命令 → 标记强制回复，跳过概率判断
        event["_xc_command_forced"] = True
        return await handle_smalltalk(text, event, context)

    except Exception as exc:
        log = getattr(context, "logger", logger)
        return public_error_response(
            context,
            exc,
            logger=log,
            component="xiaoqing_chat.handle",
        )


async def call_bot_name_only(context) -> list[dict[str, Any]]:
    """
    当消息只有 bot_name 时的随机回复

    注意：此函数由 dispatcher 调用，作为 smalltalk provider 的一部分
    """
    return await call_bot_name_only_internal(context)


async def observe_message(clean_text: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await observe_message_internal(clean_text, event, context)


async def observe_outgoing_action(
    action: dict[str, Any],
    context,
    *,
    source_plugin: str = "",
) -> list[dict[str, Any]]:
    return await observe_outgoing_action_internal(
        action,
        context,
        source_plugin=source_plugin,
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
                logger=log,
                component=f"xiaoqing_chat.shutdown.{component}",
            )

    try:
        if state.memory_db.is_dirty():
            await asyncio.to_thread(state.memory_db.save)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=log,
            component="xiaoqing_chat.shutdown.memory_db",
        )


async def shutdown(context) -> None:
    """停止新后台工作，完整落盘，再取消超时任务并做最终落盘。"""

    log = getattr(context, "logger", logger)
    log.info("XiaoQing Chat plugin shutting down")
    state = _state()
    state.stop_accepting_background_tasks()

    # 已登记任务先获得一次自然收尾机会；关闭标志会拒绝它们继续派生后台工作。
    bg_tasks = state.background_tasks()
    pending: set[asyncio.Task[Any]] = set()
    if bg_tasks:
        log.info("XiaoQing Chat: waiting for %d background tasks...", len(bg_tasks))
        _done, pending = await asyncio.wait(bg_tasks, timeout=5.0)

    # 必须在取消任务前保存：防抖写入本身也可能位于 pending 集合中。
    await _flush_shutdown_state(context, state, log)
    if pending:
        log.warning("XiaoQing Chat: cancelling %d unfinished tasks", len(pending))
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=2.0)
        # 任务的 finally 块可能在取消过程中产生最后一批状态变更。
        await _flush_shutdown_state(context, state, log)

    log.info("XiaoQing Chat plugin shutdown complete")
