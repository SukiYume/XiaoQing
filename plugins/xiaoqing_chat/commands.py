"""``/xc`` 管理子命令：统计、重置、配置、记忆、表达、黑话、审查与模型切换。

权限判断必须同时匹配事件用户、事件会话范围和当前 Principal，不能只相信事件字典中的
身份字段；所有模型覆盖仅修改本次运行状态，不写回配置文件。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.plugin_base import segments

from .chat_event import event_chat_id
from .chat_runtime import ai_route_context, chat_lock
from .handler_context import HandlerContext, handle_errors
from .logging_utils import redacted_value
from .memory import client as memory_client
from .memory.person_profile import clear_profiles_and_memory
from .memory.review_sessions import apply_review_answer, render_session_prompt
from .runtime_state import get_state


async def _clear_stores(chat_id: str, *stores) -> None:
    for store in stores:
        await asyncio.to_thread(store.clear, chat_id)


async def reset_chat_session(state, chat_id: str, data_dir: Path, context: Any = None) -> bool:
    """清除会话私有状态，返回长期记忆清没清成；媒体注册表按 media_key 全局共享，不能按 chat_id 删除。

    长期记忆没清成时本地状态照样清空，待清的会话由记忆客户端记下并自动重试。
    """

    # clear() 需要等待已经进入 asyncio.to_thread()、无法取消的持久化任务，
    # 因此把这段等待也放到事件循环之外。
    await asyncio.to_thread(state.memory_store.clear, chat_id)
    await asyncio.to_thread(clear_profiles_and_memory, data_dir, chat_id, state.memory_db)
    await _clear_stores(
        chat_id,
        state.goal_store,
        state.heartflow,
        state.action_history,
        state.pfc_state_store,
        state.bw_expr_store,
        state.bw_jargon_store,
        state.bw_recorder,
        state.bw_tracker_store,
    )
    state.review_store.clear_policy(chat_id)
    state.review_store.clear_sessions_for_chat(chat_id)
    memory_cleared = context is None or await memory_client.forget_chat(context, chat_id)
    state.clear_transient_chat_state(chat_id)
    return memory_cleared


async def reset_transient_conversation_state(state, chat_id: str) -> None:
    """长空档后清理旧话题状态，保留完整聊天历史及独立计时的召唤标记。"""

    await _clear_stores(
        chat_id, state.goal_store, state.heartflow, state.action_history, state.pfc_state_store
    )
    state.clear_transient_chat_state(chat_id)


def _visible_jargons(jargons: Mapping[Any, Any], chat_id: str) -> list[Any]:
    """统计页与黑话页列出的词条：和回复时能用上的是同一批（``JargonRecord.visible_to``）。"""
    return [item for item in jargons.values() if item.visible_to(chat_id)]


@handle_errors
async def handle_internal(
    command: str, args: str, event: dict[str, Any], context
) -> list[dict[str, Any]]:
    hctx = HandlerContext.from_event(event, context)
    chat_id, runtime, state = hctx.chat_id, hctx.runtime, hctx.state

    if command == "统计":
        lines = ["📊 会话统计\n"]

        mem_msgs = await state.memory_store.get_async(chat_id)
        lines.append(f"• 上下文消息数: {len(mem_msgs) if mem_msgs else 0}")

        expressions    = state.bw_expr_store.load()
        expr_this_chat = [e for e in expressions if e.chat_id == chat_id]
        lines.append(f"• 学到的表达 (本会话/全部): {len(expr_this_chat)}/{len(expressions)}")

        jargons        = state.bw_jargon_store.load()
        scoped_jargons = _visible_jargons(jargons, chat_id)
        lines.append(f"• 学到的黑话 (本会话): {len(scoped_jargons)}")

        recent_actions = await state.action_history.get_recent_async(chat_id, max_items=100)
        lines.append(f"• 近期行动记录: {len(recent_actions)}")

        run_stats = state.get_stats(chat_id)
        lines.append("\n本次运行 (重启后重置):")
        lines.append(f"• 回复数: {run_stats.get('replies', 0)}")
        lines.append(f"• 重置数: {run_stats.get('resets', 0)}")
        return segments("\n".join(lines))

    if command == "重置":
        is_group_reset = event.get("group_id") not in (None, "")
        confirmation   = str(args or "").strip().casefold()
        # 群里真正清空要走 reset confirm，它在 plugin.json 里只给群管理员，Core 在调用前检查。
        if is_group_reset and confirmation not in {"确认", "confirm"}:
            return segments(
                f"⚠️ 此操作会清空本群中与{hctx.bot_name}相关的共享上下文、目标、学习状态和行动记录。\n"
                "确认无误后请群主、群管理员或 Bot 管理员发送：/xc 重置 确认"
            )

        pending = state.pop_persist_task(chat_id)
        if pending is not None and not pending.done():
            pending.cancel()
        async with chat_lock(chat_id):
            memory_cleared = await reset_chat_session(state, chat_id, hctx.data_dir, context)
        state.inc_stats(chat_id, "resets")
        context.logger.info(
            "XiaoQing Chat reset_audit scope=%s chat_id=%s group_id=%s operator_user_id=%s",
            "group" if is_group_reset else "private",
            redacted_value(chat_id),
            redacted_value(event.get("group_id")),
            redacted_value(event.get("user_id")),
        )
        if not memory_cleared:
            return segments(
                "⚠️ 会话记忆已重置，但长期记忆这次没清掉，之后会自动重试；清掉之前新聊的内容不会记进长期记忆。"
            )
        return segments("✅ 已重置会话记忆")

    if command == "深度对话":
        current   = runtime.cfg.brain_chat.enable_private_brain_chat
        status    = "✅ 已启用" if current else "❌ 未启用"
        mode_desc = (
            f"🧠 深度对话模式\n\n"
            f"状态: {status}\n"
            f"说明: 深度对话模式提供更智能、更深入的对话体验。\n"
            f"- 更大的上下文窗口 ({runtime.cfg.brain_chat.brain_max_context_size} 条)\n"
            f"- 更强的思考能力 (think_level={runtime.cfg.brain_chat.brain_think_level})\n"
            f"- 专门的对话人格和风格\n"
            f"- 采样参数沿用聊天路由\n\n"
            f"提示: 把 plugins/xiaoqing_chat/config/xiaoqing_config.json 里的 brain_chat.enable_private_brain_chat 设为 true 来启用此模式。"
        )
        return segments(mode_desc)

    return segments(f"❌ 未知的内部命令: {command}")


@handle_errors
async def handle_config(_args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    hctx = HandlerContext.from_event(event, context)
    runtime, secrets = hctx.runtime, hctx.secrets
    cfg = runtime.cfg

    lines = [
        "⚙️ 插件配置概要\n",
        "基础配置:",
        f"• 普通群聊插话概率: {cfg.reply_probability_base:.0%}",
        f"• 最小回复间隔: {cfg.min_reply_interval_seconds} 秒",
        f"• 每分钟最大回复数: {cfg.max_replies_per_minute}",
        f"• 最大上下文大小: {cfg.max_context_size} 条",
        f"• LLM 温度: {cfg.llm_config('chat').temperature}",
        f"• 最大 token 数: {cfg.llm_config('chat').max_tokens}",
    ]

    lines.append("\n记忆系统:")
    lines.append(
        f"• 记忆检索: {'✅ 已启用' if cfg.memory.enable_memory_retrieval else '❌ 未启用'}"
    )
    lines.append(f"• /xc 记忆 条数: {cfg.memory.top_k}")
    lines.append(f"• 知识库最低相关度: {cfg.memory.min_score}")

    lines.append("\n表达学习:")
    lines.append(
        f"• 表达学习: {'✅ 已启用' if cfg.expression.enable_expression_learning else '❌ 未启用'}"
    )
    lines.append(f"• 最大存储数: {cfg.expression.max_store}")

    lines.append("\n深度对话模式:")
    brain_status = "✅ 已启用" if cfg.brain_chat.enable_private_brain_chat else "❌ 未启用"
    lines.append(f"• 状态: {brain_status}")
    if cfg.brain_chat.enable_private_brain_chat:
        lines.append(f"• 上下文大小: {cfg.brain_chat.brain_max_context_size} 条")
        lines.append(f"• 思考等级: {cfg.brain_chat.brain_think_level}")

    model_alias = secrets.get("_provider_name", "?")
    model_name  = secrets.get("model", "?")
    lines.append(f"\n当前 LLM 模型: {model_alias} ({model_name})")
    lines.append("\n提示: 模型路由由 config/config.json 统一配置")
    return segments("\n".join(lines))


@handle_errors
async def handle_review(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    parts = str(args or "").strip().split(maxsplit=2)
    if len(parts) < 2 or parts[0].casefold() not in {"ok", "no", "answer", "close"}:
        return segments("用法：/xc 审查 <ok|no|answer|close> <会话ID> [内容]")
    action, session_id = parts[0].casefold(), parts[1]
    answer  = parts[2].strip() if len(parts) > 2 else ""
    hctx    = HandlerContext.from_event(event, context)
    store   = hctx.state.review_store
    session = store.get_session(session_id)
    if session is None or session.chat_id != hctx.chat_id:
        return segments("❌ 审查会话不存在、已过期或不属于当前会话")
    if action in {"no", "close"}:
        store.close_session(session_id)
        return segments("✅ 审查会话已关闭")
    if action == "ok":
        store.close_session(session_id)
        return segments("✅ 已确认当前目标/策略，无需修改")
    if not answer:
        return segments("❌ answer 操作必须提供目标或策略内容")
    cfg = hctx.runtime.cfg.reflection
    session, applied = apply_review_answer(
        store             = store,
        sess              = session,
        answer            = answer,
        goal_lock_seconds = cfg.goal_lock_seconds,
    )
    if applied is None:
        return segments("❌ 当前审查会话不接受该答案")
    store.update_session(session)
    return segments(f"✅ {applied}\n\n{render_session_prompt(session)}")


@handle_errors
async def handle_memory(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    query = args.strip() if args else ""
    if not query:
        return segments("🔍 记忆检索\n\n使用方法: /xc 记忆 <关键词>\n\n示例: /xc 记忆 喜欢的食物")

    hctx = HandlerContext.from_event(event, context)
    if memory_client.capability(context) is None:
        return segments("❌ 统一记忆不可用")
    # 只查当前会话能看到的内容：这个会话的笔记、页面和原始记录，以及她自己的事。
    results = await memory_client.search(
        context,
        hctx.chat_id,
        {"query": query, "include_events": True, "limit": max(1, hctx.runtime.cfg.memory.top_k)},
    )
    if not results:
        return segments(f"🔍 记忆检索结果\n\n关键词: {query}\n\n未找到相关记忆")

    lines = [f"🔍 记忆检索结果\n\n关键词: {query}\n"]
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. {item.get('text')}")
    return segments("\n".join(lines))


@handle_errors
async def handle_expression(_args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    hctx             = HandlerContext.from_event(event, context)
    state            = hctx.state
    expression_store = state.bw_expr_store
    expressions      = [item for item in expression_store.load() if item.chat_id == hctx.chat_id]

    if not expressions:
        return segments(
            f"💬 表达学习\n\n还没有学到任何表达方式\n\n继续聊天，{hctx.bot_name}会从对话中学习表达风格"
        )

    expressions.sort(key=lambda x: (x.count, x.last_active_time), reverse=True)
    lines = ["💬 学到的表达方式\n", f"共 {len(expressions)} 条记录\n"]
    for i, expr in enumerate(expressions[:10], 1):
        lines.append(f"{i}. [{expr.style}] {expr.situation}")
        if expr.content_list:
            lines.append(f"   示例: {', '.join(expr.content_list[:3])}")
        lines.append(f"   使用次数: {expr.count}\n")
    if len(expressions) > 10:
        lines.append(f"\n... 还有 {len(expressions) - 10} 条记录")
    return segments("\n".join(lines))


@handle_errors
async def handle_jargon(_args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    hctx         = HandlerContext.from_event(event, context)
    state        = hctx.state
    jargon_store = state.bw_jargon_store
    jargons      = jargon_store.load()
    if not jargons:
        return segments(
            f"🏴‍☠️ 黑话学习\n\n还没有学到任何黑话\n\n继续聊天，{hctx.bot_name}会从对话中学习独特的词汇"
        )

    jargon_list = sorted(
        _visible_jargons(jargons, hctx.chat_id),
        key     = lambda x: x.count,
        reverse = True,
    )
    lines = ["🏴‍☠️ 学到的黑话\n", f"共 {len(jargon_list)} 条记录\n"]
    for i, jar in enumerate(jargon_list[:15], 1):
        pending     = "" if jar.confirmed else "（待确认）"
        meaning_str = f" - {jar.meaning}{pending}" if jar.meaning else ""
        lines.append(f"{i}. {jar.content}{meaning_str} (次数: {jar.count})")
    if len(jargon_list) > 15:
        lines.append(f"\n... 还有 {len(jargon_list) - 15} 条记录")
    return segments("\n".join(lines))


def _provider_listing(state, chat_id: str, providers: Mapping[str, Any], current: str) -> str:
    lines = ["🤖 LLM 模型\n"]
    for name, pcfg in providers.items():
        if isinstance(pcfg, Mapping):
            marker = " ✅" if current == name else ""
            lines.append(
                f"• {name} ({pcfg.get('model', '?')} / {pcfg.get('provider', '?')}){marker}"
            )
    if not providers:
        lines.append("(当前路由未配置任何模型)")
    lines += [
        f"\n当前会话覆盖: {state.get_chat_provider(chat_id) or '(继承)'}",
        f"全局运行时覆盖: {state.global_active_provider or '(未设置)'}",
        "默认状态按上列顺序自动回退；手动选择后会严格固定该模型。",
        "固定当前会话: /xc 模型 <名称|default>",
        "Bot 管理员全局固定: /xc 模型 global <名称|default>",
    ]
    return "\n".join(lines)


@handle_errors
async def handle_provider(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    state   = get_state()
    chat_id = event_chat_id(event)
    route = ai_route_context(context, chat_id=chat_id)
    providers = route.get("_providers", {})
    providers = providers if isinstance(providers, Mapping) else {}

    target = (args or "").strip()
    if not target:
        current = str(route.get("_provider_name", "") or "")
        return segments(_provider_listing(state, chat_id, providers, current))

    parts = target.split(maxsplit=1)
    global_scope = parts[0].casefold() in {"global", "全局"}
    if global_scope:
        if len(parts) != 2 or not parts[1].strip():
            return segments("用法: /xc 模型 global <名称|default>")
        selection = parts[1].strip()
    else:
        selection = target

    def apply(name: str | None) -> None:
        if global_scope:
            state.set_global_provider(name)
        else:
            state.set_chat_provider(chat_id, name)

    scope = "全局运行时" if global_scope else "当前会话"
    if selection.casefold() in {"默认", "default", "reset"}:
        apply(None)
        effective = state.resolve_provider_name(
            chat_id, list(providers), str(route.get("_default", "") or "")
        )
        model = providers.get(effective, {}).get("model", "?")
        return segments(
            f"✅ 已清除{scope}覆盖；当前生效 {effective} ({model})"
            "\n此切换仅在本次 Bot 运行期间有效。"
        )
    if selection not in providers:
        available = ", ".join(providers.keys()) if providers else "(无)"
        return segments(f"❌ 未知模型别名 '{selection}'\n可用: {available}")
    apply(selection)
    return segments(
        f"✅ 已将{scope}模型切换到 {selection} ({providers[selection].get('model', '?')})"
        "\n此切换仅在本次 Bot 运行期间有效。"
    )
