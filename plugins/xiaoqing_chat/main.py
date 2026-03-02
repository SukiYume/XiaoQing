"""
小青智能对话插件
提供 AI 对话、上下文记忆、表达学习等高级功能
"""
from __future__ import annotations

import logging
from typing import Any

from core.plugin_base import segments
from core.args import parse

from .handlers import (
    handle_smalltalk,
    observe_message as observe_message_internal,
    call_bot_name_only_internal,
    handle_internal,
    handle_config,
    handle_memory,
    handle_expression,
    handle_jargon,
    handle_provider,
)
from .runtime_state import get_state as _state


logger = logging.getLogger(__name__)

# ============================================================
# 子命令路由表
# ============================================================

_SUBCOMMANDS: dict[str, str] = {
    "help": "帮助",   "帮助": "帮助",   "?": "帮助",
    "reset": "重置",  "重置": "重置",   "清空": "重置",
    "stats": "统计",  "统计": "统计",   "状态": "统计",
    "brain": "深度",  "深度": "深度",
    "config": "配置", "配置": "配置",
    "memory": "记忆", "记忆": "记忆",
    "expression": "表达", "表达": "表达",
    "jargon": "黑话", "黑话": "黑话",
    "model": "模型",  "模型": "模型",   "provider": "模型",  "供应商": "模型",
}

_HANDLERS: dict[str, Any] = {
    "重置": lambda rest, ev, ctx: handle_internal("重置", rest, ev, ctx),
    "统计": lambda rest, ev, ctx: handle_internal("统计", rest, ev, ctx),
    "深度": lambda rest, ev, ctx: handle_internal("深度对话", rest, ev, ctx),
    "配置": handle_config,
    "记忆": handle_memory,
    "表达": handle_expression,
    "黑话": handle_jargon,
    "模型": handle_provider,
}


def init(context=None) -> None:
    """插件初始化"""
    if context:
        log = getattr(context, "logger", logger)
        log.info("XiaoQing Chat plugin initialized")


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context,
) -> list[dict[str, Any]]:
    """命令处理入口 — 统一 /xc <子命令> 风格"""
    try:
        parsed = parse(args)
        subcommand = parsed.first.lower() if parsed.first else ""

        # 无参数 → 帮助
        if not subcommand:
            return segments(_show_help())

        # 子命令路由
        action = _SUBCOMMANDS.get(subcommand)

        if action == "帮助":
            return segments(_show_help())

        handler = _HANDLERS.get(action) if action else None
        if handler:
            return await handler(parsed.rest(1), event, context)

        # 未匹配子命令 → 当作聊天内容（使用 args 而非 raw_message，避免带上 /xc 前缀）
        text = args.strip()
        if not text:
            return []
        # 显式 /xc 命令 → 标记强制回复，跳过概率判断
        event["_xc_command_forced"] = True
        return await handle_smalltalk(text, event, context)

    except Exception as e:
        log = getattr(context, "logger", logger)
        log.exception("XiaoQing Chat handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


def _show_help() -> str:
    """显示帮助信息"""
    return """
💬 **小青智能对话**

**基础对话:**
• /xc <内容> - 和小青聊天
• @小青 <内容> - 在群聊中呼叫小青

**会话管理:**
• /xc 清空 - 清空当前会话的上下文记忆
• /xc 统计 - 查看当前会话的统计信息

**高级功能:**
• /xc 深度 - 查看深度对话模式状态
• /xc 配置 - 查看插件配置概要
• /xc 记忆 <关键词> - 检索长期记忆
• /xc 表达 - 查看学到的表达方式
• /xc 黑话 - 查看学到的黑话
• /xc 模型 - 查看/切换 LLM 供应商

**使用提示:**
• 群聊中需要 @小青 或使用命令触发
• 私聊会自动启用聊天模式

输入 /xc help 查看此帮助
""".strip()


async def call_bot_name_only(context) -> list[dict[str, Any]]:
    """
    当消息只有 bot_name 时的随机回复
    
    注意：此函数由 dispatcher 调用，作为 smalltalk provider 的一部分
    """
    return await call_bot_name_only_internal(context)


async def observe_message(clean_text: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await observe_message_internal(clean_text, event, context)


async def shutdown(context) -> None:
    """
    插件卸载时清理
    
    等待后台任务完成（带超时），然后执行兆底数据保存。
    """
    log = getattr(context, "logger", logger)
    log.info("XiaoQing Chat plugin shutting down")
    import asyncio
    state = _state()

    # 1. Wait for background tasks with timeout
    bg_tasks = set(state._bg_tasks)
    if bg_tasks:
        log.info("XiaoQing Chat: waiting for %d background tasks...", len(bg_tasks))
        done, pending = await asyncio.wait(bg_tasks, timeout=5.0)
        if pending:
            log.warning("XiaoQing Chat: cancelling %d unfinished tasks", len(pending))
            for t in pending:
                t.cancel()
            # Give cancelled tasks a moment to clean up
            await asyncio.wait(pending, timeout=2.0)

    # 2. Final flush: persist memory and action history
    try:
        state.action_history.flush_all()
    except Exception as exc:
        log.warning("XiaoQing Chat: action_history flush failed: %s", exc)

    # 3. Save vector DB if dirty
    try:
        if state.memory_db.is_dirty():
            await asyncio.to_thread(state.memory_db.save)
    except Exception as exc:
        log.warning("XiaoQing Chat: memory_db save failed: %s", exc)

    log.info("XiaoQing Chat plugin shutdown complete")
    # 当前没有需要清理的资源
    pass
