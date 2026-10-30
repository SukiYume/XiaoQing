"""回复生成计划：一次性收集上下文块、反应规则、规划目标与人设边界，供每次尝试复用。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime
from .brain_chat import (
    get_brain_chat_max_context,
    persona_reply_style,
)
from .chat_event import event_chat_id, is_private_event
from .chat_runtime import ai_route_context, configured_bot_name, llm_call_config
from .context_builder import (
    expression_context,
    jargon_context,
    knowledge_context,
    sender_profile_context,
    tool_status_context,
)
from .llm.communication_constraints import requested_char_count
from .llm.llm_config import LLMCallConfig
from .logging_utils import log_step
from .media.marker_resolver import (
    build_media_candidates_block,
)
from .memory.memory import active_conversation_suffix
from .memory.review_sessions import build_policy_block
from .planning.planned_action import PlannedAction

_RE_GOAL = re.compile(r"(?:目标|要点|意图)[:：]\s*(.{2,120})")


_EXTERNAL_OPINION_QUERY_RE = re.compile(r"(?:觉得|认为|怎么看|如何看待|是否同意|赞成)")


QUESTION_FORM_RE = re.compile(
    r"[?？]|(?:吗|呢|么|嘛)$|(?:什么|啥|哪|谁|多少|几|怎么|如何|为何|是不是|有没有|是否|还是)"
)


_SELF_PROFILE_QUERY_RE = re.compile(
    r"(?:是谁|哪位|叫什么|多大|几岁|哪里人|来自哪里|住哪|在哪读|什么专业|做什么工作|"
    r"现在方便|以前|曾经|小时候|有没有|是否|是不是|会不会|"
    r"去过|做过|学过|参加过|当过|认识过|住过|工作过)"
)


def needs_persona_guard_when_checker_unavailable(text: str, *, bot_name: str) -> bool:
    """远程审查不可用时，识别不应无审查放行的自身资料问题。

    这是高召回的句法降级策略，只看对话对象，不维护任何生活领域、事件或答案词表。
    询问外部事物的当下观点不属于人物资料；若观点句内又次指向角色自身，
    则仍然需要人物证据门禁。
    """

    normalized = str(text or "").strip()
    if not normalized:
        return False
    if QUESTION_FORM_RE.search(normalized) is None:
        return False
    if "你" not in normalized:
        clean_bot_name = str(bot_name or "").strip()
        if not clean_bot_name or clean_bot_name not in normalized:
            return False
        # 句首名字通常只是称呼。去掉称呼后，普通知识问题仍按普通问题处理；
        # 只有剩余句子确实询问身份、经历或现实状态时才进入人物证据门禁。
        after_vocative = re.sub(
            rf"^\s*{re.escape(clean_bot_name)}\s*[，,:：、]?\s*",
            "",
            normalized,
            count=1,
        )
        if (
            clean_bot_name not in after_vocative
            and _SELF_PROFILE_QUERY_RE.search(after_vocative) is None
        ):
            return False
    self_markers = tuple(marker for marker in ("你", str(bot_name or "").strip()) if marker)
    if not any(marker in normalized for marker in self_markers):
        return False
    opinion_match = _EXTERNAL_OPINION_QUERY_RE.search(normalized)
    if opinion_match is None:
        return True
    remainder = normalized[opinion_match.end() :]
    return any(marker in remainder for marker in self_markers)


def allows_low_stakes_persona_fiction(runtime: Any) -> bool:
    return bool(runtime.cfg.personality.allow_low_stakes_persona_fiction)


def _extract_planner_goal(reasoning: str) -> str:
    text = (reasoning or "").strip()
    if not text:
        return ""
    m = _RE_GOAL.search(text)
    if not m:
        return ""
    return str(m.group(1) or "").strip()


def _merge_planner_reasoning(action_reasoning: str, plan_reasoning: str) -> str:
    action_text = (action_reasoning or "").strip()
    plan_text   = (plan_reasoning or "").strip()
    if action_text and plan_text:
        if plan_text.startswith(action_text):
            return plan_text
        return f"{action_text}\n{plan_text}".strip()
    return plan_text or action_text


@dataclass(frozen=True)
class ReplyGenerationPlan:
    text              : str
    event             : dict[str, Any]
    context           : Any
    runtime           : ChatRuntime
    state             : Any
    forced            : bool
    action            : PlannedAction
    bot_name          : str
    chat_id           : str
    is_private        : bool
    history           : Any
    earlier_history   : tuple[Any, ...]
    max_context_size  : int
    secrets           : dict[str, Any]
    model             : str
    foreground        : Any
    request_id        : str
    profile_block     : str
    policy_block      : str
    knowledge_block   : str
    expression_block  : str
    jargon_explanation: str
    keyword_rules     : tuple[Any, ...]
    regex_rules       : tuple[Any, ...]
    merged_reasoning  : str
    effective_goal    : str
    tool_info_block   : str
    effective_identity: str
    effective_style   : str
    state_text        : str
    started_at        : float
    media_candidates  : str = ""
    # 只对这一条的额外要求（比如告别收尾），叠加在人设的说话方式之后。
    turn_note: str = ""


def _load_reply_context_blocks(
    *,
    runtime: ChatRuntime,
    state: Any,
    data_dir: Any,
    chat_id: str,
    event: dict[str, Any],
    text: str,
    unknown_words: list[str],
) -> tuple[str, str, str, str, str]:
    """在事件循环外构建需要同步读取存储的提示词上下文块。"""

    profile_block = sender_profile_context(state, data_dir, chat_id, event)
    state.review_store.bind(data_dir)
    policy_block = (
        build_policy_block(state.review_store, chat_id)
        if runtime.cfg.reflection.enable_review_sessions
        else ""
    )
    knowledge_block    = knowledge_context(runtime, state, text)
    expression_block   = expression_context(runtime, state, data_dir, chat_id, text)
    jargon_explanation = jargon_context(
        state,
        data_dir,
        chat_id,
        unknown_words,
        text,
    )
    return profile_block, policy_block, knowledge_block, expression_block, jargon_explanation


def _select_reaction_rules(
    runtime: ChatRuntime, text: str
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    keyword_rules = [
        rule
        for rule in runtime.cfg.keyword_reaction.keyword_rules
        if rule.keyword
        and rule.keyword in text
        and random.random() < max(0.0, min(1.0, rule.probability))
    ]
    regex_rules: list[Any] = []
    for rule in runtime.cfg.keyword_reaction.regex_rules:
        try:
            if (
                rule.pattern
                and re.search(rule.pattern, text)
                and random.random() < max(0.0, min(1.0, rule.probability))
            ):
                regex_rules.append(rule)
        except re.error:
            continue
    return tuple(keyword_rules), tuple(regex_rules)


async def _load_reply_history(
    runtime: ChatRuntime, state: Any, chat_id: str, max_items: int
) -> tuple[list[Any], tuple[Any, ...]]:
    """返回长空档之后的当前会话，以及空档前最后几条仅供回忆的旧消息。"""
    recent = await state.memory_store.get_recent_async(chat_id, max_items=max_items)
    active = active_conversation_suffix(
        recent, idle_gap_seconds=float(runtime.cfg.memory.conversation_idle_gap_seconds)
    )
    return active, tuple(recent[: len(recent) - len(active)][-6:])


def foreground_call_config(runtime: ChatRuntime, text: str) -> LLMCallConfig:
    """前台生成的调用参数；对方明确要长文时（“写个800字的检讨”）按字数放宽输出预算和超时。"""

    foreground = llm_call_config(runtime.cfg, foreground=True)
    length = requested_char_count(text)
    if length is not None and length.mode != "at_most":
        return foreground.with_room_for(length.count)
    return foreground


async def prepare_reply_generation(
    *,
    text: str,
    event: dict[str, Any],
    context: Any,
    runtime: ChatRuntime,
    state: Any,
    forced: bool,
    action: PlannedAction,
    plan_reasoning: str,
    bot_name: str,
    secrets: dict[str, Any] | None,
    turn_note: str,
    state_text: str,
    is_brain_chat: bool,
) -> ReplyGenerationPlan:
    bot_name         = bot_name or configured_bot_name(context)
    chat_id          = event_chat_id(event)
    is_private       = is_private_event(event)
    max_context_size = get_brain_chat_max_context(runtime, is_brain_chat)

    history, earlier_history = await _load_reply_history(runtime, state, chat_id, max_context_size)
    started_at = time.monotonic()
    log_step(
        context,
        runtime,
        chat_id = chat_id,
        step    = "reply.generate.start",
        fields  = {
            "forced"       : forced,
            "brain_chat"   : is_brain_chat,
            "think_level"  : action.think_level,
            "history_items": len(history),
            "text"         : text,
        },
    )

    resolved_secrets = dict(secrets or ai_route_context(context, chat_id=chat_id))
    foreground = foreground_call_config(runtime, text)
    request_id = str(context.request_id or "")
    (
        profile_block,
        policy_block,
        knowledge_block,
        expression_block,
        jargon_explanation,
    ) = await asyncio.to_thread(
        _load_reply_context_blocks,
        runtime       = runtime,
        state         = state,
        data_dir      = context.data_dir,
        chat_id       = chat_id,
        event         = event,
        text          = text,
        unknown_words = action.unknown_words,
    )
    if knowledge_block:
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "reply.knowledge.query",
            fields  = {"kb_hits": len(knowledge_block)},
        )
    if expression_block:
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "reply.expression.pick",
            fields  = {"picked": len(expression_block)},
        )
    keyword_rules, regex_rules = _select_reaction_rules(runtime, text)

    merged_reasoning = _merge_planner_reasoning(action.reasoning, plan_reasoning)
    goal_state       = await state.goal_store.get_async(chat_id)
    current_goal     = goal_state.goal if runtime.cfg.goal.enable_goal and goal_state.goal else ""
    effective_goal   = _extract_planner_goal(merged_reasoning) or current_goal
    tool_info_block  = await tool_status_context(
        state    = state,
        data_dir = context.data_dir,
        bot_name = bot_name,
        chat_id  = chat_id,
        event    = event,
        goal     = effective_goal,
    )

    try:
        media_candidates = await build_media_candidates_block(context, runtime, chat_id=chat_id)
    except Exception:
        media_candidates = ""

    return ReplyGenerationPlan(
        text               = text,
        event              = event,
        context            = context,
        runtime            = runtime,
        state              = state,
        forced             = forced,
        action             = action,
        bot_name           = bot_name,
        chat_id            = chat_id,
        is_private         = is_private,
        history            = history,
        earlier_history    = earlier_history,
        max_context_size   = max_context_size,
        secrets            = resolved_secrets,
        model              = resolved_secrets.get("model", ""),
        foreground         = foreground,
        request_id         = request_id,
        profile_block      = profile_block,
        policy_block       = policy_block,
        knowledge_block    = knowledge_block,
        expression_block   = expression_block,
        jargon_explanation = jargon_explanation,
        keyword_rules      = keyword_rules,
        regex_rules        = regex_rules,
        merged_reasoning   = merged_reasoning,
        effective_goal     = effective_goal,
        tool_info_block    = tool_info_block,
        effective_identity = runtime.cfg.personality.card.facts(bot_name),
        effective_style    = persona_reply_style(runtime, is_brain_chat, bot_name),
        state_text         = state_text,
        started_at         = started_at,
        media_candidates   = media_candidates,
        turn_note          = turn_note.strip(),
    )
