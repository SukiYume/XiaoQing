"""
小青智能对话处理器
包含聊天处理、内部命令等核心功能
"""

from __future__ import annotations

import asyncio
import inspect
import random
import re
import time
from pathlib import Path
from typing import Any

from core.interfaces import ACTION_RESULT_MESSAGE_ID_KEY
from core.message import extract_text
from core.plugin_base import segments
from core.public_errors import public_error_message, public_error_response

from .attention_gate import decide_attention
from .brain_chat import (
    is_brain_chat_active,
    maybe_add_mode_indicator,
)
from .context_builder import _build_memory_block
from .expression.bw_expression_reflector import maybe_ask_for_reflection
from .expression.bw_reflect_tracker import tick_reflect_tracker
from .frequency_control import _freq_record, _should_reply
from .generation_limiter import GenerationLimitExceeded
from .handler_context import HandlerContext, handle_errors
from .handlers_helper import _spawn_post_reply_bg_tasks
from .handlers_internal import (
    get_bound_state as _get_bound_state_impl,
)
from .handlers_internal import (
    handle_config_impl,
    handle_expression_impl,
    handle_internal_impl,
    handle_jargon_impl,
    handle_memory_impl,
    handle_provider_impl,
    handle_review_impl,
)
from .handlers_internal import (
    is_admin_operator as _is_admin_operator,
)
from .handlers_internal import (
    is_global_admin_operator as _is_global_admin_operator,
)
from .helper_utils import (
    _chat_id,
    _extract_sender_name,
    _get_bot_name,
    _get_lock,
    _has_bot_name,
    _is_at_me,
    _is_private,
    _load_runtime,
    _most_recent_user_local_id,
    _next_local_id,
    _resolve_llm_config,
    _should_ignore_text,
)
from .llm.reply_checker import ReplyRejected
from .logging_utils import _log_step
from .media.event_media import build_effective_user_text
from .memory.fact_extraction_checkpoint import clear_fact_extraction_checkpoint
from .memory.memory import idle_gap_before_turn
from .memory.review_sessions import get_goal_override
from .memory.thinking_back import clear_records as clear_thinking_back_records
from .memory.topic_summary_cache import clear_topic_summary_entries
from .message_parts import (
    build_message_parts,
    build_text_message_parts,
    normalize_message_parts,
)
from .planning.action_history import ActionRecord
from .planning.goal_state import derive_goal_async, is_low_information_turn
from .planning.pfc_engine import run_pfc_once
from .reply_generator import _generate_reply_draft
from .reply_payload import build_reply_payload_from_parts
from .runtime_state import get_state as _state
from .smalltalk_execution import (
    finalize_smalltalk_turn_impl,
    generate_smalltalk_turn_impl,
)
from .smalltalk_media_helpers import (
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
from .store_binding import _bind_all_stores
from .task_scheduler import (
    _schedule_action_history_flush,
    _schedule_media_registry_flush,
    _schedule_memory_persist,
    _schedule_pfc_state_flush,
    _spawn_bg_task,
)


def _refresh_mood_state(runtime, state, chat_id: str) -> str:
    """挑选/复用 personality state.

    规则：
    - 已有未过期 mood，且最近活跃过 → 直接沿用，不再随机重摇。
    - 长时间静默（超过 state_force_refresh_after_idle_seconds）→ 视作"睡了一觉"，
      强制走重新挑 state 的流程。
    - 没有可用 mood → 按 state_probability 摇是否进入某个 state；
      命中后随机一个时长（在 [min,max] 之间），写入 state 缓存。
    """
    cfg = runtime.cfg.personality
    states = list(cfg.states)
    if not states:
        return ""

    now = time.time()
    last_observe = float(state.get_last_observe_ts(chat_id) or 0.0)
    last_reply = float(state.get_last_reply_ts(chat_id) or 0.0)
    last_active = max(last_observe, last_reply)
    idle_threshold = max(0.0, cfg.state_force_refresh_after_idle_seconds)

    current = state.get_mood_state(chat_id)
    if current and idle_threshold > 0 and last_active and (now - last_active) > idle_threshold:
        # 静默太久，强制让下面的逻辑走"重新决定"
        current = ""

    if current:
        return current

    if random.random() >= cfg.state_probability:
        return ""

    new_mood = random.choice(states)
    min_d = max(60.0, cfg.state_min_duration_seconds)
    max_d = max(min_d, cfg.state_max_duration_seconds)
    duration = random.uniform(min_d, max_d)
    state.set_mood_state(chat_id, new_mood, duration_seconds=duration)
    return new_mood


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
        for item in (cfg.noisy_external_source_plugins if cfg is not None else ())
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


def _reset_reply_tracking(state, chat_id: str) -> None:
    clear_transient = getattr(state, "clear_transient_chat_state", None)
    if callable(clear_transient):
        clear_transient(chat_id)
        return
    if hasattr(state, "set_continuous_reply_count"):
        state.set_continuous_reply_count(chat_id, 0)
    if hasattr(state, "set_continuous_cooldown_until"):
        state.set_continuous_cooldown_until(chat_id, 0.0)
    if hasattr(state, "set_reply_timestamps"):
        state.set_reply_timestamps(chat_id, [])
    if hasattr(state, "set_last_reply_ts"):
        state.set_last_reply_ts(chat_id, 0.0)


async def _reset_chat_session(state, chat_id: str, data_dir: Path) -> None:
    """清除会话私有状态；媒体注册表按 media_key 全局共享，不能按 chat_id 删除。"""

    # clear() 需要等待已经进入 asyncio.to_thread()、无法取消的持久化任务，
    # 因此把这段等待也放到事件循环之外。
    await asyncio.to_thread(state.memory_store.clear, chat_id)
    memory_db = getattr(state, "memory_db", None)
    delete_chat = getattr(memory_db, "delete_chat", None)
    if callable(delete_chat):
        deleted = await asyncio.to_thread(delete_chat, chat_id)
        save = getattr(memory_db, "save", None)
        if deleted and callable(save):
            # save() 自带串行保存锁；即使旧的防抖保存已经进入线程，
            # 这里的最终保存也会在其后提交删除后的最新快照。
            await asyncio.to_thread(save)
    await _clear_store_entry(state.goal_store, chat_id)
    await _clear_store_entry(state.heartflow, chat_id)
    if hasattr(state.action_history, "clear"):
        await asyncio.to_thread(state.action_history.clear, chat_id)
    await asyncio.to_thread(_clear_review_state, state, chat_id)
    await _clear_store_entry(state.pfc_state_store, chat_id)
    await asyncio.to_thread(clear_thinking_back_records, data_dir=data_dir, chat_id=chat_id)
    await asyncio.to_thread(clear_topic_summary_entries, data_dir, chat_id)
    await asyncio.to_thread(clear_fact_extraction_checkpoint, data_dir, chat_id)
    await _clear_store_entry(state.bw_expr_store, chat_id)
    await _clear_store_entry(state.bw_jargon_store, chat_id)
    await _clear_store_entry(state.bw_recorder, chat_id)
    await _clear_store_entry(state.bw_tracker_store, chat_id)
    _reset_reply_tracking(state, chat_id)


async def _reset_transient_conversation_state(state, chat_id: str, data_dir: Path) -> None:
    """长空档后清掉旧话题的短期状态，但保留可显式检索的完整聊天历史。"""

    await _clear_store_entry(state.goal_store, chat_id)
    await _clear_store_entry(state.heartflow, chat_id)
    if hasattr(state.action_history, "clear"):
        await asyncio.to_thread(state.action_history.clear, chat_id)
    await _clear_store_entry(state.pfc_state_store, chat_id)
    await asyncio.to_thread(clear_thinking_back_records, data_dir=data_dir, chat_id=chat_id)
    await asyncio.to_thread(clear_topic_summary_entries, data_dir, chat_id)
    _reset_reply_tracking(state, chat_id)


async def _maybe_reset_idle_conversation(
    event: dict[str, Any],
    context,
    runtime,
    state,
    *,
    chat_id: str,
) -> float:
    """识别本轮前的长时间空档，并且每个事件最多重置一次。"""

    if event.get("_xc_idle_context_checked"):
        return 0.0
    event["_xc_idle_context_checked"] = True

    memory_cfg = getattr(runtime.cfg, "memory", None)
    threshold = max(
        0.0,
        float(getattr(memory_cfg, "conversation_idle_gap_seconds", 1800.0) or 0.0),
    )
    if threshold <= 0:
        return 0.0

    getter = getattr(state.memory_store, "get_recent_async", None)
    if not callable(getter):
        return 0.0
    pending_history = getter(chat_id, max_items=2)
    if not inspect.isawaitable(pending_history):
        return 0.0
    history = await pending_history
    if not isinstance(history, (list, tuple)):
        return 0.0
    idle_gap = idle_gap_before_turn(
        history,
        current_local_id=str(event.get("_xc_user_recorded_local_id") or ""),
    )
    if idle_gap <= threshold:
        return 0.0

    await _reset_transient_conversation_state(state, chat_id, context.data_dir)
    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.context.reset_idle",
        fields={
            "idle_seconds": round(idle_gap, 3),
            "threshold_seconds": round(threshold, 3),
        },
    )
    return idle_gap


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
        max_replan = max(0, runtime.cfg.reply_check.max_replan)
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
                context.logger.info(
                    "XiaoQing Chat 回复被拒绝，触发重规划重试 rejection_type=%s",
                    type(exc).__name__,
                )
                continue
            context.logger.warning(
                "XiaoQing Chat 回复被拒绝(已耗尽重试) rejection_type=%s",
                type(exc).__name__,
            )
            return []
        except Exception as exc:
            return public_error_response(
                context,
                exc,
                logger=context.logger,
                component="xiaoqing_chat.smalltalk",
            )
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

    config = context.get_settings_snapshot().config
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
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="xiaoqing_chat.observe_message",
        )
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


def _event_message_timestamp(event: dict[str, Any]) -> float | None:
    """采用可信 OneBot 秒级时间；明显无效或来自未来的值回退到本机时钟。"""

    raw = event.get("time")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        timestamp = float(raw)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0 or timestamp > time.time() + 300.0:
        return None
    return timestamp


async def observe_outgoing_action(
    action: dict[str, Any],
    context,
    *,
    source_plugin: str = "",
) -> list[dict[str, Any]]:
    """把其他插件以小青身份发送的文本记入对话上下文。"""
    try:
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
            result_message_id = action.get(ACTION_RESULT_MESSAGE_ID_KEY)
            if str(source_plugin or "").strip() == "xiaoqing_chat":
                if state.memory_store.attach_latest_assistant_message_id(
                    chat_id, result_message_id
                ):
                    _schedule_memory_persist(context, runtime, chat_id=chat_id)
                return []
            local_id = _next_local_id(chat_id)
            state.memory_store.append(
                chat_id,
                role="assistant",
                name=_get_bot_name(context),
                message_id=result_message_id,
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
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="xiaoqing_chat.observe_outgoing",
        )
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
        ts=_event_message_timestamp(event),
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
    """将助手回复写入记忆和动作历史，并返回最新历史快照。

    调用方必须已经持有 ``chat_id`` 对应的异步锁。
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

    async with _get_lock(chat_id):
        await _maybe_reset_idle_conversation(
            event,
            context,
            runtime,
            state,
            chat_id=chat_id,
        )

    direct_mentioned = _is_at_me(event) or _has_bot_name(event, bot_name)
    is_private = _is_private(event)
    command_forced = bool(event.get("_xc_command_forced"))
    collected_emoji_count = max(0, int(event.get("_xc_new_emoji_count", 0) or 0))
    pending_bot_name_forced = False
    if (
        not command_forced
        and not (is_private and not runtime.cfg.brain_chat.enable_private_brain_chat)
        and not direct_mentioned
    ):
        pending_bot_name_forced = _consume_pending_bot_name_call(
            state, chat_id, _coerce_int_or_none(event.get("user_id"))
        )
    attention = await decide_attention(
        text=text,
        event=event,
        state=state,
        chat_id=chat_id,
        bot_name=bot_name,
        is_private=is_private,
        command_forced=command_forced,
        direct_mentioned=direct_mentioned,
        pending_bot_name_forced=pending_bot_name_forced,
        enable_private_brain_chat=runtime.cfg.brain_chat.enable_private_brain_chat,
    )
    mentioned = attention.mentioned
    forced = attention.forced
    force_reason = attention.force_reason

    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.recv",
        fields={
            "is_private": is_private,
            "mentioned": mentioned,
            "direct_mentioned": direct_mentioned,
            "coreference_mentioned": attention.coreference_mentioned,
            "reply_to_bot": attention.reply_to_bot,
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

    reply_gate_allowed = True
    if not forced:
        reply_gate_allowed = await _should_reply(
            runtime,
            state,
            chat_id,
            text,
            is_private,
            runtime.cfg.brain_chat.enable_private_brain_chat,
        )

    # 先用上一轮状态完成回复门控，再记录本轮观察到的新目标。这样当前消息不会先把
    # 自己标成“活跃话题”，同时即使本轮不回复，后续仍能看到真正的新话题。
    if runtime.cfg.goal.enable_goal:
        pfc_state_before_gate = await state.pfc_state_store.get_async(chat_id)
        planner_top_goal = ""
        if not forced:
            planner_goal_list = getattr(pfc_state_before_gate, "goal_list", []) or []
            if planner_goal_list and isinstance(planner_goal_list[0], dict):
                planner_top_goal = str(planner_goal_list[0].get("goal", "") or "").strip()
        goal = ""
        goal_source = "user"
        preserve_existing_goal = is_low_information_turn(text)
        if runtime.cfg.reflection.enable_review_sessions:
            override_goal = get_goal_override(state.review_store, chat_id)
            if override_goal:
                goal = override_goal
                goal_source = "review"
        if not goal and not planner_top_goal and not preserve_existing_goal:
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
        elif not planner_top_goal and not preserve_existing_goal:
            await _clear_store_entry(state.goal_store, chat_id)
            _log_step(context, runtime, chat_id=chat_id, step="smalltalk.goal.clear", fields={})
        elif preserve_existing_goal:
            _log_step(context, runtime, chat_id=chat_id, step="smalltalk.goal.keep", fields={})

    if not forced:
        if not reply_gate_allowed:
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
        bg = _resolve_llm_config(runtime.cfg, foreground=False)

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

    brain_chat_active = is_brain_chat_active(runtime, is_private)
    mood_text = _refresh_mood_state(runtime, state, chat_id)

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
    """协调闲聊门控、限流、生成和投递确认。

    准备阶段先重建有效输入并完成 attention/频率门控；进入生成配额后才确保用户消息
    已记录，并在锁外执行 PFC 或直接回复。机器人状态只在外发成功后提交，摘要和表达
    学习等后台任务也只在投递确认后启动。
    """
    hctx = hctx or HandlerContext.from_event(event, context)
    runtime = hctx.runtime

    if not runtime.cfg.enable_smalltalk:
        return []

    t0 = time.monotonic()
    prepared = await _prepare_smalltalk_turn(clean_text, event, context, hctx)
    if prepared is None:
        return []

    user_scope = str(event.get("user_id") or "anonymous")
    try:
        async with hctx.state.generation_limiter.admit(
            chat_id=hctx.chat_id,
            user_id=user_scope,
            max_global=max(0, runtime.cfg.max_generation_inflight_global),
            max_per_chat=max(0, runtime.cfg.max_generation_inflight_per_chat),
            max_per_user=max(0, runtime.cfg.max_generation_inflight_per_user),
            max_calls_per_user_per_day=max(0, runtime.cfg.max_generation_calls_per_user_per_day),
        ):
            generated = await _generate_smalltalk_turn(prepared, event, context, hctx)
            return await _finalize_smalltalk_turn(
                prepared,
                generated,
                event,
                context,
                hctx,
                started_at=t0,
            )
    except GenerationLimitExceeded as exc:
        _log_step(
            context,
            runtime,
            chat_id=hctx.chat_id,
            step="smalltalk.generation_limited",
            fields={"limit": str(exc), "forced": prepared.forced},
        )
        return segments("⏳ 当前请求较多，请稍后再试") if prepared.forced else []


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
    runtime = None
    try:
        runtime = _load_runtime(context)
        configured = [
            str(item).strip() for item in runtime.cfg.bot_name_only_replies if str(item).strip()
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
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="smalltalk.bot_name_only",
            fields={
                "user_id": user_id,
                "followup_ttl_s": _BOT_NAME_ONLY_FOLLOWUP_TTL_SECONDS,
            },
        )
    return segments(random.choice(replies))


@handle_errors
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
        is_admin_operator_fn=_is_admin_operator,
    )


@handle_errors
async def handle_config(_args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_config_impl(
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


@handle_errors
async def handle_memory(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_memory_impl(
        args,
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


@handle_errors
async def handle_expression(_args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_expression_impl(
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


@handle_errors
async def handle_jargon(_args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_jargon_impl(
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
    )


@handle_errors
async def handle_review(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_review_impl(
        args,
        event,
        context,
        handler_context_from_event=HandlerContext.from_event,
        is_admin_operator_fn=_is_admin_operator,
    )


def _get_bound_state(context):
    return _get_bound_state_impl(
        context,
        state_loader=_state,
        bind_all_stores=_bind_all_stores,
    )


@handle_errors
async def handle_provider(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    return await handle_provider_impl(
        args,
        event,
        context,
        state_getter=_state,
        chat_id_from_event=_chat_id,
        is_admin_operator_fn=_is_admin_operator,
        is_global_admin_operator_fn=_is_global_admin_operator,
    )
