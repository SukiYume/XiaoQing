from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.plugin_base import segments

from .memory.review_sessions import apply_review_answer, render_session_prompt


def get_data_dir(context) -> Path:
    return context.data_dir


def get_bound_state(context, *, state_loader: Callable[[], Any], bind_all_stores: Callable[[Any, Path], None]):
    state = state_loader()
    bind_all_stores(state, get_data_dir(context))
    return state


def is_admin_operator(event: dict[str, Any], context) -> bool:
    principal = getattr(context, "principal", None) if context is not None else None
    if principal is None or getattr(principal, "kind", None) != "user":
        return False
    try:
        if int(principal.user_id) != int(event.get("user_id")):
            return False
    except (TypeError, ValueError):
        return False

    event_group_id = event.get("group_id")
    if event_group_id in (None, ""):
        if getattr(principal, "group_id", None) is not None or not getattr(
            principal, "is_private", False
        ):
            return False
        return is_global_admin_operator(event, context)

    try:
        if int(principal.group_id) != int(event_group_id):
            return False
    except (TypeError, ValueError):
        return False
    if is_global_admin_operator(event, context):
        return True
    can_manage_group = getattr(principal, "can_manage_group", None)
    return bool(callable(can_manage_group) and can_manage_group(event_group_id))


def is_global_admin_operator(event: dict[str, Any], context) -> bool:
    principal = getattr(context, "principal", None) if context is not None else None
    capabilities = getattr(context, "capabilities", None) if context is not None else None
    if (
        principal is None
        or getattr(principal, "kind", None) != "user"
        or not getattr(capabilities, "is_bot_admin", False)
    ):
        return False
    try:
        if int(principal.user_id) != int(event.get("user_id")):
            return False
    except (TypeError, ValueError):
        return False
    event_group_id = event.get("group_id")
    if event_group_id in (None, ""):
        return getattr(principal, "group_id", None) is None and bool(
            getattr(principal, "is_private", False)
        )
    try:
        return int(principal.group_id) == int(event_group_id) and not getattr(
            principal, "is_private", False
        )
    except (TypeError, ValueError):
        return False


def short_base(url: str) -> str:
    url = (url or "").rstrip("/")
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix) :]
    parts = url.split("/")
    return parts[0] if parts else url


async def handle_internal_impl(
    command: str,
    args: str,
    event: dict[str, Any],
    context,
    *,
    handler_context_from_event,
    get_lock,
    reset_chat_session,
    cancel_pending_task,
    is_admin_operator_fn: Callable[[dict[str, Any], Any], bool],
) -> list[dict[str, Any]]:
    hctx = handler_context_from_event(event, context)
    chat_id, runtime, state = hctx.chat_id, hctx.runtime, hctx.state

    if command == "统计":
        lines = ["📊 **会话统计**\n"]

        mem_msgs = await state.memory_store.get_async(chat_id)
        lines.append(f"• 上下文消息数: {len(mem_msgs) if mem_msgs else 0}")

        expressions = state.bw_expr_store.load()
        expr_this_chat = [e for e in expressions if e.chat_id == chat_id]
        lines.append(f"• 学到的表达 (本会话/全部): {len(expr_this_chat)}/{len(expressions)}")

        jargons = state.bw_jargon_store.load()
        scoped_jargons = [
            item for item in jargons.values()
            if item.is_global or any(str(pair[0]) == chat_id for pair in item.chat_id_counts if pair)
        ]
        lines.append(f"• 学到的黑话 (本会话): {len(scoped_jargons)}")

        recent_actions = await state.action_history.get_recent_async(chat_id, max_items=100)
        lines.append(f"• 近期行动记录: {len(recent_actions)}")

        run_stats = state.get_stats(chat_id)
        lines.append("\n**本次运行 (重启后重置):**")
        lines.append(f"• 回复数: {run_stats.get('replies', 0)}")
        lines.append(f"• 重置数: {run_stats.get('resets', 0)}")
        return segments("\n".join(lines))

    if command == "重置":
        is_group_reset = event.get("group_id") not in (None, "")
        confirmation = str(args or "").strip().casefold()
        if is_group_reset:
            if not is_admin_operator_fn(event, context):
                return segments(
                    "❌ 群会话重置仅限 Bot 管理员、群管理员或群主执行；"
                    "普通成员不能清除群共享状态。"
                )
            if confirmation not in {"确认", "confirm"}:
                return segments(
                    "⚠️ 此操作会清空本群的小青共享上下文、目标、学习状态和行动记录。\n"
                    "确认无误后请发送：/xc 重置 确认"
                )

        pop_persist_task = getattr(state, "pop_persist_task", None)
        pending = pop_persist_task(chat_id) if callable(pop_persist_task) else None
        if pending is not None:
            cancel_pending_task(pending)
        async with get_lock(chat_id):
            await reset_chat_session(state, chat_id)
        state.inc_stats(chat_id, "resets")
        context.logger.info(
            "XiaoQing Chat reset_audit scope=%s chat_id=%s group_id=%s operator_user_id=%s",
            "group" if is_group_reset else "private",
            chat_id,
            event.get("group_id"),
            event.get("user_id"),
        )
        return segments("✅ 已重置会话记忆")

    if command == "深度对话":
        current = runtime.cfg.brain_chat.enable_private_brain_chat
        status = "✅ 已启用" if current else "❌ 未启用"
        mode_desc = (
            f"🧠 **深度对话模式**\n\n"
            f"状态: {status}\n"
            f"说明: 深度对话模式提供更智能、更深入的对话体验。\n"
            f"- 更大的上下文窗口 ({runtime.cfg.brain_chat.brain_max_context_size} 条)\n"
            f"- 更强的思考能力 (think_level={runtime.cfg.brain_chat.brain_think_level})\n"
            f"- 专门的对话人格和风格\n"
            f"- 更理性的回复温度 ({runtime.cfg.brain_chat.brain_temperature})\n\n"
            f"提示: 修改 config/xiaoqing_config.json 中的 brain_chat.enable_private_brain_chat 来启用此模式。"
        )
        return segments(mode_desc)

    return segments(f"❌ 未知的内部命令: {command}")


async def handle_config_impl(args: str, event: dict[str, Any], context, *, handler_context_from_event) -> list[dict[str, Any]]:
    hctx = handler_context_from_event(event, context)
    runtime, secrets = hctx.runtime, hctx.secrets
    cfg = runtime.cfg

    lines = [
        "⚙️ **插件配置概要**\n",
        "**基础配置:**",
        f"• 普通群聊插话概率: {cfg.reply_probability_base:.0%}",
        f"• 最小回复间隔: {cfg.min_reply_interval_seconds} 秒",
        f"• 每分钟最大回复数: {cfg.max_replies_per_minute}",
        f"• 最大上下文大小: {cfg.max_context_size} 条",
        f"• LLM 温度: {cfg.temperature}",
        f"• 最大 token 数: {cfg.max_tokens}",
    ]

    lines.append("\n**记忆系统:**")
    lines.append(
        f"• 记忆检索: {'✅ 已启用' if cfg.memory.enable_memory_retrieval else '❌ 未启用'}"
    )
    lines.append(f"• 最大检索数: {cfg.memory.top_k}")
    lines.append(f"• 最小相似度: {cfg.memory.min_score}")

    lines.append("\n**表达学习:**")
    lines.append(
        f"• 表达学习: {'✅ 已启用' if cfg.expression.enable_expression_learning else '❌ 未启用'}"
    )
    lines.append(f"• 最大注入数: {cfg.expression.max_injected}")
    lines.append(f"• 最大存储数: {cfg.expression.max_store}")

    lines.append("\n**深度对话模式:**")
    brain_status = "✅ 已启用" if cfg.brain_chat.enable_private_brain_chat else "❌ 未启用"
    lines.append(f"• 状态: {brain_status}")
    if cfg.brain_chat.enable_private_brain_chat:
        lines.append(f"• 上下文大小: {cfg.brain_chat.brain_max_context_size} 条")
        lines.append(f"• 思考等级: {cfg.brain_chat.brain_think_level}")

    provider_name = secrets.get("_provider_name", "?")
    provider_model = secrets.get("model", "?")
    lines.append(f"\n**LLM 供应商:** {provider_name} ({provider_model})")
    lines.append("\n**提示:** 详细配置请查看 config/xiaoqing_config.json")
    return segments("\n".join(lines))


async def handle_review_impl(
    args: str,
    event: dict[str, Any],
    context,
    *,
    handler_context_from_event,
    is_admin_operator_fn,
) -> list[dict[str, Any]]:
    if not is_admin_operator_fn(event, context):
        return segments("❌ 仅 Bot 管理员、群管理员或群主可处理审查会话")
    parts = str(args or "").strip().split(maxsplit=2)
    if len(parts) < 2 or parts[0].casefold() not in {"ok", "no", "answer", "close"}:
        return segments("用法：/xc 审查 <ok|no|answer|close> <会话ID> [内容]")
    action, session_id = parts[0].casefold(), parts[1]
    answer = parts[2].strip() if len(parts) > 2 else ""
    hctx = handler_context_from_event(event, context)
    store = hctx.state.review_store
    session = store.get_session(session_id)
    if session is None or session.chat_id != hctx.chat_id:
        return segments("❌ 审查会话不存在、已过期或不属于当前会话")
    if action in {"no", "close"}:
        store.close_session(session_id)
        return segments("✅ 审查会话已关闭")
    if action == "ok":
        if session.kind == "bad_reply_pattern" and session.step <= 0:
            session.step = 1
            store.update_session(session)
            return segments(render_session_prompt(session))
        store.close_session(session_id)
        return segments("✅ 已确认当前目标/策略，无需修改")
    if not answer:
        return segments("❌ answer 操作必须提供规则、目标或策略内容")
    cfg = hctx.runtime.cfg.reflection
    session, applied = apply_review_answer(
        store=store,
        sess=session,
        answer=answer,
        goal_lock_seconds=cfg.goal_lock_seconds,
        max_avoid_patterns=cfg.max_avoid_patterns,
    )
    if applied is None:
        return segments("❌ 当前审查会话不接受该答案")
    store.update_session(session)
    return segments(f"✅ {applied}\n\n{render_session_prompt(session)}")


async def handle_memory_impl(args: str, event: dict[str, Any], context, *, handler_context_from_event) -> list[dict[str, Any]]:
    query = args.strip() if args else ""
    if not query:
        return segments("🔍 **记忆检索**\n\n使用方法: /xc 记忆 <关键词>\n\n示例: /xc 记忆 喜欢的食物")

    hctx = handler_context_from_event(event, context)
    runtime, state = hctx.runtime, hctx.state
    memory_db = state.memory_db
    if not memory_db:
        return segments("❌ 记忆数据库未初始化")

    results = memory_db.query(
        query, chat_id=hctx.chat_id, top_k=runtime.cfg.memory.top_k, min_score=runtime.cfg.memory.min_score
    )
    if not results:
        return segments(f"🔍 **记忆检索结果**\n\n关键词: {query}\n\n未找到相关记忆")

    lines = [f"🔍 **记忆检索结果**\n\n关键词: {query}\n"]
    for i, item in enumerate(results, 1):
        score = item.score * 100
        lines.append(f"**{i}.** (相关度: {score:.1f}%)")
        lines.append(f"{item.text}\n")
    return segments("\n".join(lines))


async def handle_expression_impl(args: str, event: dict[str, Any], context, *, handler_context_from_event) -> list[dict[str, Any]]:
    hctx = handler_context_from_event(event, context)
    state = hctx.state
    expression_store = state.bw_expr_store
    expressions = [item for item in expression_store.load() if item.chat_id == hctx.chat_id]

    if not expressions:
        return segments(
            "💬 **表达学习**\n\n还没有学到任何表达方式\n\n继续聊天，小青会从对话中学习表达风格"
        )

    expressions.sort(key=lambda x: (x.count, x.last_active_time), reverse=True)
    lines = ["💬 **学到的表达方式**\n", f"共 {len(expressions)} 条记录\n"]
    for i, expr in enumerate(expressions[:10], 1):
        lines.append(f"**{i}.** [{expr.style}] {expr.situation}")
        if expr.content_list:
            lines.append(f"   示例: {', '.join(expr.content_list[:3])}")
        lines.append(f"   使用次数: {expr.count}\n")
    if len(expressions) > 10:
        lines.append(f"\n... 还有 {len(expressions) - 10} 条记录")
    return segments("\n".join(lines))


async def handle_jargon_impl(args: str, event: dict[str, Any], context, *, handler_context_from_event) -> list[dict[str, Any]]:
    hctx = handler_context_from_event(event, context)
    state = hctx.state
    jargon_store = state.bw_jargon_store
    jargons = jargon_store.load()
    if not jargons:
        return segments(
            "🏴‍☠️ **黑话学习**\n\n还没有学到任何黑话\n\n继续聊天，小青会从对话中学习独特的词汇"
        )

    jargon_list = sorted(
        [
            item for item in jargons.values()
            if item.is_global or any(str(pair[0]) == hctx.chat_id for pair in item.chat_id_counts if pair)
        ],
        key=lambda x: x.count,
        reverse=True,
    )
    lines = ["🏴‍☠️ **学到的黑话**\n", f"共 {len(jargon_list)} 条记录\n"]
    for i, jar in enumerate(jargon_list[:15], 1):
        meaning_str = f" - {jar.meaning}" if jar.meaning else ""
        lines.append(f"**{i}.** {jar.content}{meaning_str} (次数: {jar.count})")
    if len(jargon_list) > 15:
        lines.append(f"\n... 还有 {len(jargon_list) - 15} 条记录")
    return segments("\n".join(lines))


async def handle_provider_impl(
    args: str,
    event: dict[str, Any],
    context,
    *,
    state_getter: Callable[[], Any],
    chat_id_from_event: Callable[[dict[str, Any]], str],
    is_admin_operator_fn: Callable[[dict[str, Any], Any], bool],
    is_global_admin_operator_fn: Callable[[dict[str, Any], Any], bool],
    short_base_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    state = state_getter()
    chat_id = chat_id_from_event(event)
    secrets_base: dict[str, Any] = (context.secrets or {}).get("plugins", {}).get(
        "xiaoqing_chat", {}
    ) or {}
    configured_providers = secrets_base.get("providers") or {}
    providers: dict[str, dict[str, Any]] = {
        str(name): dict(value)
        for name, value in configured_providers.items()
        if isinstance(name, str) and isinstance(value, dict)
    } if isinstance(configured_providers, dict) else {}
    default_name: str = secrets_base.get("default", "") or ""
    current = state.resolve_provider_name(chat_id, list(providers), default_name)

    target = (args or "").strip()
    if not target:
        lines = ["🤖 **LLM 供应商**\n"]
        for name, pcfg in providers.items():
            if not isinstance(pcfg, dict):
                continue
            model_name = pcfg.get("model", "?")
            base = pcfg.get("api_base", "?")
            marker = " ✅" if current == name else ""
            lines.append(f"• **{name}** ({model_name} @ {short_base_fn(base)}){marker}")
        if not providers:
            lines.append("(未配置任何供应商)")
        local_override = state.get_chat_provider(chat_id)
        global_override = state.global_active_provider
        lines.append(f"\n当前会话覆盖: {local_override or '(继承)'}")
        lines.append(f"全局运行时覆盖: {global_override or '(未设置)'}")
        lines.append("切换当前会话: /xc 模型 <名称|default>")
        lines.append("Bot 管理员切换全局: /xc 模型 global <名称|default>")
        return segments("\n".join(lines))

    parts = target.split(maxsplit=1)
    global_scope = parts[0].casefold() in {"global", "全局"}
    if global_scope:
        if len(parts) != 2 or not parts[1].strip():
            return segments("用法: /xc 模型 global <名称|default>")
        if not is_global_admin_operator_fn(event, context):
            return segments("❌ 只有 Bot 全局管理员可切换全局 LLM 供应商")
        selection = parts[1].strip()
    else:
        if not is_admin_operator_fn(event, context):
            return segments("❌ 仅 Bot 管理员、当前群管理员或群主可切换本会话供应商")
        selection = target

    if selection.casefold() in {"默认", "default", "reset"}:
        if global_scope:
            state.set_global_provider(None)
            scope_label = "全局运行时覆盖"
        else:
            state.set_chat_provider(chat_id, None)
            scope_label = "当前会话覆盖"
        effective = state.resolve_provider_name(chat_id, list(providers), default_name)
        effective_cfg = providers.get(effective, {})
        effective_model = effective_cfg.get("model", "?")
        return segments(
            f"✅ 已清除{scope_label}；当前生效 **{effective}** ({effective_model})"
            "\n此切换仅在本次 Bot 运行期间有效。"
        )

    if selection not in providers:
        available = ", ".join(providers.keys()) if providers else "(无)"
        return segments(f"❌ 未知供应商 '{selection}'\n可用: {available}")

    if global_scope:
        state.set_global_provider(selection)
        scope_label = "全局运行时供应商"
    else:
        state.set_chat_provider(chat_id, selection)
        scope_label = "当前会话供应商"
    pcfg = providers[selection]
    model_name = pcfg.get("model", "?")
    return segments(
        f"✅ 已将{scope_label}切换到 **{selection}** ({model_name})"
        "\n此切换仅在本次 Bot 运行期间有效。"
    )
