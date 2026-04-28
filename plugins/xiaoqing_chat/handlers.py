"""
小青智能对话处理器
包含聊天处理、内部命令等核心功能
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from core.message import extract_text
from core.plugin_base import segments

from .llm.reply_checker import ReplyRejected
from .brain_chat import (
    is_brain_chat_active,
    maybe_add_mode_indicator,
)
from .helper_utils import (
    _chat_id,
    _extract_sender_name,
    _get_lock,
    _get_bot_name,
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
from .message_parts import (
    build_message_parts,
    build_text_message_parts,
    normalize_message_parts,
)
from .runtime_state import get_state as _state
from .store_binding import _bind_all_stores
from .task_scheduler import (
    _schedule_memory_persist,
    _spawn_bg_task,
    _schedule_media_registry_flush,
    _schedule_pfc_state_flush,
    _schedule_action_history_flush,
)
from .context_builder import _build_memory_block
from .reply_generator import _generate_reply_draft
from .frequency_control import _freq_record, _should_reply
from .media import build_effective_user_text
from .planning.goal_state import derive_goal_async
from .memory.review_sessions import get_goal_override
from .expression.bw_expression_reflector import maybe_ask_for_reflection
from .expression.bw_reflect_tracker import tick_reflect_tracker
from .planning.pfc_engine import run_pfc_once
from .planning.action_history import ActionRecord
from .handler_context import HandlerContext, handle_errors
from .handlers_helper import _spawn_post_reply_bg_tasks
from .handlers_internal import (
    get_bound_state as _get_bound_state_impl,
    get_data_dir as _get_data_dir_impl,
    handle_config_impl,
    handle_expression_impl,
    handle_internal_impl,
    handle_jargon_impl,
    handle_memory_impl,
    handle_provider_impl,
    is_admin_operator as _is_admin_operator_impl,
    short_base as _short_base_impl,
)
from .reply_payload import build_reply_payload_from_parts
from .smalltalk_execution import (
    finalize_smalltalk_turn_impl,
    generate_smalltalk_turn_impl,
)
from .smalltalk_media_helpers import (
    _assistant_reply_parts,
    _display_reply_text,
    _event_media_items_for_memory,
    _mark_reply_media_used,
    _media_action_detail,
    _normalize_generated_reply_state,
    _prefix_reply_parts,
    _reply_send_prefix,
    _sync_message_parts_to_registry,
)
from .smalltalk_models import _GeneratedSmalltalkTurn, _PreparedSmalltalkTurn, _ReplyEnvelope


_SENSITIVE_EXTERNAL_TEXT_RE = re.compile(
    r"(?:"
    r"登录\s*token|web\s*token|api[_-]?key|secret|set[_-]?secret|password|密码|authkey|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"
    r")",
    re.IGNORECASE,
)

_BOT_NAME_ONLY_FOLLOWUP_TTL_SECONDS = 60.0
_DEFAULT_BOT_NAME_ONLY_REPLIES = ("在呢", "嗯？", "怎么啦", "我在", "有事吗")


def _should_skip_external_bot_memory(text: str, source_plugin: str, cfg: Any | None = None) -> bool:
    source = str(source_plugin or "").strip().lower()
    noisy_plugins = {
        str(item or "").strip().lower()
        for item in getattr(cfg, "noisy_external_source_plugins", []) or []
        if str(item or "").strip()
    }
    if source in noisy_plugins:
        return True
    compact = str(text or "").strip()
    if not compact:
        return True
    if len(compact) > 1000:
        return True
    return bool(_SENSITIVE_EXTERNAL_TEXT_RE.search(compact))


def _context_chat_and_user_id(context) -> tuple[str, int | None]:
    user_id = _coerce_int_or_none(getattr(context, "current_user_id", None))
    group_id = _coerce_int_or_none(getattr(context, "current_group_id", None))
    if group_id is not None:
        return f"g{group_id}", user_id
    if user_id is not None:
        return f"u{user_id}", user_id
    return "", user_id


def _coerce_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _state_method(state, name: str):
    if hasattr(type(state), name) or name in getattr(state, "__dict__", {}):
        method = getattr(state, name, None)
        if callable(method):
            return method
    return None


def _consume_pending_bot_name_call(state, chat_id: str, user_id: int | None) -> bool:
    method = _state_method(state, "consume_pending_bot_name_call")
    if method is None:
        return False
    try:
        return bool(method(chat_id, user_id))
    except Exception:
        return False


def _last_reply_gate_log_fields(state, chat_id: str) -> dict[str, Any]:
    method = _state_method(state, "get_reply_gate_decision")
    if method is None:
        return {}
    try:
        decision = method(chat_id)
    except Exception:
        return {}
    as_log_fields = getattr(decision, "as_log_fields", None)
    if callable(as_log_fields):
        try:
            fields = as_log_fields()
            return dict(fields) if isinstance(fields, dict) else {}
        except Exception:
            return {}
    return {}


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


def _command_token(text: str) -> str:
    stripped = str(text or "").strip()
    return stripped.split(maxsplit=1)[0].lower() if stripped else ""


def _is_prefixed_xc_command_observation(clean_text: str, event: dict[str, Any], context) -> bool:
    if _command_token(clean_text) == "xc":
        return True

    raw_text = extract_text(event.get("message")).strip()
    if not raw_text:
        raw_text = str(event.get("raw_message", "") or "").strip()
    if not raw_text:
        return False

    config = getattr(context, "config", {}) or {}
    prefixes = tuple(config.get("command_prefixes", ["/"]) or ["/"])
    for prefix in prefixes:
        prefix_text = str(prefix)
        if not prefix_text or not raw_text.startswith(prefix_text):
            continue
        remainder = raw_text[len(prefix_text) :].lstrip()
        if _command_token(remainder) == "xc":
            return True
    return False


async def observe_message(clean_text: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    try:
        runtime = _load_runtime(context)
        if not runtime.cfg.enable_smalltalk:
            return []

        if _is_prefixed_xc_command_observation(clean_text, event, context):
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


def _outgoing_action_chat_event(action: dict[str, Any]) -> dict[str, Any] | None:
    act = str(action.get("action", "") or "").strip()
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    if act == "send_group_msg":
        group_id = params.get("group_id")
        if group_id in (None, ""):
            return None
        return {"group_id": group_id, "user_id": params.get("user_id")}
    if act == "send_private_msg":
        user_id = params.get("user_id")
        if user_id in (None, ""):
            return None
        return {"user_id": user_id}
    return None


def _outgoing_action_text(action: dict[str, Any]) -> str:
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    message = params.get("message")
    return extract_text(message).strip()


async def observe_outgoing_action(
    action: dict[str, Any],
    context,
    *,
    source_plugin: str = "",
) -> list[dict[str, Any]]:
    """Record text sent by other plugins as XiaoQing's own dialogue context."""
    try:
        if str(source_plugin or "").strip() == "xiaoqing_chat":
            return []

        runtime = _load_runtime(context)
        if not runtime.cfg.enable_smalltalk:
            return []

        event = _outgoing_action_chat_event(action)
        if event is None:
            return []

        text = _outgoing_action_text(action)
        if _should_ignore_text(text, runtime):
            return []
        if _should_skip_external_bot_memory(text, source_plugin, runtime.cfg):
            return []

        chat_id = _chat_id(event)
        state = _get_bound_state(context)
        async with _get_lock(chat_id):
            local_id = _next_local_id(chat_id)
            state.memory_store.append(
                chat_id,
                role="assistant",
                name=_get_bot_name(context),
                local_id=local_id,
                parts=build_text_message_parts(text),
            )
            _schedule_memory_persist(context, runtime, chat_id=chat_id)
            await state.heartflow.on_bot_reply_async(chat_id=chat_id)
            state.action_history.append(
                chat_id,
                ActionRecord(
                    ts=time.time(),
                    local_target="",
                    action="external_plugin_message",
                    reasoning=f"source_plugin={str(source_plugin or '').strip() or '-'}",
                    detail={"source_plugin": str(source_plugin or "").strip(), "text": text[:500]},
                    executed=True,
                ),
            )
            _schedule_action_history_flush(context, runtime, chat_id=chat_id)
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="smalltalk.memory.append_external_bot",
                fields={"local_id": local_id, "source_plugin": str(source_plugin or "").strip()},
            )
    except Exception:
        try:
            context.logger.warning("xiaoqing_chat observe_outgoing_action failed", exc_info=True)
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
    cached_effective_parts = normalize_message_parts(event.get("_xc_effective_user_parts"))
    if cached_effective_parts:
        message_parts = _sync_message_parts_to_registry(
            state,
            cached_effective_parts,
            context=context,
            runtime=runtime,
            schedule_media_registry_flush=_schedule_media_registry_flush,
        )
    else:
        message_parts = _sync_message_parts_to_registry(
            state,
            build_message_parts(
                text,
                await _event_media_items_for_memory(event, context=context, runtime=runtime),
                store=getattr(state, "media_store", None),
            ),
            context=context,
            runtime=runtime,
            schedule_media_registry_flush=_schedule_media_registry_flush,
        )
    state.memory_store.append(
        chat_id,
        role="user",
        name=_extract_sender_name(event),
        user_id=event.get("user_id"),
        message_id=msg_id,
        local_id=local_id,
        parts=message_parts,
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
    user_local_id: str,
    *,
    forced: bool,
    action_str: str,
    reasoning: str,
    detail: dict[str, Any],
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Record assistant reply in memory and action history. Returns history snapshot.

    Must be called inside an async lock for chat_id.
    """
    assistant_local_id = _next_local_id(chat_id)
    state.memory_store.append(
        chat_id,
        role="assistant",
        name=bot_name,
        local_id=assistant_local_id,
        parts=parts,
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

def _build_generated_reply_output(
    context,
    runtime,
    generated: _GeneratedSmalltalkTurn,
    *,
    brain_chat_active: bool,
    display_parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
) -> _ReplyEnvelope | None:
    normalized_display_parts = normalize_message_parts(display_parts)
    if not generated.reply and not normalized_display_parts:
        return None

    reply_for_send = (
        maybe_add_mode_indicator(generated.reply, runtime) if brain_chat_active else generated.reply
    )
    send_parts = _prefix_reply_parts(
        normalized_display_parts,
        _reply_send_prefix(generated.reply, reply_for_send),
    )
    payload = build_reply_payload_from_parts(
        send_parts,
        display_parts=normalized_display_parts,
    )
    return _ReplyEnvelope(
        text=reply_for_send,
        display_parts=normalized_display_parts,
        send_parts=send_parts,
        payload=payload,
    )

def _cancel_pending_task(task: Any) -> None:
    if task is not None and not task.done():
        task.cancel()


def _cancel_generated_tasks(generated: _GeneratedSmalltalkTurn) -> None:
    _cancel_pending_task(generated.speculative_memory_task)


async def _generate_reply_result(**kwargs) -> tuple[str, tuple[dict[str, Any], ...], Any]:
    draft = await _generate_reply_draft(**kwargs)
    if draft is None:
        return "", (), None
    reply_text = str(getattr(draft, "text", "") or "").strip()
    reply_parts = normalize_message_parts(getattr(draft, "parts", ()) or ())
    if reply_text and not reply_parts:
        reply_parts = build_text_message_parts(reply_text)
    return reply_text, reply_parts, getattr(draft, "media_marker", None)


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
    pending_bot_name_forced = False
    forced = False
    force_reason = ""
    if command_forced:
        forced = True
        force_reason = "command"
    elif is_private and not runtime.cfg.brain_chat.enable_private_brain_chat:
        forced = True
        force_reason = "private"
    elif mentioned:
        forced = True
        force_reason = "mentioned"
    else:
        pending_bot_name_forced = _consume_pending_bot_name_call(
            state, chat_id, _coerce_int_or_none(event.get("user_id"))
        )
        if pending_bot_name_forced:
            forced = True
            force_reason = "bot_name_followup"

    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.recv",
        fields={
            "is_private": is_private,
            "mentioned": mentioned,
            "forced": forced,
            "force_reason": force_reason,
            "pending_bot_name": pending_bot_name_forced,
            "brain_chat_enabled": runtime.cfg.brain_chat.enable_private_brain_chat,
            "msg_id": event.get("message_id"),
            "user_id": event.get("user_id"),
            "group_id": event.get("group_id"),
            "text": text,
            "new_emoji_count": collected_emoji_count,
        },
    )

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
        if not await _should_reply(
            runtime,
            state,
            chat_id,
            text,
            is_private,
            mentioned,
            runtime.cfg.brain_chat.enable_private_brain_chat,
        ):
            gate_fields = _last_reply_gate_log_fields(state, chat_id)
            gate_fields.update({"text": text})
            if "reason" not in gate_fields:
                gate_fields["reason"] = "reply_gate"
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="smalltalk.no_reply",
                fields=gate_fields,
            )
            maybe_coro = state.heartflow.on_no_reply_async(chat_id=chat_id)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
            return None

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
        force_reason=force_reason,
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
    return await generate_smalltalk_turn_impl(
        prepared,
        event,
        context,
        hctx,
        generated_turn_factory=_GeneratedSmalltalkTurn,
        ensure_user_message_recorded=_ensure_user_message_recorded,
        get_lock=_get_lock,
        generate_reply_result=_generate_reply_result,
        build_memory_block=_build_memory_block,
        run_pfc_once=run_pfc_once,
        normalize_generated_reply_state=_normalize_generated_reply_state,
        cancel_generated_tasks=_cancel_generated_tasks,
        schedule_pfc_state_flush=_schedule_pfc_state_flush,
        clear_store_entry=_clear_store_entry,
        log_step=_log_step,
        build_text_message_parts=build_text_message_parts,
    )


async def _finalize_smalltalk_turn(
    prepared: _PreparedSmalltalkTurn,
    generated: _GeneratedSmalltalkTurn,
    event: dict[str, Any],
    context,
    hctx: HandlerContext,
    *,
    started_at: float,
) -> list[dict[str, Any]]:
    return await finalize_smalltalk_turn_impl(
        prepared,
        generated,
        event,
        context,
        hctx,
        started_at=started_at,
        get_lock=_get_lock,
        most_recent_user_local_id=_most_recent_user_local_id,
        cancel_generated_tasks=_cancel_generated_tasks,
        assistant_reply_parts=_assistant_reply_parts,
        build_generated_reply_output=_build_generated_reply_output,
        sync_message_parts_to_registry=_sync_message_parts_to_registry,
        schedule_media_registry_flush=_schedule_media_registry_flush,
        clear_store_entry=_clear_store_entry,
        record_bot_reply=_record_bot_reply,
        media_action_detail=_media_action_detail,
        schedule_pfc_state_flush=_schedule_pfc_state_flush,
        schedule_action_history_flush=_schedule_action_history_flush,
        spawn_bg_task=_spawn_bg_task,
        spawn_post_reply_bg_tasks=_spawn_post_reply_bg_tasks,
        display_reply_text=_display_reply_text,
        mark_reply_media_used=_mark_reply_media_used,
        log_step=_log_step,
    )


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
    replies = list(_DEFAULT_BOT_NAME_ONLY_REPLIES)
    try:
        runtime = _load_runtime(context)
        configured = [
            str(item).strip()
            for item in getattr(runtime.cfg, "bot_name_only_replies", []) or []
            if str(item).strip()
        ]
        if configured:
            replies = configured
    except Exception:
        pass
    chat_id, user_id = _context_chat_and_user_id(context)
    if chat_id:
        _state().set_pending_bot_name_call(
            chat_id,
            user_id,
            ttl_seconds=_BOT_NAME_ONLY_FOLLOWUP_TTL_SECONDS,
        )
        try:
            payload = {
                "step": "smalltalk.bot_name_only",
                "chat_id": chat_id,
                "user_id": user_id,
                "followup_ttl_s": _BOT_NAME_ONLY_FOLLOWUP_TTL_SECONDS,
            }
            context.logger.info("xiaoqing_chat step=%s", json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
    return segments(random.choice(replies))


@handle_errors("命令处理")
async def handle_internal(
    command: str, args: str, event: dict[str, Any], context
) -> list[dict[str, Any]]:
    return await handle_internal_impl(
        command,
        args,
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
        get_lock=_get_lock,
        reset_chat_session=_reset_chat_session,
        cancel_pending_task=_cancel_pending_task,
    )


@handle_errors("配置查询")
async def handle_config(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_config_impl(
        args,
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


@handle_errors("记忆检索")
async def handle_memory(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_memory_impl(
        args,
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


@handle_errors("表达查询")
async def handle_expression(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_expression_impl(
        args,
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


@handle_errors("黑话查询")
async def handle_jargon(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_jargon_impl(
        args,
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


def _get_data_dir(context) -> Path:
    return _get_data_dir_impl(context)


def _get_bound_state(context):
    return _get_bound_state_impl(
        context,
        state_loader=_state,
        bind_all_stores=_bind_all_stores,
    )


def _is_admin_operator(event: dict[str, Any], context) -> bool:
    return _is_admin_operator_impl(event, context)


@handle_errors("供应商切换")
async def handle_provider(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_provider_impl(
        args,
        event,
        context,
        state_getter=_state,
        is_admin_operator_fn=_is_admin_operator,
        short_base_fn=_short_base,
    )


def _short_base(url: str) -> str:
    return _short_base_impl(url)
