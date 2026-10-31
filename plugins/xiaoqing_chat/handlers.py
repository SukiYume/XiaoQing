"""
拟人智能对话处理器
包含聊天处理、内部命令等核心功能
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, cast

from core.delivery import SENSITIVE_ACTION_KEY
from core.interfaces import ACTION_RESULT_MESSAGE_ID_KEY
from core.message import extract_text
from core.plugin_base import segments
from core.public_errors import public_error_message, public_error_response

from .agent_bridge import (
    AGENT_PLUGIN,
    MAX_AGENT_MEMORY_CHARS,
    agent_may_take_over,
    answer_with_agent_or_chat,
)
from .attention_gate import decide_attention
from .brain_chat import (
    is_brain_chat_active,
)
from .chat_event import (
    EVERYONE_MENTION_ID,
    EVERYONE_MENTION_LABEL,
    MENTION_LABELS_KEY,
    event_chat_id,
    is_bot_mentioned,
    is_private_event,
    mentions_bot_name,
    other_mentions,
    text_with_mentions,
)
from .chat_runtime import (
    ai_route_context,
    chat_lock,
    configured_bot_name,
    is_ignored_text,
    llm_call_config,
    load_runtime,
)
from .commands import reset_transient_conversation_state
from .config.config import DEFAULT_BOT_NAME_ONLY_REPLIES
from .expression.bw_expression_reflector import maybe_ask_for_reflection
from .expression.bw_reflect_tracker import tick_reflect_tracker
from .frequency_control import decide_reply
from .generation_limiter import CHAT_SUPERSEDED, GenerationLimitExceeded
from .handler_context import HandlerContext
from .life.closing_guard import should_stay_silent
from .life.daily_life import day_key, ensure_today
from .life.schedule import delay_factor, participation_factor, routine_slot_now
from .life.schedule import state_text as routine_state_text
from .llm.check_result import ReplyRejected
from .llm.communication_constraints import requests_silence
from .logging_utils import log_step
from .media.event_media import build_effective_user_text
from .memory.local_ids import allocate_local_id
from .memory.memory import idle_gap_before_turn
from .memory.review_sessions import get_goal_override
from .message_parts import (
    build_text_message_parts,
)
from .planning.action_history import ActionRecord
from .planning.goal_state import derive_goal_async, is_low_information_turn
from .post_reply_tasks import spawn_life_extraction
from .runtime_state import SELF_MOOD_KEY
from .runtime_state import get_state as _state
from .smalltalk_models import PreparedSmalltalkTurn
from .store_binding import bound_state
from .task_scheduler import (
    schedule_action_history_flush,
    schedule_memory_persist,
    spawn_bg_task,
)
from .turn_delivery import finalize_smalltalk_turn
from .turn_generation import ensure_user_message_recorded, generate_smalltalk_turn
from .utils.wall_clock import local_datetime

# 会话状态、重置与长空档判定


_SLEEPY_MOOD_RE                    = re.compile(r"困|睡|迷糊")
_QUIET_MOOD_RE                     = re.compile(r"听|话少|轻一点|困")
_last_mood_check: dict[str, float] = {}


def _refresh_mood_state(runtime, state, _chat_id: str, *, availability: str = "free") -> str:
    """挑选或复用角色此刻的心境。

    规则：
    - 心境全局一份，不随会话变化；已有未过期的直接沿用，不再随机重摇。
    - 超过 state_force_refresh_after_idle_seconds 没有任何会话来过消息 → 视作"睡了一觉"，
      重新挑。
    - 作息是睡觉时换成犯困的心境（人设卡里有的话）；上课等忙碌时段偏向安静的心境。
    - 没有可用心境 → 按 state_probability 摇是否进入某个心境；命中后随机一个时长。
    """
    cfg               = runtime.cfg.personality
    states: list[str] = list(cfg.card.moods)
    if not states:
        return ""

    now                             = time.time()
    last_check                      = _last_mood_check.get(SELF_MOOD_KEY, 0.0)
    idle_threshold                  = max(0.0, cfg.state_force_refresh_after_idle_seconds)
    _last_mood_check[SELF_MOOD_KEY] = now

    current_value = state.get_mood_state(SELF_MOOD_KEY)
    current       = current_value if isinstance(current_value, str) else ""
    if current and idle_threshold > 0 and last_check and (now - last_check) > idle_threshold:
        # 静默太久，强制让下面的逻辑走"重新决定"
        current = ""
    sleepy = [mood for mood in states if _SLEEPY_MOOD_RE.search(mood)]
    if availability == "asleep" and sleepy and current not in sleepy:
        current = ""

    if current:
        return current

    if availability == "asleep" and sleepy:
        candidates = sleepy
    else:
        if random.random() >= cfg.state_probability:
            return ""
        quiet      = [mood for mood in states if _QUIET_MOOD_RE.search(mood)]
        candidates = quiet if availability == "busy" and quiet and random.random() < 0.5 else states

    new_mood = random.choice(candidates)
    min_d    = max(60.0, cfg.state_min_duration_seconds)
    max_d    = max(min_d, cfg.state_max_duration_seconds)
    duration = random.uniform(min_d, max_d)
    state.set_mood_state(SELF_MOOD_KEY, new_mood, duration_seconds=duration)
    return new_mood


_SENSITIVE_EXTERNAL_TEXT_RE = re.compile(
    r"(?:"
    r"登录\s*token|web\s*token|api[_-]?key|secret|set[_-]?secret|password|密码|authkey|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"
    r")",
    re.IGNORECASE,
)

# 其他插件的长推送（APOD、arXiv）只记开头一段：知道群里刚推送过什么，又不让长篇正文刷满上下文。
_MAX_EXTERNAL_MEMORY_CHARS = 1000
_EXTERNAL_HEADLINE_CHARS   = 160

_BOT_NAME_ONLY_FOLLOWUP_TTL_SECONDS = 60.0
# 同一会话已有一轮在生成时，新一轮最多排队这么久；旧一轮看到新消息会在下一个检查点退出。
_CHAT_GENERATION_QUEUE_SECONDS = 20.0


def _should_skip_external_bot_memory(text: str, source_plugin: str, cfg: Any | None = None) -> bool:
    source        = str(source_plugin or "").strip().lower()
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
    if len(compact) > _MAX_EXTERNAL_MEMORY_CHARS:
        return True
    return bool(_SENSITIVE_EXTERNAL_TEXT_RE.search(compact))


def _external_headline(text: str) -> str:
    """长推送的开头一段：标题加正文起头，够知道推送的是什么，后面的长篇不记。"""

    compact = " ".join(str(text or "").split())
    return compact[:_EXTERNAL_HEADLINE_CHARS].rstrip() + "…（内容较长，只记下开头）"


def _context_chat_and_user_id(context) -> tuple[str, int | None]:
    user_id  = _coerce_int_or_none(getattr(context, "current_user_id", None))
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
    except (TypeError, ValueError, OverflowError):
        return None


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

    threshold = max(0.0, float(runtime.cfg.memory.conversation_idle_gap_seconds))
    if threshold <= 0:
        return 0.0

    history = await state.memory_store.get_recent_async(chat_id, max_items=2)
    idle_gap = float(
        idle_gap_before_turn(
            history,
            current_local_id=str(event.get("_xc_user_recorded_local_id") or ""),
        )
    )
    if idle_gap <= threshold:
        return 0.0

    await reset_transient_conversation_state(state, chat_id, context.data_dir)
    log_step(
        context,
        runtime,
        chat_id = chat_id,
        step    = "smalltalk.context.reset_idle",
        fields  = {
            "idle_seconds"     : round(idle_gap, 3),
            "threshold_seconds": round(threshold, 3),
        },
    )
    return idle_gap


# 入站聊天与外部出站动作观察


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
    max_replan                  = 1
    try:
        runtime    = load_runtime(context)
        max_replan = max(0, runtime.cfg.reply_check.max_replan)
        hctx = HandlerContext.from_event(event, context, runtime=runtime)
    except Exception as exc:
        context.logger.debug(
            "xiaoqing_chat failed to prepare handler context error_type=%s",
            type(exc).__name__,
        )
        hctx = None
    total_attempts = max_replan + 1

    for attempt in range(total_attempts):
        try:
            return await _maybe_reply_smalltalk(clean_text, event, context, hctx=hctx)
        except ReplyRejected as exc:
            try:
                chat_id = hctx.chat_id if hctx is not None else event_chat_id(event)
                state   = hctx.state if hctx is not None else bound_state(context)
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
                    schedule_action_history_flush(context, hctx.runtime, chat_id=chat_id)
            except Exception as history_exc:
                context.logger.debug(
                    "xiaoqing_chat failed to record rejected reply error_type=%s",
                    type(history_exc).__name__,
                )
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
                logger    = context.logger,
                component = "xiaoqing_chat.smalltalk",
            )
    return []


def _chat_text_with_addressee(clean_text: str, event: dict[str, Any], bot_name: str) -> str:
    """恢复 Core 为命令路由剥掉的句首称呼，聊天历史才能分清这句话是对谁说的。"""

    text = str(clean_text or "").strip()
    if event.get(MENTION_LABELS_KEY):
        text = text_with_mentions(event) or text
    if not text or not bot_name or text.startswith(bot_name):
        return text
    raw_text = (
        extract_text(event.get("message")).strip()
        or str(event.get("raw_message", "") or "").strip()
    )
    if raw_text.startswith(bot_name) or is_bot_mentioned(event):
        return f"{bot_name}，{text}"
    return text


async def _label_other_mentions(event: dict[str, Any], state, chat_id: str) -> None:
    """给本轮 @ 的其他成员取展示名：优先用段内名字，其次用群里最近的发言名。"""

    if MENTION_LABELS_KEY in event:
        return
    mentions = other_mentions(event)
    if not mentions:
        event[MENTION_LABELS_KEY] = {}
        return
    known: dict[str, str] = {}
    if any(not name and target != EVERYONE_MENTION_ID for target, name in mentions):
        for message in await state.memory_store.get_recent_async(chat_id, max_items=200):
            if message.role == "user" and message.user_id is not None and message.name:
                known[str(message.user_id)] = message.name
    labels: dict[str, str] = {}
    for target, name in mentions:
        if target == EVERYONE_MENTION_ID:
            labels[target] = EVERYONE_MENTION_LABEL
        else:
            labels[target] = f"@{name or known.get(target) or f'用户{target}'}"
    event[MENTION_LABELS_KEY] = labels


async def _chat_text_for_turn(
    clean_text: str, event: dict[str, Any], bot_name: str, state, chat_id: str
) -> str:
    await _label_other_mentions(event, state, chat_id)
    return _chat_text_with_addressee(clean_text, event, bot_name)


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

    config   = context.get_settings_snapshot().config
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
        runtime = load_runtime(context)
        if not runtime.cfg.enable_smalltalk:
            return []

        if _is_prefixed_xc_command_observation(clean_text, event, context):
            return []

        chat_id = event_chat_id(event)
        state   = bound_state(context)
        text    = await build_effective_user_text(
            await _chat_text_for_turn(
                clean_text, event, configured_bot_name(context), state, chat_id
            ),
            event,
            context = context,
            runtime = runtime,
        )
        if not text:
            return []
        if is_ignored_text(text, runtime):
            return []

        async with chat_lock(chat_id):
            await ensure_user_message_recorded(text, event, context, runtime, state=state)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "xiaoqing_chat.observe_message",
        )
        return []
    return []


def _outgoing_action_params(action: dict[str, Any]) -> dict[str, Any]:
    """提取 OneBot 动作参数；畸形参数统一按空对象处理。"""

    raw_params = action.get("params")
    if not isinstance(raw_params, dict):
        return {}
    return cast(dict[str, Any], raw_params)


def _outgoing_action_chat_event(action: dict[str, Any]) -> dict[str, Any] | None:
    act    = str(action.get("action", "") or "").strip()
    params = _outgoing_action_params(action)
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
    params  = _outgoing_action_params(action)
    message = params.get("message")
    return extract_text(message).strip()


async def observe_outgoing_action(
    action: dict[str, Any],
    context,
    *,
    source_plugin: str = "",
) -> list[dict[str, Any]]:
    """把其他插件以小青身份发送的文本记入对话上下文。"""
    if action.get(SENSITIVE_ACTION_KEY):
        return []
    try:
        runtime = load_runtime(context)
        if not runtime.cfg.enable_smalltalk:
            return []

        event = _outgoing_action_chat_event(action)
        if event is None:
            return []
        if str(source_plugin or "").strip() == "xiaoqing_chat":
            # 聊天自己的回复在发送回执提交时已经连同消息 ID 记下；审查提示等其他外发不进对话。
            return []

        text = _outgoing_action_text(action)
        if str(source_plugin or "").strip() == AGENT_PLUGIN:
            # 群友常常只引用结果接着说；记不下这条，就认不出那是在跟小青说话。
            text = text[:MAX_AGENT_MEMORY_CHARS]
        elif len(
            text.strip()
        ) > _MAX_EXTERNAL_MEMORY_CHARS and not _SENSITIVE_EXTERNAL_TEXT_RE.search(text):
            text = _external_headline(text)
        if is_ignored_text(text, runtime):
            return []
        if _should_skip_external_bot_memory(text, source_plugin, runtime.cfg):
            return []

        chat_id = event_chat_id(event)
        state   = bound_state(context)
        async with chat_lock(chat_id):
            result_message_id = action.get(ACTION_RESULT_MESSAGE_ID_KEY)
            local_id          = allocate_local_id(chat_id)
            state.memory_store.append(
                chat_id,
                role          = "assistant",
                name          = configured_bot_name(context),
                message_id    = result_message_id,
                local_id      = local_id,
                parts         = build_text_message_parts(text),
                source_plugin = str(source_plugin or "").strip() or "external",
            )
            schedule_memory_persist(context, runtime, chat_id=chat_id)
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
            schedule_action_history_flush(context, runtime, chat_id=chat_id)
            log_step(
                context,
                runtime,
                chat_id = chat_id,
                step    = "smalltalk.memory.append_external_bot",
                fields  = {"local_id": local_id, "source_plugin": str(source_plugin or "").strip()},
            )
            history = (
                list(await state.memory_store.get_recent_async(chat_id, max_items=12))
                if str(source_plugin or "").strip() == AGENT_PLUGIN
                else []
            )
        if history:
            # agent 的后台结果是小青自己交付的：答应过的事据此结掉，新的说法也记下来。
            spawn_life_extraction(
                context,
                runtime,
                state,
                chat_id  = chat_id,
                bot_name = configured_bot_name(context),
                secrets=ai_route_context(context, chat_id=chat_id),
                history = history,
                source  = "agent",
            )
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "xiaoqing_chat.observe_outgoing",
        )
        return []
    return []


# 单轮生成、记忆提交与投递编排


async def _resolve_attention(text: str, event: dict[str, Any], context, hctx: HandlerContext):
    """判断本轮是否在叫机器人、是否必须回复，并记录接收日志。"""

    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    brain_chat_enabled = runtime.cfg.brain_chat.enable_private_brain_chat
    direct_mentioned   = is_bot_mentioned(event) or mentions_bot_name(event, hctx.bot_name)
    is_private         = is_private_event(event)
    command_forced     = bool(event.get("_xc_command_forced"))
    # 上一条只喊了名字时，紧跟的这句话视为在对机器人说。
    pending_bot_name_forced = (
        not command_forced
        and not (is_private and not brain_chat_enabled)
        and not direct_mentioned
        and state.consume_pending_bot_name_call(chat_id, _coerce_int_or_none(event.get("user_id")))
    )
    attention = await decide_attention(
        text                      = text,
        event                     = event,
        state                     = state,
        chat_id                   = chat_id,
        bot_name                  = hctx.bot_name,
        is_private                = is_private,
        command_forced            = command_forced,
        direct_mentioned          = direct_mentioned,
        pending_bot_name_forced   = pending_bot_name_forced,
        enable_private_brain_chat = brain_chat_enabled,
    )
    log_step(
        context,
        runtime,
        chat_id = chat_id,
        step    = "smalltalk.recv",
        fields  = {
            "is_private"           : is_private,
            "mentioned"            : attention.mentioned,
            "direct_mentioned"     : direct_mentioned,
            "coreference_mentioned": attention.coreference_mentioned,
            "reply_to_bot"         : attention.reply_to_bot,
            "forced"               : attention.forced,
            "force_reason"         : attention.force_reason,
            "pending_bot_name"     : pending_bot_name_forced,
            "brain_chat_enabled"   : brain_chat_enabled,
            "msg_id"               : event.get("message_id"),
            "user_id"              : event.get("user_id"),
            "group_id"             : event.get("group_id"),
            "text"                 : text,
            "new_emoji_count"      : max(0, int(event.get("_xc_new_emoji_count", 0) or 0)),
        },
    )
    return attention, is_private


async def _update_observed_goal(context, hctx: HandlerContext, text: str, *, forced: bool) -> None:
    """记录本轮观察到的会话目标；规划器已有目标或本轮信息量太低时保持原目标。"""

    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    pfc_state        = await state.pfc_state_store.get_async(chat_id)
    planner_top_goal = "" if forced else pfc_state.top_goal
    preserve         = is_low_information_turn(text)
    goal, source = "", "user"
    if runtime.cfg.reflection.enable_review_sessions:
        override = get_goal_override(state.review_store, chat_id)
        if override:
            goal, source = override, "review"
    if not goal and not planner_top_goal and not preserve:
        goal = await derive_goal_async(
            data_dir=context.data_dir, chat_id=chat_id, current_text=text, planner_reasoning=""
        )
    if goal:
        await state.goal_store.set_async(chat_id, goal=goal, source=source)
        log_step(
            context, runtime, chat_id=chat_id, step="smalltalk.goal.set", fields={"goal": goal}
        )
    elif preserve:
        log_step(context, runtime, chat_id=chat_id, step="smalltalk.goal.keep", fields={})
    elif not planner_top_goal:
        await asyncio.to_thread(state.goal_store.clear, chat_id)
        log_step(context, runtime, chat_id=chat_id, step="smalltalk.goal.clear", fields={})


async def _log_reply_gate_skip(context, hctx: HandlerContext, text: str) -> None:
    decision = hctx.state.get_reply_gate_decision(hctx.chat_id)
    fields   = decision.as_log_fields() if decision is not None else {}
    fields.setdefault("reason", "reply_gate")
    fields["text"] = text
    log_step(context, hctx.runtime, chat_id=hctx.chat_id, step="smalltalk.no_reply", fields=fields)
    await hctx.state.heartflow.on_no_reply_async(chat_id=hctx.chat_id)


def _spawn_expression_reflection(context, hctx: HandlerContext) -> None:
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    reflection = runtime.cfg.reflection
    bg = llm_call_config(runtime.cfg, foreground=False)

    async def _run_reflection() -> None:
        await tick_reflect_tracker(
            operator_chat_id = chat_id,
            memory_store     = state.memory_store,
            expr_store       = state.bw_expr_store,
            tracker_store    = state.bw_tracker_store,
            secrets          = hctx.secrets,
            bot_name         = hctx.bot_name,
            **bg.request_controls(),
        )
        await maybe_ask_for_reflection(
            context              = context,
            expr_store           = state.bw_expr_store,
            tracker_store        = state.bw_tracker_store,
            operator_user_id     = int(reflection.operator_user_id),
            operator_group_id    = int(reflection.operator_group_id),
            min_interval_seconds = float(reflection.min_interval_seconds),
            ask_per_check        = int(reflection.ask_per_check),
        )

    spawn_bg_task(context, _run_reflection(), name=f"reflection:{chat_id}")
    log_step(context, runtime, chat_id=chat_id, step="smalltalk.reflection.spawn", fields={})


async def _private_closing_settled(text: str, event: dict[str, Any], hctx: HandlerContext) -> bool:
    """私聊里对方只回了句收尾，而角色已经收过尾：这一轮不接话，避免互道晚安停不下来。"""

    life = hctx.runtime.cfg.life
    if not life.enable_private_closing_guard or event.get("_xc_command_forced"):
        return False
    history = await hctx.state.memory_store.get_recent_async(hctx.chat_id, max_items=8)
    return should_stay_silent(
        text,
        history,
        now                = time.time(),
        window_seconds     = float(life.closing_guard_window_seconds),
        current_message_id = event.get("message_id"),
    )


def spawn_daily_life(context, runtime, state, secrets: dict[str, Any], *, bot_name: str) -> None:
    """今天的生活小事还没安排时在后台安排一次；早上的定时任务通常已经做过。"""

    if not runtime.cfg.life.enable_daily_life:
        return
    if state.daily_life.get(day_key(local_datetime())) is not None:
        return
    card = runtime.cfg.personality.card
    spawn_bg_task(
        context,
        ensure_today(
            store   = state.daily_life,
            persona = card.facts(bot_name),
            routine = card.routine,
            secrets = secrets,
            llm=llm_call_config(runtime.cfg, foreground=False),
        ),
        name="daily_life:today",
    )


def _apply_routine(event: dict[str, Any], hctx: HandlerContext) -> float:
    """按人设卡〔作息〕记下此刻的作息底色和延迟乘数，返回普通群聊插话概率的乘数。"""

    life = hctx.runtime.cfg.life
    if not life.enable_schedule_state:
        return 1.0
    try:
        slot = routine_slot_now(hctx.runtime.cfg.personality.card, local_datetime())
    except ValueError:
        return 1.0
    event["_xc_routine_text"]         = routine_state_text(slot)
    event["_xc_routine_availability"] = slot.availability if slot is not None else "free"
    if not life.schedule_affects_pacing:
        return 1.0
    event["_xc_delay_factor"] = delay_factor(
        slot, busy=life.busy_delay_factor, asleep=life.asleep_delay_factor
    )
    return participation_factor(
        slot, busy=life.busy_participation_factor, asleep=life.asleep_participation_factor
    )


async def _prepare_smalltalk_turn(
    clean_text: str,
    event: dict[str, Any],
    context,
    hctx: HandlerContext,
) -> PreparedSmalltalkTurn | None:
    """重建有效输入并完成召唤判断与回复门控；本轮不该回复时返回 None。"""

    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    text = await build_effective_user_text(
        await _chat_text_for_turn(clean_text, event, hctx.bot_name, state, chat_id),
        event,
        context = context,
        runtime = runtime,
    )
    if requests_silence(text):
        log_step(context, runtime, chat_id=chat_id, step="smalltalk.explicit_silence")
        return None
    if is_ignored_text(text, runtime):
        log_step(context, runtime, chat_id=chat_id, step="smalltalk.ignore", fields={"text": text})
        return None

    async with chat_lock(chat_id):
        await _maybe_reset_idle_conversation(event, context, runtime, state, chat_id=chat_id)

    attention, is_private = await _resolve_attention(text, event, context, hctx)
    if is_private and await _private_closing_settled(text, event, hctx):
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "smalltalk.closing_silence",
            fields  = {"text": text},
        )
        return None
    routine_factor = _apply_routine(event, hctx)
    spawn_daily_life(context, runtime, state, hctx.secrets, bot_name=hctx.bot_name)
    reply_gate_allowed = attention.forced or await decide_reply(
        runtime,
        state,
        chat_id,
        text,
        is_private,
        runtime.cfg.brain_chat.enable_private_brain_chat,
        routine_factor=routine_factor,
    )
    # 先用上一轮状态完成回复门控，再记录本轮观察到的新目标。这样当前消息不会先把
    # 自己标成“活跃话题”，同时即使本轮不回复，后续仍能看到真正的新话题。
    if runtime.cfg.goal.enable_goal:
        await _update_observed_goal(context, hctx, text, forced=attention.forced)
    if not reply_gate_allowed:
        await _log_reply_gate_skip(context, hctx, text)
        return None
    if runtime.cfg.reflection.enable_expression_reflection:
        _spawn_expression_reflection(context, hctx)

    return PreparedSmalltalkTurn(
        text              = text,
        mentioned         = attention.mentioned,
        is_private        = is_private,
        forced            = attention.forced,
        force_reason      = attention.force_reason,
        brain_chat_active = is_brain_chat_active(runtime, is_private),
        mood_text         = _refresh_mood_state(
            runtime,
            state,
            chat_id,
            availability=str(event.get("_xc_routine_availability") or "free"),
        ),
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
    hctx    = hctx or HandlerContext.from_event(event, context)
    runtime = hctx.runtime

    if not runtime.cfg.enable_smalltalk:
        return []

    t0       = time.monotonic()
    prepared = await _prepare_smalltalk_turn(clean_text, event, context, hctx)
    if prepared is None:
        return []

    async def chat(deciding: asyncio.Task[Any] | None) -> list[dict[str, Any]]:
        return await _reply_with_chat(
            prepared, event, context, hctx, started_at=t0, deciding=deciding
        )

    # 明确叫小青时让 agent 判断要不要动手，同时照常生成聊天回复；它不接手就用聊天回复。
    if agent_may_take_over(prepared, event, context):
        return await answer_with_agent_or_chat(prepared, event, context, hctx, chat)
    return await chat(None)


async def _reply_with_chat(
    prepared: PreparedSmalltalkTurn,
    event: dict[str, Any],
    context,
    hctx: HandlerContext,
    *,
    started_at: float,
    deciding: asyncio.Task[Any] | None,
) -> list[dict[str, Any]]:
    """在生成名额内生成并投递聊天回复；``deciding`` 是同时进行的 agent 判断。"""

    runtime    = hctx.runtime
    user_scope = str(event.get("user_id") or "anonymous")
    try:
        async with hctx.state.generation_limiter.admit(
            chat_id                    = hctx.chat_id,
            user_id                    = user_scope,
            max_global                 = max(0, runtime.cfg.max_generation_inflight_global),
            max_per_chat               = max(0, runtime.cfg.max_generation_inflight_per_chat),
            max_per_user               = max(0, runtime.cfg.max_generation_inflight_per_user),
            max_calls_per_user_per_day = max(0, runtime.cfg.max_generation_calls_per_user_per_day),
            chat_wait_seconds          = _CHAT_GENERATION_QUEUE_SECONDS,
        ):
            generated = await generate_smalltalk_turn(prepared, event, context, hctx)
            if deciding is not None and await asyncio.shield(deciding) is not None:
                # 生成比 agent 的判断先完成，而 agent 要接手：这次的聊天回复不发。
                generated.cancel_speculative_memory()
                return []
            return await finalize_smalltalk_turn(
                prepared,
                generated,
                event,
                context,
                hctx,
                started_at=started_at,
            )
    except GenerationLimitExceeded as exc:
        log_step(
            context,
            runtime,
            chat_id = hctx.chat_id,
            step    = "smalltalk.generation_limited",
            fields  = {"limit": str(exc), "forced": prepared.forced},
        )
        # 被同会话更新的消息顶替时由新一轮接手，不再发忙碌提示。
        if not prepared.forced or str(exc) == CHAT_SUPERSEDED:
            return []
        return segments("消息有点多，我缓一下再回")


async def call_bot_name_only(context) -> list[dict[str, Any]]:
    """
    处理只有机器人名称的内部调用

    返回一个随机的简短回应短语。

    Args:
        context: 插件上下文

    Returns:
        包含单个文本消息段的列表
    """
    replies = list(DEFAULT_BOT_NAME_ONLY_REPLIES)
    runtime = None
    try:
        runtime    = load_runtime(context)
        configured = [
            str(item).strip() for item in runtime.cfg.bot_name_only_replies if str(item).strip()
        ]
        if configured:
            replies = configured
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "xiaoqing_chat.bot_name_only.config",
        )
    chat_id, user_id = _context_chat_and_user_id(context)
    if chat_id:
        _state().set_pending_bot_name_call(
            chat_id,
            user_id,
            ttl_seconds=_BOT_NAME_ONLY_FOLLOWUP_TTL_SECONDS,
        )
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "smalltalk.bot_name_only",
            fields  = {
                "user_id"       : user_id,
                "followup_ttl_s": _BOT_NAME_ONLY_FOLLOWUP_TTL_SECONDS,
            },
        )
    return segments(random.choice(replies))
