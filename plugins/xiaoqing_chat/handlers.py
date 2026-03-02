"""
小青智能对话处理器
包含聊天处理、内部命令等核心功能
"""
from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

from core.plugin_base import build_action, segments

from .constants import EXPRESSION_LEARN_MIN_INTERVAL, EXPRESSION_LEARN_MIN_MESSAGES
from .brain_chat import (
    get_brain_chat_identity,
    get_brain_chat_max_context,
    get_brain_chat_reply_style,
    get_brain_chat_temperature,
    get_brain_chat_think_level,
    is_brain_chat_active,
    maybe_add_mode_indicator,
)
from .helper_utils import (
    _chat_id,
    _extract_sender_name,
    _get_bot_name,
    _get_llm_secrets,
    _is_at_me,
    _has_bot_name,
    _is_private,
    _next_local_id,
    _should_ignore_text,
)
from .logging_utils import _log_step
from .reply_splitter import _split_chat_reply
from .runtime_state import get_state as _state
from .store_binding import _bind_all_stores
from .task_scheduler import _schedule_memory_persist, _schedule_memory_db_save, _spawn_bg_task, _schedule_action_history_flush
from .context_builder import _build_memory_block
from .reply_generator import _generate_reply
from .helper_utils import _load_runtime, _get_lock
from .task_scheduler import _track_bg_task
from .frequency_control import _freq_record, _should_reply, _score_interest
from .planning.goal_state import derive_goal
from .memory.review_sessions import get_goal_override, maybe_open_goal_strategy_review, maybe_push_session
from .expression.bw_expression_reflector import maybe_ask_for_reflection
from .expression.bw_reflect_tracker import tick_reflect_tracker
from .planning.pfc_engine import run_pfc_once
from .llm.summarizer import maybe_update_topic_summary
from .memory.knowledge_extract import maybe_extract_person_facts
from .expression.bw_message_recorder import extract_and_learn
from .planning.action_history import ActionRecord
from .planning.planner import PlannedAction
from .handlers_helper import _spawn_post_reply_bg_tasks


async def handle_smalltalk(clean_text: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    处理闲聊消息
    
    这是 xiaoqing_chat 插件闲聊功能的主要入口点。
    它处理用户消息，运行 PFC (Plan-From-Context) 规划系统，
    生成回复，并安排后台任务进行记忆持久化、摘要和表达学习。
    
    Args:
        clean_text: 清理后的用户消息文本
        event: OneBot 事件字典，包含消息元数据
        context: 插件上下文，包含 logger、config、data_dir 等
    
    Returns:
        要发送的消息段字典列表，如果不应发送回复则返回空列表
    """
    from .helper_utils import _load_runtime
    from .task_scheduler import _track_bg_task

    try:
        return await _maybe_reply_smalltalk(clean_text, event, context)
    except Exception as exc:
        context.logger.exception("XiaoQing Chat smalltalk 处理失败: %s", exc)
        return segments(f"❌ 对话处理出错: {str(exc)}")


async def observe_message(clean_text: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    try:
        runtime = _load_runtime(context)
        if not runtime.cfg.enable_smalltalk:
            return []

        text = (clean_text or "").strip()
        if not text:
            return []
        if _should_ignore_text(text, runtime):
            return []

        _ensure_user_message_recorded(text, event, context, runtime)
    except Exception:
        return []
    return []


def _ensure_user_message_recorded(text: str, event: dict[str, Any], context, runtime) -> str:
    chat_id = _chat_id(event)

    data_dir = _get_data_dir(context)
    state = _state()
    _bind_all_stores(state, data_dir)
    state.review_store.cleanup_expired()

    msg_id = event.get("message_id")
    history = state.memory_store.get(chat_id)
    existing_local_id = str(event.get("_xc_user_recorded_local_id") or "").strip()

    if not existing_local_id and msg_id is not None:
        for msg in reversed(history[-40:]):
            if msg.role == "user" and msg.message_id == msg_id:
                existing_local_id = (msg.local_id or "").strip()
                if existing_local_id:
                    break

    if existing_local_id:
        event["_xc_user_recorded"] = True
        event["_xc_user_recorded_local_id"] = existing_local_id
        return existing_local_id

    local_id = _next_local_id(chat_id)
    state.memory_store.append(
        chat_id,
        role="user",
        name=_extract_sender_name(event),
        user_id=event.get("user_id"),
        message_id=msg_id,
        local_id=local_id,
        content=text,
    )
    _schedule_memory_persist(context, runtime, chat_id=chat_id)
    state.heartflow.on_user_message(chat_id=chat_id)
    _log_step(context, runtime, chat_id=chat_id, step="smalltalk.memory.append_user", fields={"local_id": local_id})
    event["_xc_user_recorded"] = True
    event["_xc_user_recorded_local_id"] = local_id
    return local_id


async def _maybe_reply_smalltalk(clean_text: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    核心闲聊处理逻辑，基于 PFC 规划和回复生成
    
    该函数协调整个闲聊流程：
    1. 验证配置并忽略禁用文本
    2. 将用户消息存储到记忆中
    3. 如果启用则运行反思任务
    4. 根据频率规则检查是否应该发送回复
    5. 获取每个聊天的锁并运行 PFC 规划
    6. 生成并存储机器人的回复
    7. 为摘要和表达学习生成后台任务
    
    注意：为了防止并发修改共享状态，锁会在整个 PFC+回复生成过程中保持。
    这可能会在高负载下导致延迟，是未来优化的候选项。
    
    Args:
        clean_text: 清理后的用户消息文本
        event: OneBot 事件字典
        context: 插件上下文
    
    Returns:
        回复的消息段字典列表，或空列表
    """


    runtime = _load_runtime(context)
    if not runtime.cfg.enable_smalltalk:
        return []

    text = clean_text.strip()
    if _should_ignore_text(text, runtime):
        _log_step(context, runtime, chat_id=_chat_id(event), step="smalltalk.ignore", fields={"text": text})
        return []

    bot_name = _get_bot_name(context)
    mentioned = _is_at_me(event) or _has_bot_name(event, bot_name)
    is_private = _is_private(event)
    command_forced = bool(event.get("_xc_command_forced"))
    forced = command_forced or (is_private and not runtime.cfg.brain_chat.enable_private_brain_chat) or mentioned

    t0 = time.monotonic()
    chat_id = _chat_id(event)
    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.recv",
        fields={
            "is_private": is_private,
            "mentioned": mentioned,
            "forced": forced,
            "brain_chat_enabled": runtime.cfg.brain_chat.enable_private_brain_chat,
            "msg_id": event.get("message_id"),
            "user_id": event.get("user_id"),
            "group_id": event.get("group_id"),
            "text": text,
        },
    )
    data_dir = _get_data_dir(context)
    local_id = _ensure_user_message_recorded(text, event, context, runtime)
    if runtime.cfg.goal.enable_goal:
        g = derive_goal(data_dir=context.data_dir, chat_id=chat_id, current_text=text, planner_reasoning="")
        if runtime.cfg.reflection.enable_review_sessions:
            og = get_goal_override(_state().review_store, chat_id)
            if og:
                g = og
        _state().goal_store.set(chat_id, goal=g, source="user")
        _log_step(context, runtime, chat_id=chat_id, step="smalltalk.goal.set", fields={"goal": g})

    if runtime.cfg.reflection.enable_expression_reflection:
        secrets = _get_llm_secrets(context)

        async def _run_reflection() -> None:
            await tick_reflect_tracker(
                context=context,
                operator_chat_id=chat_id,
                memory_store=_state().memory_store,
                expr_store=_state().bw_expr_store,
                tracker_store=_state().bw_tracker_store,
                secrets=secrets,
                timeout_seconds=float(getattr(runtime.cfg, "background_timeout_seconds", runtime.cfg.timeout_seconds)),
                max_retry=int(getattr(runtime.cfg, "background_max_retry", runtime.cfg.max_retry)),
                retry_interval_seconds=float(getattr(runtime.cfg, "background_retry_interval_seconds", runtime.cfg.retry_interval_seconds)),
                proxy=secrets.get("proxy", "") or "",
                endpoint_path=secrets.get("endpoint_path", "") or runtime.cfg.endpoint_path,
            )
            await maybe_ask_for_reflection(
                context=context,
                expr_store=_state().bw_expr_store,
                tracker_store=_state().bw_tracker_store,
                operator_user_id=int(runtime.cfg.reflection.operator_user_id),
                operator_group_id=int(runtime.cfg.reflection.operator_group_id),
                min_interval_seconds=float(runtime.cfg.reflection.min_interval_seconds),
                ask_per_check=int(runtime.cfg.reflection.ask_per_check),
            )

        _spawn_bg_task(context, _run_reflection(), name=f"reflection:{chat_id}")
        _log_step(context, runtime, chat_id=chat_id, step="smalltalk.reflection.spawn", fields={})


    if not forced:
        _interest = _score_interest(text)
        if not _should_reply(runtime, _state(), chat_id, text, is_private, mentioned, runtime.cfg.brain_chat.enable_private_brain_chat, interest=_interest):
            return []



    secrets = _get_llm_secrets(context)

    # 检查是否为深度对话模式（不需要锁）
    brain_chat_active = is_brain_chat_active(runtime, is_private, forced)

    # 用于 PFC 规划期间并行预取记忆（在锁内设置）
    speculative_memory_task = None

    # 准备 PFC 生成函数（不需要锁）
    async def _pfc_generate(mode: str, planner_reason: str, extra_reason: str) -> str:
        style_override = ""
        if mode == "say_goodbye":
            style_override = "说一句很短很自然的告别收尾，不要延伸话题。"
        elif mode == "send_new_message":
            style_override = "你刚发过一条消息，如果要继续发一条新消息，短一点，别轰炸。"
        act = PlannedAction(
            action="reply",
            target_message_id=local_id,
            think_level=runtime.cfg.planner.resolve_think_level(),
            quote=False,
            reasoning=str(planner_reason or "").strip(),
            question="",
            unknown_words=[],
        )
        pr = (planner_reason or "").strip()
        if extra_reason:
            pr = (pr + "\n" + str(extra_reason).strip()).strip()
        out = await _generate_reply(
            text=text,
            event=event,
            context=context,
            runtime=runtime,
            state=_state(),
            forced=forced,
            action=act,
            plan_reasoning=pr,
            reply_style_override=style_override,
            state_text=mood_text,
            is_brain_chat=brain_chat_active,
            prefetched_memory_task=speculative_memory_task,
        )
        return out or ""

    # 计算本次要使用的情绪状态（持久化，避免每轮随机切换）
    mood_text = _state().get_mood_state(chat_id)
    if mood_text:
        # 已有活跃情绪：以 10% 概率自然漂移到新状态
        if runtime.cfg.personality.states and random.random() < 0.10:
            mood_text = random.choice(runtime.cfg.personality.states)
            _state().set_mood_state(chat_id, mood_text, duration_seconds=1800.0)
    elif runtime.cfg.personality.states and random.random() < runtime.cfg.personality.state_probability:
        # 无活跃情绪：按概率设置一个新状态
        mood_text = random.choice(runtime.cfg.personality.states)
        _state().set_mood_state(chat_id, mood_text, duration_seconds=1800.0)

    history_snapshot = None
    async with _get_lock(chat_id):
        # forced (/xc 命令 或 @小青) → 跳过 PFC 规划，直接回复
        if forced:
            _log_step(context, runtime, chat_id=chat_id, step="smalltalk.forced_direct", fields={})
            direct_act = PlannedAction(
                action="reply",
                target_message_id=local_id,
                think_level=runtime.cfg.planner.resolve_think_level(),
                quote=False,
                reasoning="用户直接发起对话，需要回复",
                question="",
                unknown_words=[],
            )
            reply = await _generate_reply(
                text=text,
                event=event,
                context=context,
                runtime=runtime,
                state=_state(),
                forced=True,
                action=direct_act,
                plan_reasoning="用户直接发起对话，需要回复",
                state_text=mood_text,
                is_brain_chat=brain_chat_active,
            )
            reply = (reply or "").strip()
            if not reply:
                reply = random.choice(["嗯…", "行", "我在听", "你继续", "有点卡，等下"])
            assistant_local_id = _next_local_id(chat_id)
            _state().memory_store.append(chat_id, role="assistant", name=bot_name, local_id=assistant_local_id, content=reply)
            _schedule_memory_persist(context, runtime, chat_id=chat_id)
            _log_step(context, runtime, chat_id=chat_id, step="smalltalk.memory.append_bot", fields={"local_id": assistant_local_id})
            history_snapshot = _state().memory_store.get(chat_id)
            _freq_record(chat_id, runtime, _state(), forced=True)
            _state().heartflow.on_bot_reply(chat_id=chat_id)
            _state().inc_stats(chat_id, "replies")
            _state().action_history.append(
                chat_id,
                ActionRecord(ts=time.time(), local_target=local_id, action="reply", reasoning="forced_direct", detail={"source": "forced"}, executed=True),
            )
            _schedule_action_history_flush(context, runtime, chat_id=chat_id)
        else:
            # 非强制 → 并行预取记忆（与 PFC 规划同时进行，节省 ~2s）
            _max_ctx = get_brain_chat_max_context(runtime) if brain_chat_active else runtime.cfg.max_context_size
            _spec_history = _state().memory_store.get_recent(chat_id, max_items=_max_ctx)
            speculative_memory_task = asyncio.create_task(
                _build_memory_block(
                    context=context,
                    runtime=runtime,
                    state=_state(),
                    secrets=secrets,
                    data_dir=data_dir,
                    chat_id=chat_id,
                    history=_spec_history[-_max_ctx:] if _max_ctx > 0 else [],
                    current_text=text,
                    planner_question="",
                    bot_name=bot_name,
                )
            )
            # PFC 规划（~2s）—— 与记忆预取并行进行
            pfc_result = await run_pfc_once(
                context=context,
                runtime_cfg=runtime.cfg,
                secrets=secrets,
                bot_name=bot_name,
                is_private=is_private,
                chat_id=chat_id,
                current_text=text,
                memory_store=_state().memory_store,
                action_history=_state().action_history,
                memory_db=_state().memory_db,
                pfc_state_store=_state().pfc_state_store,
                generate_reply=_pfc_generate,
            )
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="smalltalk.pfc.done",
                fields={"action": pfc_result.action, "ended": bool(pfc_result.ended), "reason": pfc_result.reason, "reply_chars": len((pfc_result.reply or "").strip())},
            )

            reply = (pfc_result.reply or "").strip()
            if not reply:
                # 取消未使用的预取记忆任务
                if speculative_memory_task and not speculative_memory_task.done():
                    speculative_memory_task.cancel()
                _state().heartflow.on_no_reply(chat_id=chat_id)
                _state().action_history.append(
                    chat_id,
                    ActionRecord(
                        ts=time.time(),
                        local_target=local_id,
                        action="no_reply",
                        reasoning=str(pfc_result.action or "").strip() + (f":{pfc_result.reason}" if pfc_result.reason else ""),
                        detail={"source": "pfc"},
                        executed=True,
                    ),
                )
                _schedule_action_history_flush(context, runtime, chat_id=chat_id)
                return []

            assistant_local_id = _next_local_id(chat_id)
            _state().memory_store.append(chat_id, role="assistant", name=bot_name, local_id=assistant_local_id, content=reply)
            _schedule_memory_persist(context, runtime, chat_id=chat_id)
            _log_step(context, runtime, chat_id=chat_id, step="smalltalk.memory.append_bot", fields={"local_id": assistant_local_id})

            history_snapshot = _state().memory_store.get(chat_id)
            _freq_record(chat_id, runtime, _state(), forced=False)
            _state().heartflow.on_bot_reply(chat_id=chat_id)
            _state().inc_stats(chat_id, "replies")
            _state().action_history.append(
                chat_id,
                ActionRecord(
                    ts=time.time(),
                    local_target=local_id,
                    action="reply",
                    reasoning=str(pfc_result.action or "").strip() + (f":{pfc_result.reason}" if pfc_result.reason else ""),
                    detail={"source": "pfc"},
                    executed=True,
                ),
            )
            _schedule_action_history_flush(context, runtime, chat_id=chat_id)

    # 锁外调度后台任务（不阻塞其他请求）
    await _spawn_post_reply_bg_tasks(
        context,
        runtime,
        _state(),
        chat_id,
        bot_name,
        secrets,
        history_snapshot,
        event,
    )

    if runtime.cfg.debug.log_latency:
        context.logger.info("xiaoqing_chat smalltalk chat_id=%s latency=%.3fs", chat_id, time.monotonic() - t0)
    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.done",
        fields={"elapsed_s": round(time.monotonic() - t0, 3), "reply_chars": len(reply), "reply": reply},
    )

    # 深度对话模式：添加模式指示器（如果启用）
    reply = maybe_add_mode_indicator(reply, runtime)

    # 聊天模式：拆分换行符为多条消息
    parts = _split_chat_reply(reply)
    if len(parts) > 1:
        # 如果有多条消息，前N-1条通过send_action直接发送
        user_id = event.get("user_id")
        group_id = event.get("group_id")

        # 发送前N-1条消息
        for part in parts[:-1]:
            action = build_action(segments(part), user_id, group_id)
            if action:
                await context.send_action(action)

        # 只返回最后一条消息
        return segments(parts[-1])

    return segments(reply)

async def call_bot_name_only_internal(context) -> list[dict[str, Any]]:
    """
    处理只有机器人名称的内部调用
    
    返回一个随机的简短回应短语。
    
    Args:
        context: 插件上下文
    
    Returns:
        包含单个文本消息段的列表
    """
    replies = [
        "在呢",
        "嗯？",
        "怎么啦",
        "我在",
        "有事吗",
    ]
    return segments(random.choice(replies))


async def handle_internal(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    处理内部管理命令
    
    支持的命令：
    - "统计": 显示当前会话统计信息
    - "重置": 重置当前聊天的会话记忆
    
    Args:
        command: 命令名称
        args: 命令参数（当前未使用）
        event: OneBot 事件字典
        context: 插件上下文
    
    Returns:
        包含命令结果的消息段列表
    """
    try:
        chat_id = _chat_id(event)
        from .helper_utils import _load_runtime
        runtime = _load_runtime(context)
        data_dir = _get_data_dir(context)
        state = _state()
        _bind_all_stores(state, data_dir)
        
        if command == "统计":
            lines = ["📊 **会话统计**\n"]

            # 持久化数据统计
            mem_msgs = state.memory_store.get(chat_id)
            lines.append(f"• 上下文消息数: {len(mem_msgs) if mem_msgs else 0}")

            expressions = state.bw_expr_store.load()
            expr_this_chat = [e for e in expressions if e.chat_id == chat_id]
            lines.append(f"• 学到的表达 (本会话/全部): {len(expr_this_chat)}/{len(expressions)}")
            
            jargons = state.bw_jargon_store.load()
            # Jargon is global usually, but check if we track per chat counts
            jargon_count = len(jargons)
            lines.append(f"• 学到的黑话 (全部): {jargon_count}")

            recent_actions = state.action_history.get_recent(chat_id, max_items=100)
            lines.append(f"• 近期行动记录: {len(recent_actions)}")

            # 本次运行（内存中）的计数
            run_stats = state.get_stats(chat_id)
            lines.append(f"\n**本次运行 (重启后重置):**")
            lines.append(f"• 回复数: {run_stats.get('replies', 0)}")
            lines.append(f"• 重置数: {run_stats.get('resets', 0)}")

            return segments("\n".join(lines))
        
        if command == "重置":
            _state().memory_store.clear(chat_id)
            # Also clear PFC state so "ended" / "ignore" flags are reset
            pfc_st = _state().pfc_state_store.get(chat_id)
            pfc_st.ended = False
            pfc_st.ignore_until_ts = 0.0
            pfc_st.last_successful_reply_action = ""
            pfc_st.goal_list = []
            pfc_st.knowledge_list = []
            _state().pfc_state_store.save(chat_id)
            _state().inc_stats(chat_id, "resets")
            context.logger.info("XiaoQing Chat: 会话 %s 已重置", chat_id)
            return segments("✅ 已重置会话记忆")

        if command == "深度对话":
            # 切换深度对话模式
            current = runtime.cfg.brain_chat.enable_private_brain_chat
            # 这里只是显示当前配置，实际修改需要编辑配置文件
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
    
    except Exception as e:
        context.logger.exception("XiaoQing Chat internal 命令处理失败: %s", e)
        return segments(f"❌ 命令处理出错: {str(e)}")


async def handle_config(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    显示插件配置概要
    
    Args:
        args: 命令参数（当前未使用）
        event: OneBot 事件字典
        context: 插件上下文
    
    Returns:
        包含配置概要的消息段列表
    """
    try:
        from .helper_utils import _load_runtime
        runtime = _load_runtime(context)
        cfg = runtime.cfg
        
        lines = [
            "⚙️ **插件配置概要**\n",
            "**基础配置:**",
            f"• 回复概率 (群聊/私聊): {cfg.reply_probability_base:.0%} / {cfg.reply_probability_private:.0%}",
            f"• 最小回复间隔: {cfg.min_reply_interval_seconds} 秒",
            f"• 每分钟最大回复数: {cfg.max_replies_per_minute}",
            f"• 最大上下文大小: {cfg.max_context_size} 条",
            f"• LLM 温度: {cfg.temperature}",
            f"• 最大 token 数: {cfg.max_tokens}",
        ]
        
        lines.append("\n**记忆系统:**")
        lines.append(f"• 记忆检索: {'✅ 已启用' if cfg.memory.enable_memory_retrieval else '❌ 未启用'}")
        lines.append(f"• 最大检索数: {cfg.memory.top_k}")
        lines.append(f"• 最小相似度: {cfg.memory.min_score}")
        
        lines.append("\n**表达学习:**")
        lines.append(f"• 表达学习: {'✅ 已启用' if cfg.expression.enable_expression_learning else '❌ 未启用'}")
        lines.append(f"• 最大注入数: {cfg.expression.max_injected}")
        lines.append(f"• 最大存储数: {cfg.expression.max_store}")
        
        lines.append("\n**深度对话模式:**")
        brain_status = "✅ 已启用" if cfg.brain_chat.enable_private_brain_chat else "❌ 未启用"
        lines.append(f"• 状态: {brain_status}")
        if cfg.brain_chat.enable_private_brain_chat:
            lines.append(f"• 上下文大小: {cfg.brain_chat.brain_max_context_size} 条")
            lines.append(f"• 思考等级: {cfg.brain_chat.brain_think_level}")

        secrets = _get_llm_secrets(context)
        provider_name = secrets.get("_provider_name", "?")
        provider_model = secrets.get("model", "?")
        lines.append(f"\n**LLM 供应商:** {provider_name} ({provider_model})")
        
        lines.append("\n**提示:** 详细配置请查看 config/xiaoqing_config.json")
        
        return segments("\n".join(lines))
    
    except Exception as e:
        context.logger.exception("XiaoQing Chat config 命令处理失败: %s", e)
        return segments(f"❌ 配置查询出错: {str(e)}")


async def handle_memory(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    检索长期记忆
    
    Args:
        args: 搜索关键词
        event: OneBot 事件字典
        context: 插件上下文
    
    Returns:
        包含检索结果的消息段列表
    """
    try:
        query = args.strip() if args else ""
        if not query:
            return segments("🔍 **记忆检索**\n\n使用方法: /xc记忆 <关键词>\n\n示例: /xc记忆 喜欢的食物")
        
        from .helper_utils import _load_runtime
        runtime = _load_runtime(context)
        
        state = _state()
        data_dir = _get_data_dir(context)
        _bind_all_stores(state, data_dir)
        memory_db = state.memory_db
        if not memory_db:
            return segments("❌ 记忆数据库未初始化")
        
        results = memory_db.query(
            query,
            top_k=runtime.cfg.memory.top_k,
            min_score=runtime.cfg.memory.min_score
        )
        
        if not results:
            return segments(f"🔍 **记忆检索结果**\n\n关键词: {query}\n\n未找到相关记忆")
        
        lines = [f"🔍 **记忆检索结果**\n\n关键词: {query}\n"]
        for i, item in enumerate(results, 1):
            score = item.score * 100
            lines.append(f"**{i}.** (相关度: {score:.1f}%)")
            lines.append(f"{item.text}\n")
        
        return segments("\n".join(lines))
    
    except Exception as e:
        context.logger.exception("XiaoQing Chat memory 命令处理失败: %s", e)
        return segments(f"❌ 记忆检索出错: {str(e)}")


async def handle_expression(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    查看学到的表达方式
    
    Args:
        args: 命令参数（当前未使用）
        event: OneBot 事件字典
        context: 插件上下文
    
    Returns:
        包含表达方式的消息段列表
    """
    try:
        from .expression.bw_expression_store import ExpressionStore
        
        state = _state()
        data_dir = _get_data_dir(context)
        _bind_all_stores(state, data_dir)
        expression_store = state.bw_expr_store
        
        expressions = expression_store.load()
        
        if not expressions:
            return segments("💬 **表达学习**\n\n还没有学到任何表达方式\n\n继续聊天，小青会从对话中学习表达风格")
        
        # 排序：按使用次数和最近活跃时间
        expressions.sort(key=lambda x: (x.count, x.last_active_time), reverse=True)
        
        lines = ["💬 **学到的表达方式**\n"]
        lines.append(f"共 {len(expressions)} 条记录\n")
        
        # 显示前 10 条
        for i, expr in enumerate(expressions[:10], 1):
            lines.append(f"**{i}.** [{expr.style}] {expr.situation}")
            if expr.content_list:
                lines.append(f"   示例: {', '.join(expr.content_list[:3])}")
            lines.append(f"   使用次数: {expr.count}\n")
        
        if len(expressions) > 10:
            lines.append(f"\n... 还有 {len(expressions) - 10} 条记录")
        
        return segments("\n".join(lines))
    
    except Exception as e:
        context.logger.exception("XiaoQing Chat expression 命令处理失败: %s", e)
        return segments(f"❌ 表达查询出错: {str(e)}")


async def handle_jargon(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    查看学到的黑话 (Jargon)
    """
    try:
        state = _state()
        data_dir = _get_data_dir(context)
        _bind_all_stores(state, data_dir)
        jargon_store = state.bw_jargon_store
        
        jargons = jargon_store.load()
        if not jargons:
            return segments("🏴‍☠️ **黑话学习**\n\n还没有学到任何黑话\n\n继续聊天，小青会从对话中学习独特的词汇")
            
        # Sort by count
        jargon_list = sorted(jargons.values(), key=lambda x: x.count, reverse=True)
        
        lines = ["🏴‍☠️ **学到的黑话**\n"]
        lines.append(f"共 {len(jargon_list)} 条记录\n")
        
        for i, jar in enumerate(jargon_list[:15], 1):
            meaning_str = f" - {jar.meaning}" if jar.meaning else ""
            lines.append(f"**{i}.** {jar.content}{meaning_str} (次数: {jar.count})")
            
        if len(jargon_list) > 15:
            lines.append(f"\n... 还有 {len(jargon_list) - 15} 条记录")
            
        return segments("\n".join(lines))

    except Exception as e:
        context.logger.exception("XiaoQing Chat jargon 命令处理失败: %s", e)
        return segments(f"❌ 黑话查询出错: {str(e)}")


def _get_data_dir(context) -> Path:
    """
    获取数据目录
    
    优先使用插件目录下的 data 目录（如果存在且有内容），否则使用 context.data_dir
    """
    local_data = Path(__file__).parent / "data"
    if local_data.exists() and (local_data / "bw_learner").exists():
        return local_data
    return context.data_dir


async def handle_provider(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """切换 / 查看 LLM 供应商

    用法:
        /xc 模型          — 显示当前供应商和可选列表
        /xc 模型 <名称>   — 切换到指定供应商
        /xc 模型 默认     — 恢复为 secrets.json 中的默认配置
    """
    try:
        state = _state()
        secrets_base: dict[str, Any] = (
            (context.secrets or {}).get("plugins", {}).get("xiaoqing_chat", {}) or {}
        )
        providers: dict[str, Any] = secrets_base.get("providers") or {}
        default_name: str = secrets_base.get("default", "") or ""
        current = state.active_provider or default_name

        target = (args or "").strip()
        if not target:
            # --- 列表模式 ---
            lines = ["🤖 **LLM 供应商**\n"]
            for name, pcfg in providers.items():
                if not isinstance(pcfg, dict):
                    continue
                m = pcfg.get("model", "?")
                b = pcfg.get("api_base", "?")
                marker = " ✅" if current == name else ""
                lines.append(f"• **{name}** ({m} @ {_short_base(b)}){marker}")
            if not providers:
                lines.append("(未配置任何供应商)")
            lines.append(f"\n切换: /xc 模型 <名称>")
            return segments("\n".join(lines))

        # --- 切换模式 ---
        if target in ("默认", "default", "reset"):
            state.active_provider = None
            dflt = providers.get(default_name, {})
            dflt_model = dflt.get("model", "?") if isinstance(dflt, dict) else "?"
            return segments(f"✅ 已切换回默认供应商 **{default_name}** ({dflt_model})")

        if target not in providers:
            available = ", ".join(providers.keys()) if providers else "(无)"
            return segments(f"❌ 未知供应商 '{target}'\n可用: {available}")

        state.active_provider = target
        pcfg = providers[target]
        m = pcfg.get("model", "?") if isinstance(pcfg, dict) else "?"
        return segments(f"✅ 已切换到 **{target}** ({m})")

    except Exception as e:
        context.logger.exception("XiaoQing Chat provider 命令处理失败: %s", e)
        return segments(f"❌ 供应商切换出错: {str(e)}")


def _short_base(url: str) -> str:
    """Shorten an API base URL for display."""
    url = (url or "").rstrip("/")
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    parts = url.split("/")
    return parts[0] if parts else url
