"""
小青智能对话处理器
包含聊天处理、内部命令等核心功能
"""

from __future__ import annotations

import asyncio
import random
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.plugin_base import build_action, segments

from .llm.reply_checker import ReplyRejected
from .brain_chat import (
    get_brain_chat_max_context,
    is_brain_chat_active,
    maybe_add_mode_indicator,
)
from .helper_utils import (
    _chat_id,
    _extract_sender_name,
    _get_lock,
    _is_at_me,
    _has_bot_name,
    _is_private,
    _load_runtime,
    _most_recent_user_local_id,
    _next_local_id,
    _resolve_llm_config,
    _should_ignore_text,
)
from .logging_utils import _log_step
from .reply_splitter import _split_chat_reply
from .runtime_state import get_state as _state
from .store_binding import _bind_all_stores
from .task_scheduler import (
    _schedule_memory_persist,
    _spawn_bg_task,
    _schedule_pfc_state_flush,
    _schedule_action_history_flush,
)
from .context_builder import _build_memory_block
from .reply_generator import _generate_reply
from .frequency_control import _freq_record, _should_reply, _score_interest
from .media import build_effective_user_text
from .media.emoji_library import mark_emoji_used, resolve_emoji_file_path
from .media.emoji_reply import plan_emoji_reply
from .planning.goal_state import derive_goal_async
from .memory.review_sessions import get_goal_override
from .expression.bw_expression_reflector import maybe_ask_for_reflection
from .expression.bw_reflect_tracker import tick_reflect_tracker
from .planning.pfc_engine import PFCRunResult, run_pfc_once
from .planning.action_history import ActionRecord
from .planning.planned_action import PlannedAction
from .handler_context import HandlerContext, handle_errors
from .handlers_helper import _spawn_post_reply_bg_tasks
from .reply_payload import build_reply_payload


@dataclass(frozen=True)
class _PreparedSmalltalkTurn:
    text: str
    mentioned: bool
    is_private: bool
    forced: bool
    brain_chat_active: bool
    mood_text: str
    collected_emoji_count: int


@dataclass
class _GeneratedSmalltalkTurn:
    local_id: str = ""
    pfc_result: PFCRunResult | None = None
    pfc_state_snapshot: Any = None
    speculative_memory_task: Any = None
    reply: str = ""
    reply_for_send: str = ""
    reply_payload: Any = None
    emoji_plan: Any = None


async def _clear_store_entry(store, chat_id: str) -> None:
    clear_async = getattr(store, "clear_async", None)
    if callable(clear_async):
        maybe_clear = clear_async(chat_id)
        if asyncio.iscoroutine(maybe_clear):
            await maybe_clear
        return

    clear = getattr(store, "clear", None)
    if callable(clear):
        clear(chat_id)


def _clear_review_state(state, chat_id: str) -> None:
    review_store = getattr(state, "review_store", None)
    if review_store is None:
        return

    clear_policy = getattr(review_store, "clear_policy", None)
    if callable(clear_policy):
        clear_policy(chat_id)

    clear_sessions = getattr(review_store, "clear_sessions_for_chat", None)
    if callable(clear_sessions):
        clear_sessions(chat_id)


async def _reset_pfc_state(state, chat_id: str) -> None:
    pfc_st = await state.pfc_state_store.get_async(chat_id)
    pfc_st.ended = False
    pfc_st.ignore_until_ts = 0.0
    pfc_st.last_successful_reply_action = ""
    pfc_st.goal_list = []
    pfc_st.knowledge_list = []
    pfc_st.planner_fail_ts = []
    pfc_st.planner_skip_until = 0.0
    await state.pfc_state_store.save_async(chat_id)


def _reset_reply_tracking(state, chat_id: str) -> None:
    if hasattr(state, "set_continuous_reply_count"):
        state.set_continuous_reply_count(chat_id, 0)
    if hasattr(state, "set_continuous_cooldown_until"):
        state.set_continuous_cooldown_until(chat_id, 0.0)
    if hasattr(state, "set_reply_timestamps"):
        state.set_reply_timestamps(chat_id, [])
    if hasattr(state, "set_last_reply_ts"):
        state.set_last_reply_ts(chat_id, 0.0)


async def _reset_chat_session(state, chat_id: str) -> None:
    state.memory_store.clear(chat_id)
    await _clear_store_entry(state.goal_store, chat_id)
    await _clear_store_entry(state.heartflow, chat_id)
    if hasattr(state.action_history, "clear"):
        state.action_history.clear(chat_id)
    _clear_review_state(state, chat_id)
    await _reset_pfc_state(state, chat_id)
    _reset_reply_tracking(state, chat_id)


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
    hctx: HandlerContext | None = None
    max_replan = 1
    try:
        runtime = _load_runtime(context)
        max_replan = max(0, int(getattr(runtime.cfg.reply_check, "max_replan", 1)))
        hctx = HandlerContext.from_event(event, context, runtime=runtime)
    except Exception:
        hctx = None
    total_attempts = max_replan + 1

    for attempt in range(total_attempts):
        try:
            return await _maybe_reply_smalltalk(clean_text, event, context, hctx=hctx)
        except ReplyRejected as exc:
            try:
                chat_id = hctx.chat_id if hctx is not None else _chat_id(event)
                state = hctx.state if hctx is not None else _get_bound_state(context)
                state.action_history.append(
                    chat_id,
                    ActionRecord(
                        ts=time.time(),
                        local_target=str(event.get("_xc_user_recorded_local_id") or ""),
                        action="reply_rejected",
                        reasoning=str(exc),
                        detail={"source": "reply_checker", "need_replan": bool(exc.need_replan)},
                        executed=False,
                    ),
                )
                if hctx is not None:
                    _schedule_action_history_flush(context, hctx.runtime, chat_id=chat_id)
            except Exception:
                pass
            if exc.need_replan and attempt < max_replan:
                context.logger.info("XiaoQing Chat 回复被拒绝，触发重规划重试: %s", exc)
                continue
            context.logger.warning("XiaoQing Chat 回复被拒绝(已耗尽重试): %s", exc)
            return []
        except Exception as exc:
            context.logger.exception("XiaoQing Chat smalltalk 处理失败: %s", exc)
            return segments("❌ 对话处理出错，请稍后再试")
    return []


async def observe_message(clean_text: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    try:
        runtime = _load_runtime(context)
        if not runtime.cfg.enable_smalltalk:
            return []

        text = await build_effective_user_text(clean_text, event, context=context, runtime=runtime)
        if not text:
            return []
        if _should_ignore_text(text, runtime):
            return []

        chat_id = _chat_id(event)
        state = _get_bound_state(context)
        async with _get_lock(chat_id):
            await _ensure_user_message_recorded(text, event, context, runtime, state=state)
    except Exception:
        try:
            context.logger.warning("xiaoqing_chat observe_message failed", exc_info=True)
        except Exception:
            pass
        return []
    return []


async def _ensure_user_message_recorded(
    text: str,
    event: dict[str, Any],
    context,
    runtime,
    *,
    state=None,
) -> str:
    chat_id = _chat_id(event)

    if state is None:
        state = _get_bound_state(context)
    state.review_store.cleanup_expired()
    state.set_last_observe_ts(chat_id, time.time())

    msg_id = event.get("message_id")
    history = await state.memory_store.get_async(chat_id)
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
    await state.heartflow.on_user_message_async(chat_id=chat_id)
    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.memory.append_user",
        fields={"local_id": local_id},
    )
    event["_xc_user_recorded"] = True
    event["_xc_user_recorded_local_id"] = local_id
    return local_id


async def _record_bot_reply(
    context,
    runtime,
    state,
    chat_id: str,
    bot_name: str,
    reply: str,
    user_local_id: str,
    *,
    forced: bool,
    action_str: str,
    reasoning: str,
    detail: dict[str, Any],
) -> list[Any]:
    """Record assistant reply in memory and action history. Returns history snapshot.

    Must be called inside an async lock for chat_id.
    """
    assistant_local_id = _next_local_id(chat_id)
    state.memory_store.append(
        chat_id, role="assistant", name=bot_name, local_id=assistant_local_id, content=reply
    )
    _schedule_memory_persist(context, runtime, chat_id=chat_id)
    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.memory.append_bot",
        fields={"local_id": assistant_local_id},
    )
    history_snapshot = await state.memory_store.get_async(chat_id)
    _freq_record(chat_id, runtime, state, forced=forced)
    await state.heartflow.on_bot_reply_async(chat_id=chat_id)
    state.inc_stats(chat_id, "replies")
    state.action_history.append(
        chat_id,
        ActionRecord(
            ts=time.time(),
            local_target=user_local_id,
            action=action_str,
            reasoning=reasoning,
            detail=detail,
            executed=True,
        ),
    )
    _schedule_action_history_flush(context, runtime, chat_id=chat_id)
    return history_snapshot


def _emoji_action_detail(emoji_plan) -> dict[str, Any]:
    if not emoji_plan:
        return {}
    return {
        "emoji_marker": emoji_plan.marker,
        "emoji_hash": emoji_plan.entry.media_hash,
        "emoji_mode": getattr(emoji_plan, "mode", ""),
    }


def _display_reply_text(generated: _GeneratedSmalltalkTurn) -> str:
    if generated.reply_payload is not None:
        return generated.reply_payload.display_text
    return generated.reply_for_send


def _cancel_pending_task(task: Any) -> None:
    if task is not None and not task.done():
        task.cancel()


async def _prepare_smalltalk_turn(
    clean_text: str,
    event: dict[str, Any],
    context,
    hctx: HandlerContext,
) -> _PreparedSmalltalkTurn | None:
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    bot_name, secrets = hctx.bot_name, hctx.secrets

    text = await build_effective_user_text(clean_text, event, context=context, runtime=runtime)
    if _should_ignore_text(text, runtime):
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="smalltalk.ignore",
            fields={"text": text},
        )
        return None

    mentioned = _is_at_me(event) or _has_bot_name(event, bot_name)
    is_private = _is_private(event)
    command_forced = bool(event.get("_xc_command_forced"))
    collected_emoji_count = max(0, int(event.get("_xc_new_emoji_count", 0) or 0))
    forced = (
        command_forced
        or (is_private and not runtime.cfg.brain_chat.enable_private_brain_chat)
        or mentioned
        or collected_emoji_count > 0
    )

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
            "new_emoji_count": collected_emoji_count,
        },
    )

    if runtime.cfg.reflection.enable_expression_reflection:
        bg = _resolve_llm_config(runtime.cfg, secrets, foreground=False)

        async def _run_reflection() -> None:
            await tick_reflect_tracker(
                context=context,
                operator_chat_id=chat_id,
                memory_store=state.memory_store,
                expr_store=state.bw_expr_store,
                tracker_store=state.bw_tracker_store,
                secrets=secrets,
                **bg.to_dict(),
            )
            await maybe_ask_for_reflection(
                context=context,
                expr_store=state.bw_expr_store,
                tracker_store=state.bw_tracker_store,
                operator_user_id=int(runtime.cfg.reflection.operator_user_id),
                operator_group_id=int(runtime.cfg.reflection.operator_group_id),
                min_interval_seconds=float(runtime.cfg.reflection.min_interval_seconds),
                ask_per_check=int(runtime.cfg.reflection.ask_per_check),
            )

        _spawn_bg_task(context, _run_reflection(), name=f"reflection:{chat_id}")
        _log_step(context, runtime, chat_id=chat_id, step="smalltalk.reflection.spawn", fields={})

    if runtime.cfg.goal.enable_goal:
        pfc_state_before_gate = await state.pfc_state_store.get_async(chat_id)
        planner_top_goal = ""
        if not forced:
            planner_goal_list = getattr(pfc_state_before_gate, "goal_list", []) or []
            if planner_goal_list and isinstance(planner_goal_list[0], dict):
                planner_top_goal = str(planner_goal_list[0].get("goal", "") or "").strip()
        goal = ""
        goal_source = "user"
        if runtime.cfg.reflection.enable_review_sessions:
            override_goal = get_goal_override(state.review_store, chat_id)
            if override_goal:
                goal = override_goal
                goal_source = "review"
        if not goal and not planner_top_goal:
            goal = await derive_goal_async(
                data_dir=context.data_dir,
                chat_id=chat_id,
                current_text=text,
                planner_reasoning="",
            )
        if goal:
            await state.goal_store.set_async(chat_id, goal=goal, source=goal_source)
            _log_step(
                context, runtime, chat_id=chat_id, step="smalltalk.goal.set", fields={"goal": goal}
            )
        elif not planner_top_goal:
            await _clear_store_entry(state.goal_store, chat_id)
            _log_step(context, runtime, chat_id=chat_id, step="smalltalk.goal.clear", fields={})

    if not forced:
        interest = _score_interest(text)
        if not await _should_reply(
            runtime,
            state,
            chat_id,
            text,
            is_private,
            mentioned,
            runtime.cfg.brain_chat.enable_private_brain_chat,
            interest=interest,
        ):
            maybe_coro = state.heartflow.on_no_reply_async(chat_id=chat_id)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
            return None

    brain_chat_active = is_brain_chat_active(runtime, is_private, forced)
    mood_text = state.get_mood_state(chat_id)
    if mood_text:
        if runtime.cfg.personality.states and random.random() < 0.10:
            mood_text = random.choice(runtime.cfg.personality.states)
            state.set_mood_state(chat_id, mood_text, duration_seconds=1800.0)
    elif (
        runtime.cfg.personality.states
        and random.random() < runtime.cfg.personality.state_probability
    ):
        mood_text = random.choice(runtime.cfg.personality.states)
        state.set_mood_state(chat_id, mood_text, duration_seconds=1800.0)

    return _PreparedSmalltalkTurn(
        text=text,
        mentioned=mentioned,
        is_private=is_private,
        forced=forced,
        brain_chat_active=brain_chat_active,
        mood_text=mood_text,
        collected_emoji_count=collected_emoji_count,
    )


async def _generate_smalltalk_turn(
    prepared: _PreparedSmalltalkTurn,
    event: dict[str, Any],
    context,
    hctx: HandlerContext,
) -> _GeneratedSmalltalkTurn:
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    bot_name, secrets, data_dir = hctx.bot_name, hctx.secrets, hctx.data_dir
    generated = _GeneratedSmalltalkTurn()

    async with _get_lock(chat_id):
        generated.local_id = await _ensure_user_message_recorded(
            prepared.text,
            event,
            context,
            runtime,
            state=state,
        )
        if not prepared.forced:
            generated.pfc_state_snapshot = deepcopy(await state.pfc_state_store.get_async(chat_id))
            if generated.pfc_state_snapshot and generated.pfc_state_snapshot.ended:
                generated.pfc_state_snapshot.ended = False

    async def _pfc_generate(mode: str, planner_reason: str, extra_reason: str) -> str:
        style_override = ""
        if mode == "say_goodbye":
            style_override = "说一句很短很自然的告别收尾，不要延伸话题。"
        elif mode == "send_new_message":
            style_override = "你刚发过一条消息，如果要继续发一条新消息，短一点，别轰炸。"
        act = PlannedAction(
            action="reply",
            target_message_id=generated.local_id,
            think_level=runtime.cfg.planner.resolve_think_level(),
            quote=False,
            reasoning=str(planner_reason or "").strip(),
            question="",
            unknown_words=[],
        )
        plan_reasoning = (planner_reason or "").strip()
        if extra_reason:
            plan_reasoning = (plan_reasoning + "\n" + str(extra_reason).strip()).strip()
        out = await _generate_reply(
            text=prepared.text,
            event=event,
            context=context,
            runtime=runtime,
            state=state,
            forced=prepared.forced,
            action=act,
            plan_reasoning=plan_reasoning,
            bot_name=bot_name,
            secrets=secrets,
            reply_style_override=style_override,
            state_text=prepared.mood_text,
            is_brain_chat=prepared.brain_chat_active,
            prefetched_memory_task=generated.speculative_memory_task,
        )
        return out or ""

    if prepared.forced:
        _log_step(context, runtime, chat_id=chat_id, step="smalltalk.forced_direct", fields={})
        direct_act = PlannedAction(
            action="reply",
            target_message_id=generated.local_id,
            think_level=runtime.cfg.planner.resolve_think_level(),
            quote=False,
            reasoning="用户直接发起对话，需要回复",
            question="",
            unknown_words=[],
        )
        generated.reply = (
            await _generate_reply(
                text=prepared.text,
                event=event,
                context=context,
                runtime=runtime,
                state=state,
                forced=True,
                action=direct_act,
                plan_reasoning="用户直接发起对话，需要回复",
                bot_name=bot_name,
                secrets=secrets,
                state_text=prepared.mood_text,
                is_brain_chat=prepared.brain_chat_active,
            )
            or ""
        ).strip()
        if not generated.reply:
            generated.reply = random.choice(["嗯…", "行", "我在听", "你继续", "有点卡，等下"])
    else:
        max_context_size = get_brain_chat_max_context(runtime, prepared.brain_chat_active)
        speculative_history = await state.memory_store.get_recent_async(chat_id, max_items=max_context_size)
        generated.speculative_memory_task = asyncio.create_task(
            _build_memory_block(
                context=context,
                runtime=runtime,
                state=state,
                secrets=secrets,
                data_dir=data_dir,
                chat_id=chat_id,
                history=speculative_history[-max_context_size:] if max_context_size > 0 else [],
                current_text=prepared.text,
                planner_question="",
                bot_name=bot_name,
            )
        )
        generated.pfc_result = await run_pfc_once(
            context=context,
            runtime_cfg=runtime.cfg,
            secrets=secrets,
            bot_name=bot_name,
            is_private=prepared.is_private,
            chat_id=chat_id,
            current_text=prepared.text,
            memory_store=state.memory_store,
            action_history=state.action_history,
            memory_db=state.memory_db,
            pfc_state_store=state.pfc_state_store,
            generate_reply=_pfc_generate,
            state_override=generated.pfc_state_snapshot,
            persist_state=False,
        )
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="smalltalk.pfc.done",
            fields={
                "action": generated.pfc_result.action,
                "ended": bool(generated.pfc_result.ended),
                "reason": generated.pfc_result.reason,
                "reply_chars": len((generated.pfc_result.reply or "").strip()),
            },
        )
        generated.reply = (generated.pfc_result.reply or "").strip()

    if generated.reply:
        generated.reply_for_send = (
            maybe_add_mode_indicator(generated.reply, runtime)
            if prepared.brain_chat_active
            else generated.reply
        )
        media_cfg = getattr(runtime.cfg, "media", None)
        if getattr(media_cfg, "enable_outbound_emoji_reply", False):
            try:
                history_for_emoji = await state.memory_store.get_recent_async(
                    chat_id,
                    max_items=min(max(6, int(getattr(runtime.cfg, "max_context_size", 30))), 12),
                )
                generated.emoji_plan = await plan_emoji_reply(
                    context=context,
                    runtime=runtime,
                    history=history_for_emoji,
                    user_text=prepared.text,
                    reply_text=generated.reply,
                    secrets=secrets,
                )
            except Exception:
                generated.emoji_plan = None
        payload_text = generated.reply_for_send
        payload_display_text = generated.reply
        if generated.emoji_plan is not None and getattr(generated.emoji_plan, "mode", "") == "emoji_only":
            payload_text = ""
            payload_display_text = generated.emoji_plan.marker
        generated.reply_payload = build_reply_payload(
            payload_text,
            emoji_file_path=(
                str(resolve_emoji_file_path(context, generated.emoji_plan.entry.file_path))
                if generated.emoji_plan
                else ""
            ),
            emoji_marker=generated.emoji_plan.marker if generated.emoji_plan else "",
            display_text=payload_display_text,
        )

    return generated


async def _finalize_smalltalk_turn(
    prepared: _PreparedSmalltalkTurn,
    generated: _GeneratedSmalltalkTurn,
    event: dict[str, Any],
    context,
    hctx: HandlerContext,
    *,
    started_at: float,
) -> list[dict[str, Any]]:
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    bot_name = hctx.bot_name

    history_snapshot: list[Any] = []
    should_schedule_pfc_state_flush = False
    should_return_empty = False
    commit_error: Exception | None = None

    try:
        async with _get_lock(chat_id):
            if _most_recent_user_local_id(chat_id) != generated.local_id:
                _cancel_pending_task(generated.speculative_memory_task)
                return []

            if not prepared.forced and generated.pfc_state_snapshot is not None:
                state.pfc_state_store.set_state(chat_id, generated.pfc_state_snapshot)
                should_schedule_pfc_state_flush = True
                if runtime.cfg.goal.enable_goal:
                    top_goal = ""
                    goal_list = getattr(generated.pfc_state_snapshot, "goal_list", []) or []
                    if goal_list and isinstance(goal_list[0], dict):
                        top_goal = str(goal_list[0].get("goal", "") or "").strip()
                    if top_goal:
                        await state.goal_store.set_async(chat_id, goal=top_goal, source="planner")
                    else:
                        await _clear_store_entry(state.goal_store, chat_id)

            if prepared.forced:
                history_snapshot = await _record_bot_reply(
                    context,
                    runtime,
                    state,
                    chat_id,
                    bot_name,
                    generated.reply_payload.display_text if generated.reply_payload else generated.reply,
                    generated.local_id,
                    forced=True,
                    action_str="reply",
                    reasoning="forced_direct",
                    detail={"source": "forced", **_emoji_action_detail(generated.emoji_plan)},
                )
            else:
                assert generated.pfc_result is not None
                if not generated.reply:
                    _cancel_pending_task(generated.speculative_memory_task)
                    await state.heartflow.on_no_reply_async(chat_id=chat_id)
                    state.action_history.append(
                        chat_id,
                        ActionRecord(
                            ts=time.time(),
                            local_target=generated.local_id,
                            action=str(generated.pfc_result.action or "no_reply").strip() or "no_reply",
                            reasoning=str(generated.pfc_result.action or "").strip()
                            + (
                                f":{generated.pfc_result.reason}"
                                if generated.pfc_result.reason
                                else ""
                            ),
                            detail={"source": "pfc"},
                            executed=True,
                        ),
                    )
                    _schedule_action_history_flush(context, runtime, chat_id=chat_id)
                    should_return_empty = True
                else:
                    pfc_reasoning = str(generated.pfc_result.action or "").strip()
                    if generated.pfc_result.reason:
                        pfc_reasoning += f":{generated.pfc_result.reason}"
                    history_snapshot = await _record_bot_reply(
                        context,
                        runtime,
                        state,
                        chat_id,
                        bot_name,
                        generated.reply_payload.display_text if generated.reply_payload else generated.reply,
                        generated.local_id,
                        forced=False,
                        action_str=str(generated.pfc_result.action or "reply").strip() or "reply",
                        reasoning=pfc_reasoning,
                        detail={"source": "pfc", **_emoji_action_detail(generated.emoji_plan)},
                    )
    except Exception as exc:
        commit_error = exc

    if should_schedule_pfc_state_flush:
        _schedule_pfc_state_flush(context, runtime, chat_id=chat_id)

    if commit_error is not None:
        raise commit_error

    if should_return_empty:
        return []

    await _spawn_post_reply_bg_tasks(hctx, history_snapshot, event)

    if runtime.cfg.debug.log_latency:
        context.logger.info(
            "xiaoqing_chat smalltalk chat_id=%s latency=%.3fs", chat_id, time.monotonic() - started_at
        )
    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.done",
        fields={
            "elapsed_s": round(time.monotonic() - started_at, 3),
            "reply_chars": len(_display_reply_text(generated)),
            "reply": _display_reply_text(generated),
        },
    )

    outbound_batches = (
        generated.reply_payload.outbound_batches
        if generated.reply_payload
        else segments(generated.reply_for_send)
    )
    if outbound_batches and isinstance(outbound_batches[0], dict):
        outbound_batches = [outbound_batches]

    if len(outbound_batches) > 1:
        user_id = event.get("user_id")
        group_id = event.get("group_id")
        send_all_batches = generated.emoji_plan is not None
        pending_batches = outbound_batches if send_all_batches else outbound_batches[:-1]
        for batch in pending_batches:
            action = build_action(batch, user_id, group_id)
            if action:
                await context.send_action(action)

        if generated.emoji_plan is not None:
            try:
                mark_emoji_used(context, runtime, generated.emoji_plan.entry)
            except Exception:
                pass
            return []

        return outbound_batches[-1]

    if generated.emoji_plan is not None and getattr(generated.emoji_plan, "mode", "") == "emoji_only":
        try:
            mark_emoji_used(context, runtime, generated.emoji_plan.entry)
        except Exception:
            pass

    return outbound_batches[0] if outbound_batches else []


async def _maybe_reply_smalltalk(
    clean_text: str,
    event: dict[str, Any],
    context,
    *,
    hctx: HandlerContext | None = None,
) -> list[dict[str, Any]]:
    """
    核心闲聊处理逻辑，基于 PFC 规划和回复生成

    该函数协调整个闲聊流程：
    1. 验证配置并忽略禁用文本
    2. 将用户消息存储到记忆中
    3. 如果启用则运行反思任务
    4. 根据频率规则检查是否应该发送回复
    5. 在锁外运行 PFC 规划和回复生成
    6. 在短临界区内存储机器人的回复
    7. 为摘要和表达学习生成后台任务

    Args:
        clean_text: 清理后的用户消息文本
        event: OneBot 事件字典
        context: 插件上下文

    Returns:
        回复的消息段字典列表，或空列表
    """
    hctx = hctx or HandlerContext.from_event(event, context)
    runtime = hctx.runtime

    if not runtime.cfg.enable_smalltalk:
        return []

    t0 = time.monotonic()
    prepared = await _prepare_smalltalk_turn(clean_text, event, context, hctx)
    if prepared is None:
        return []

    generated = await _generate_smalltalk_turn(prepared, event, context, hctx)
    return await _finalize_smalltalk_turn(
        prepared,
        generated,
        event,
        context,
        hctx,
        started_at=t0,
    )


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


@handle_errors("命令处理")
async def handle_internal(
    command: str, args: str, event: dict[str, Any], context
) -> list[dict[str, Any]]:
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
    hctx = HandlerContext.from_event(event, context)
    chat_id, runtime, state = hctx.chat_id, hctx.runtime, hctx.state

    if command == "统计":
        lines = ["📊 **会话统计**\n"]

        # 持久化数据统计
        mem_msgs = await state.memory_store.get_async(chat_id)
        lines.append(f"• 上下文消息数: {len(mem_msgs) if mem_msgs else 0}")

        expressions = state.bw_expr_store.load()
        expr_this_chat = [e for e in expressions if e.chat_id == chat_id]
        lines.append(f"• 学到的表达 (本会话/全部): {len(expr_this_chat)}/{len(expressions)}")

        jargons = state.bw_jargon_store.load()
        jargon_count = len(jargons)
        lines.append(f"• 学到的黑话 (全部): {jargon_count}")

        recent_actions = await state.action_history.get_recent_async(chat_id, max_items=100)
        lines.append(f"• 近期行动记录: {len(recent_actions)}")

        # 本次运行（内存中）的计数
        run_stats = state.get_stats(chat_id)
        lines.append(f"\n**本次运行 (重启后重置):**")
        lines.append(f"• 回复数: {run_stats.get('replies', 0)}")
        lines.append(f"• 重置数: {run_stats.get('resets', 0)}")

        return segments("\n".join(lines))

    if command == "重置":
        async with _get_lock(chat_id):
            await _reset_chat_session(state, chat_id)
        state.inc_stats(chat_id, "resets")
        context.logger.info("XiaoQing Chat: 会话 %s 已重置", chat_id)
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


@handle_errors("配置查询")
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
    hctx = HandlerContext.from_event(event, context)
    runtime, secrets = hctx.runtime, hctx.secrets
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


@handle_errors("记忆检索")
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
    query = args.strip() if args else ""
    if not query:
        return segments("🔍 **记忆检索**\n\n使用方法: /xc记忆 <关键词>\n\n示例: /xc记忆 喜欢的食物")

    hctx = HandlerContext.from_event(event, context)
    runtime, state = hctx.runtime, hctx.state
    memory_db = state.memory_db
    if not memory_db:
        return segments("❌ 记忆数据库未初始化")

    results = memory_db.query(
        query, top_k=runtime.cfg.memory.top_k, min_score=runtime.cfg.memory.min_score
    )

    if not results:
        return segments(f"🔍 **记忆检索结果**\n\n关键词: {query}\n\n未找到相关记忆")

    lines = [f"🔍 **记忆检索结果**\n\n关键词: {query}\n"]
    for i, item in enumerate(results, 1):
        score = item.score * 100
        lines.append(f"**{i}.** (相关度: {score:.1f}%)")
        lines.append(f"{item.text}\n")

    return segments("\n".join(lines))


@handle_errors("表达查询")
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
    hctx = HandlerContext.from_event(event, context)
    state = hctx.state
    expression_store = state.bw_expr_store

    expressions = expression_store.load()

    if not expressions:
        return segments(
            "💬 **表达学习**\n\n还没有学到任何表达方式\n\n继续聊天，小青会从对话中学习表达风格"
        )

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


@handle_errors("黑话查询")
async def handle_jargon(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """
    查看学到的黑话 (Jargon)
    """
    hctx = HandlerContext.from_event(event, context)
    state = hctx.state
    jargon_store = state.bw_jargon_store

    jargons = jargon_store.load()
    if not jargons:
        return segments(
            "🏴‍☠️ **黑话学习**\n\n还没有学到任何黑话\n\n继续聊天，小青会从对话中学习独特的词汇"
        )

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


def _get_data_dir(context) -> Path:
    """Thin wrapper kept for test-patching compatibility."""
    return context.data_dir


def _get_bound_state(context):
    state = _state()
    _bind_all_stores(state, _get_data_dir(context))
    return state


def _is_admin_operator(event: dict[str, Any], context) -> bool:
    user_id = event.get("user_id")
    group_id = event.get("group_id")

    if context and hasattr(context, "is_admin"):
        try:
            if context.is_admin(user_id, group_id):
                return True
        except Exception:
            pass

    uid = str(user_id)

    def _contains_user(candidate_ids: Any) -> bool:
        if not isinstance(candidate_ids, (list, tuple, set)):
            return False
        return any(str(admin_id) == uid for admin_id in candidate_ids)

    if context and hasattr(context, "admin_ids"):
        if _contains_user(getattr(context, "admin_ids", [])):
            return True

    if context and hasattr(context, "secrets"):
        secrets = getattr(context, "secrets", {}) or {}
        if _contains_user(secrets.get("admin_user_ids", [])):
            return True

    if context and hasattr(context, "check_permission"):
        try:
            if context.check_permission(user_id, "admin"):
                return True
        except Exception:
            pass

    return False


@handle_errors("供应商切换")
async def handle_provider(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """切换 / 查看 LLM 供应商

    用法:
        /xc 模型          — 显示当前供应商和可选列表
        /xc 模型 <名称>   — 切换到指定供应商
        /xc 模型 默认     — 恢复为 secrets.json 中的默认配置
    """
    state = _state()
    secrets_base: dict[str, Any] = (context.secrets or {}).get("plugins", {}).get(
        "xiaoqing_chat", {}
    ) or {}
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

    if not _is_admin_operator(event, context):
        return segments("❌ 仅管理员可切换 LLM 供应商")

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


def _short_base(url: str) -> str:
    """Shorten an API base URL for display."""
    url = (url or "").rstrip("/")
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix) :]
    parts = url.split("/")
    return parts[0] if parts else url
