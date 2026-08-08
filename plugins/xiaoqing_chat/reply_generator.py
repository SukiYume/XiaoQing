"""构建、生成并审查一轮小青回复候选。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.sensitive_audit import summarize_sensitive

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime

from .brain_chat import (
    get_brain_chat_identity,
    get_brain_chat_max_context,
    get_brain_chat_reply_style,
    get_brain_chat_temperature,
)
from .context_builder import (
    _build_expression_block,
    _build_jargon_explanation,
    _build_knowledge_block,
    _build_memory_block,
    _build_profile_block,
    _build_tool_info_block,
)
from .helper_utils import (
    _chat_id,
    _extract_sender_name,
    _get_ai_route_context,
    _get_bot_name,
    _is_private,
    _most_recent_user_local_id,
    _replace_local_ids_with_text,
    _resolve_llm_config,
)
from .llm.llm_client import LLMError, chat_completions_with_fallback_paths
from .llm.postprocess import join_reply, process_llm_response
from .llm.prompt_builder import ChatMessage, build_dialogue_prompt, build_prompt_messages
from .llm.reply_checker import (
    ReplyCheckResult,
    ReplyRejected,
    _heuristic_check,
    _media_meta_reply_check,
    _requires_configured_profile_boundary,
    check_reply,
)
from .logging_utils import _log_step, _safe_correlation_id
from .media.marker_resolver import (
    ResolvedMarker,
    marker_media_part,
    parse_marker,
    resolve_marker,
    strip_marker,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
)
from .memory.memory import active_conversation_suffix
from .memory.review_sessions import build_policy_block
from .message_parts import (
    build_text_message_parts,
    merge_reply_media_parts,
    normalize_message_parts,
)
from .planning.planned_action import PlannedAction
from .reply_payload import build_reply_payload_from_parts

_RE_GOAL = re.compile(r"(?:目标|要点|意图)[:：]\s*(.{2,120})")
_REASONING_TURN_RE = re.compile(
    r"(?:\d(?:[\d.,]*\d)?\s*(?:公斤|千克|克|厘米|毫米|米|公里|立方|每立方|%|％|kg|km|cm))"
    r"|(?:密度|半径|直径|重力|加速度|公式|计算|数量级|等于|换算)"
)
_EXTERNAL_OPINION_QUERY_RE = re.compile(r"(?:觉得|认为|怎么看|如何看待|是否同意|赞成)")
_QUESTION_FORM_RE = re.compile(
    r"[?？]|(?:吗|呢|么|嘛)$|(?:什么|哪|谁|多少|怎么|如何|为何|是不是|有没有|是否|还是)"
)
_KNOWN_NAME_QUERY_RE = re.compile(r"你(?:到底|究竟)?(?:是)?(?:谁|哪位)")
_PERSONA_INTRO_QUERY_RE = re.compile(
    r"(?:你|小青)[^，。！？；;\n]{0,24}"
    r"(?:什么样的人|介绍(?:一下)?自己|自我介绍|说说你自己|你的性格)"
)
_SELF_PROFILE_QUERY_RE = re.compile(
    r"(?:是谁|哪位|叫什么|多大|几岁|哪里人|来自哪里|住哪|在哪读|什么专业|做什么工作|"
    r"现在方便|以前|曾经|小时候|有没有|是否|是不是|会不会|"
    r"去过|做过|学过|参加过|当过|认识过|住过|工作过)"
)

# 只在明确点名且重生成仍无法安全回答未知履历时使用。这些表达不补造人物事实，
# 也不反问用户为什么问，避免把正常聊天推回机械的审讯式承接。
_PERSONA_GROUNDING_FALLBACKS = (
    "这段我真没准话，不硬编啦。聊事情本身倒可以。",
    "以前这段我真说不准，别让我现编啦。聊现在倒可以。",
    "这个个人经历我说不准，咱们就事论事吧。",
)
_CONTEXT_GROUNDING_FALLBACKS = (
    "这证据链也太薄了，我可不替人下结论。",
    "光凭眼前这点就开判，容易冤枉人。",
    "这点信息不够定性，先别给人家写剧情。",
)
_PUBLIC_IDENTITY_FALLBACK_MAX_CHARS = 120
_CURRENT_OPINION_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*"
    r"(?:我(?:觉得|认为|看|感觉|主张)|我的(?:看法|观点)是)"
    r"\s*[：,:，]?\s*(?P<claim>[^，。！？；;\n]{2,80})"
)
_NO_QUESTION_REQUEST_RE = re.compile(
    r"(?:别|不要|不用|不必|不许|请勿)(?:再|总是|一直)?"
    r"(?:反问|追问|提问|问(?:我|问题)?|用问题(?:来)?收尾|以问题收尾)"
)


@dataclass(frozen=True)
class ReplyDraft:
    """已规范化、可进入审查和媒体合并阶段的回复草稿。"""

    text: str
    text_parts: tuple[str, ...]
    parts: tuple[dict[str, Any], ...]
    raw_text: str = ""
    rewritten_text: str = ""
    media_marker: ResolvedMarker | None = None


# 路由、人物边界与确定性文本修复


def _needs_reasoning_route(text: str) -> bool:
    """数值、单位和科学关系优先交给推理模型，普通闲聊继续使用低延迟 route。"""

    return _REASONING_TURN_RE.search(str(text or "")) is not None


def _needs_persona_guard_when_checker_unavailable(text: str, *, bot_name: str) -> bool:
    """远程审查不可用时，识别不应无审查放行的自身资料问题。

    这是高召回的句法降级策略，只看对话对象，不维护任何生活领域、事件或答案词表。
    询问外部事物的当下观点不属于人物资料；若观点句内又次指向角色自身，
    则仍然需要人物证据门禁。
    """

    normalized = str(text or "").strip()
    if not normalized:
        return False
    if _QUESTION_FORM_RE.search(normalized) is None:
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


def _allows_low_stakes_persona_fiction(runtime: Any) -> bool:
    cfg = getattr(runtime, "cfg", None)
    personality = getattr(cfg, "personality", None)
    return bool(getattr(personality, "allow_low_stakes_persona_fiction", False))


def _current_opinion_fallback(text: str) -> str:
    """从本轮显式主张生成不引入外部事实的贴题保守回应。"""

    match = _CURRENT_OPINION_RE.search(str(text or ""))
    if match is None:
        return ""
    claim = match.group("claim").strip(" ，,。！？!?；;：:")
    if not claim:
        return ""
    return f"这个我不完全同意，{claim}说得太满了。"


def _concise_public_identity(identity: str) -> str:
    """只提取可公开的人物简介开头，绝不带出内部策略说明。"""

    lead = re.split(r"[；;\n]", str(identity or "").strip(), maxsplit=1)[0].strip()
    if len(lead) <= _PUBLIC_IDENTITY_FALLBACK_MAX_CHARS:
        return lead.rstrip("。")
    sentences = re.findall(r"[^。！？!?]+[。！？!?]?", lead)
    selected: list[str] = []
    length = 0
    for sentence in sentences:
        cleaned = sentence.strip()
        if not cleaned or length + len(cleaned) > _PUBLIC_IDENTITY_FALLBACK_MAX_CHARS:
            break
        selected.append(cleaned)
        length += len(cleaned)
    if selected:
        return "".join(selected).rstrip("。")
    return lead[:_PUBLIC_IDENTITY_FALLBACK_MAX_CHARS].rstrip("，,、；;。 ")


def _repair_no_question_draft(current_text: str, draft: ReplyDraft) -> ReplyDraft:
    """明确要求不追问时，保留候选中的陈述句并删除问句。"""

    if (
        _NO_QUESTION_REQUEST_RE.search(str(current_text or "")) is None
        or re.search(r"[?？]", draft.text) is None
        or _draft_has_media(draft.parts)
    ):
        return draft
    statements = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?])\s*|\n+", draft.text)
        if part.strip() and re.search(r"[?？]", part) is None
    ]
    repaired = _build_reply_draft(
        statements,
        raw_text=draft.raw_text,
        rewritten_text="\n".join(statements),
    )
    return repaired or draft


def _is_turn_stale(chat_id: str, event: dict[str, Any]) -> bool:
    local_id = str(event.get("_xc_user_recorded_local_id") or "").strip()
    if not local_id:
        return False
    try:
        latest_local_id = str(_most_recent_user_local_id(chat_id) or "")
        return latest_local_id != local_id
    except Exception:
        return False


# 草稿规范化、媒体合并与审查输入


def _log_prompt_audit_metadata(
    context: Any,
    messages: list[ChatMessage],
    *,
    request_id: str,
) -> None:
    """保留提示词调试指标，但不写入提示词正文。"""

    safe_request_id = _safe_correlation_id(request_id)
    for index, message in enumerate(messages):
        content = message.content if isinstance(message.content, str) else ""
        summary = summarize_sensitive(content)
        role = (
            message.role if message.role in {"system", "user", "assistant", "tool"} else "unknown"
        )
        try:
            context.logger.info(
                "xiaoqing_chat sensitive_audit operation=reply_prompt status=prepared "
                "request_id=%s role=%s role_index=%d length=%d bytes=%d fingerprint=%s",
                safe_request_id,
                role,
                index,
                summary.length,
                summary.byte_length,
                summary.fingerprint,
            )
        except Exception:
            return


def _normalize_reply_text_parts(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    cleaned: list[str] = []
    for item in values:
        if isinstance(item, str):
            text = item.strip()
        elif item in (None, "", False):
            text = ""
        else:
            continue
        if text:
            cleaned.append(text)
    return tuple(cleaned)


def _build_reply_draft(
    text_parts: Any,
    *,
    raw_text: str = "",
    rewritten_text: str = "",
) -> ReplyDraft | None:
    normalized_text_parts = _normalize_reply_text_parts(text_parts)
    reply_text = join_reply(normalized_text_parts)
    if not reply_text:
        return None
    return ReplyDraft(
        text=reply_text,
        text_parts=normalized_text_parts,
        parts=build_text_message_parts(reply_text),
        raw_text=str(raw_text or ""),
        rewritten_text=str(rewritten_text or raw_text or ""),
    )


def _draft_has_media(parts: Any) -> bool:
    return any(
        str(part.get("kind", "") or "").strip() != "text" for part in normalize_message_parts(parts)
    )


def _attach_reply_media_marker(
    draft: ReplyDraft,
    *,
    context,
    resolved_marker: ResolvedMarker | None = None,
) -> ReplyDraft:
    if resolved_marker is None or _draft_has_media(draft.parts):
        return draft

    media_part = marker_media_part(context, resolved_marker)
    if media_part is None:
        return draft

    merged_parts = merge_reply_media_parts(
        draft.parts,
        (media_part,),
    )
    return ReplyDraft(
        text=draft.text or resolved_marker.marker,
        text_parts=draft.text_parts,
        parts=merged_parts,
        raw_text=draft.raw_text,
        rewritten_text=draft.rewritten_text,
        media_marker=resolved_marker,
    )


def _reply_checker_inputs(draft: ReplyDraft) -> tuple[str, str]:
    normalized_parts = normalize_message_parts(draft.parts)
    payload = build_reply_payload_from_parts(normalized_parts)
    final_reply = str(payload.display_text or "").strip() or str(draft.text or "").strip()
    heuristic_reply = "".join(
        str(part.get("text", "") or "")
        for part in normalized_parts
        if str(part.get("kind", "") or "").strip() == "text"
    ).strip()
    if not heuristic_reply:
        heuristic_reply = final_reply
    return final_reply, heuristic_reply


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
    plan_text = (plan_reasoning or "").strip()
    if action_text and plan_text:
        if plan_text.startswith(action_text):
            return plan_text
        return f"{action_text}\n{plan_text}".strip()
    return plan_text or action_text


@dataclass(frozen=True)
class _ReplyGenerationPlan:
    text: str
    event: dict[str, Any]
    context: Any
    runtime: _ChatRuntime
    state: Any
    forced: bool
    action: PlannedAction
    bot_name: str
    chat_id: str
    is_private: bool
    history: Any
    max_context_size: int
    chat_temperature: float
    secrets: dict[str, Any]
    model: str
    foreground: Any
    request_id: str
    profile_block: str
    policy_block: str
    knowledge_block: str
    expression_block: str
    jargon_explanation: str
    keyword_rules: tuple[Any, ...]
    regex_rules: tuple[Any, ...]
    merged_reasoning: str
    effective_goal: str
    tool_info_block: str
    effective_identity: str
    effective_style: str
    state_text: str
    started_at: float


@dataclass
class _ReplyAttemptState:
    max_items: int
    prefetched_memory: asyncio.Task[str] | None = None
    cached_memory: str | None = None
    regen_used: int = 0
    extra_check_hint: str = ""


@dataclass(frozen=True)
class _RejectedCandidate:
    text: str
    result: ReplyCheckResult
    draft: ReplyDraft | None = None
    # 只有明确标记为 soft 的风格拒绝才允许在强制回复的重生成耗尽后采用。
    # 上下文、说话人、人物经历、事实、结构和媒体错误都属于 hard，绝不放行。
    allow_after_regen_exhausted: bool = False


# 上下文准备与单次候选请求


def _load_reply_context_blocks(
    *,
    runtime: _ChatRuntime,
    state: Any,
    data_dir: Any,
    chat_id: str,
    event: dict[str, Any],
    text: str,
    unknown_words: list[str],
) -> tuple[str, str, str, str, str]:
    """在事件循环外构建需要同步读取存储的提示词上下文块。"""

    profile_block = _build_profile_block(state, data_dir, chat_id, event)
    state.review_store.bind(data_dir)
    policy_block = (
        build_policy_block(state.review_store, chat_id)
        if runtime.cfg.reflection.enable_review_sessions
        else ""
    )
    knowledge_block = _build_knowledge_block(runtime, state, text)
    expression_block = _build_expression_block(runtime, state, data_dir, chat_id)
    jargon_explanation = _build_jargon_explanation(
        state,
        data_dir,
        chat_id,
        unknown_words,
    )
    return profile_block, policy_block, knowledge_block, expression_block, jargon_explanation


def _select_reaction_rules(
    runtime: _ChatRuntime, text: str
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


async def _prepare_reply_generation(
    *,
    text: str,
    event: dict[str, Any],
    context: Any,
    runtime: _ChatRuntime,
    state: Any,
    forced: bool,
    action: PlannedAction,
    plan_reasoning: str,
    bot_name: str,
    secrets: dict[str, Any] | None,
    reply_style_override: str,
    state_text: str,
    is_brain_chat: bool,
) -> _ReplyGenerationPlan:
    if not context.http_session:
        raise RuntimeError("http_session not available")

    bot_name = bot_name or _get_bot_name(context)
    chat_id = _chat_id(event)
    is_private = _is_private(event)
    max_context_size = get_brain_chat_max_context(runtime, is_brain_chat)
    chat_temperature = get_brain_chat_temperature(runtime, is_brain_chat)

    history = await state.memory_store.get_recent_async(chat_id, max_items=max_context_size)
    memory_cfg = getattr(runtime.cfg, "memory", None)
    history = active_conversation_suffix(
        history,
        idle_gap_seconds=float(getattr(memory_cfg, "conversation_idle_gap_seconds", 1800.0) or 0.0),
    )
    started_at = time.monotonic()
    _log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="reply.generate.start",
        fields={
            "forced": forced,
            "brain_chat": is_brain_chat,
            "think_level": getattr(action, "think_level", None),
            "history_items": len(history),
            "text": text,
        },
    )

    resolved_secrets = dict(secrets or _get_ai_route_context(context, chat_id=chat_id))
    foreground = _resolve_llm_config(runtime.cfg, foreground=True)
    request_id = str(getattr(context, "request_id", "") or "")
    (
        profile_block,
        policy_block,
        knowledge_block,
        expression_block,
        jargon_explanation,
    ) = await asyncio.to_thread(
        _load_reply_context_blocks,
        runtime=runtime,
        state=state,
        data_dir=context.data_dir,
        chat_id=chat_id,
        event=event,
        text=text,
        unknown_words=action.unknown_words,
    )
    if knowledge_block:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.knowledge.query",
            fields={"kb_hits": len(knowledge_block)},
        )
    if expression_block:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.expression.pick",
            fields={"picked": len(expression_block)},
        )
    style_override = reply_style_override.strip()
    if (
        not style_override
        and runtime.cfg.personality.multiple_reply_style
        and random.random() < max(0.0, min(1.0, runtime.cfg.personality.multiple_probability))
    ):
        style_override = random.choice(runtime.cfg.personality.multiple_reply_style).strip()
    keyword_rules, regex_rules = _select_reaction_rules(runtime, text)

    merged_reasoning = _merge_planner_reasoning(action.reasoning, plan_reasoning)
    goal_state = await state.goal_store.get_async(chat_id)
    current_goal = goal_state.goal if runtime.cfg.goal.enable_goal and goal_state.goal else ""
    effective_goal = _extract_planner_goal(merged_reasoning) or current_goal
    tool_info_block = await _build_tool_info_block(
        state=state,
        data_dir=context.data_dir,
        bot_name=bot_name,
        chat_id=chat_id,
        event=event,
        goal=effective_goal,
    )

    return _ReplyGenerationPlan(
        text=text,
        event=event,
        context=context,
        runtime=runtime,
        state=state,
        forced=forced,
        action=action,
        bot_name=bot_name,
        chat_id=chat_id,
        is_private=is_private,
        history=history,
        max_context_size=max_context_size,
        chat_temperature=chat_temperature,
        secrets=resolved_secrets,
        model=resolved_secrets.get("model", ""),
        foreground=foreground,
        request_id=request_id,
        profile_block=profile_block,
        policy_block=policy_block,
        knowledge_block=knowledge_block,
        expression_block=expression_block,
        jargon_explanation=jargon_explanation,
        keyword_rules=keyword_rules,
        regex_rules=regex_rules,
        merged_reasoning=merged_reasoning,
        effective_goal=effective_goal,
        tool_info_block=tool_info_block,
        effective_identity=get_brain_chat_identity(runtime, is_brain_chat),
        effective_style=style_override or get_brain_chat_reply_style(runtime, is_brain_chat),
        state_text=state_text,
        started_at=started_at,
    )


async def _load_attempt_memory(
    plan: _ReplyGenerationPlan,
    attempt: _ReplyAttemptState,
    trimmed_history: Any,
) -> str:
    if attempt.cached_memory is not None:
        memory_block = attempt.cached_memory
    elif attempt.prefetched_memory is not None:
        try:
            memory_block = await attempt.prefetched_memory
        except Exception:
            memory_block = ""
        attempt.prefetched_memory = None
        attempt.cached_memory = memory_block
    else:
        memory_block = await _build_memory_block(
            context=plan.context,
            runtime=plan.runtime,
            state=plan.state,
            secrets=plan.secrets,
            data_dir=plan.context.data_dir,
            chat_id=plan.chat_id,
            history=trimmed_history,
            current_text=plan.text,
            planner_question=plan.action.question,
            bot_name=plan.bot_name,
        )
        attempt.cached_memory = memory_block

    full_memory_block = memory_block
    if plan.profile_block:
        full_memory_block = (plan.profile_block + "\n" + (full_memory_block or "")).strip() + "\n"
    if plan.policy_block.strip():
        full_memory_block = (
            plan.policy_block.strip() + "\n\n" + (full_memory_block or "").strip()
        ).strip() + "\n"
    if plan.knowledge_block:
        full_memory_block = (plan.knowledge_block + "\n" + (full_memory_block or "")).strip() + "\n"
    return full_memory_block


def _build_attempt_messages(
    plan: _ReplyGenerationPlan,
    attempt: _ReplyAttemptState,
    trimmed_history: Any,
    memory_block: str,
) -> list[dict[str, str]]:
    messages = build_prompt_messages(
        is_private=plan.is_private,
        bot_name=plan.bot_name,
        sender_name=_extract_sender_name(plan.event),
        think_level=plan.action.think_level,
        history=trimmed_history,
        current_text=plan.text,
        personality=plan.runtime.cfg.personality,
        keyword_rules=plan.keyword_rules,
        regex_rules=plan.regex_rules,
        current_parts=normalize_message_parts(plan.event.get("_xc_effective_user_parts")),
        memory_block=memory_block,
        expression_habits_block=plan.expression_block,
        jargon_explanation=plan.jargon_explanation,
        tool_info_block=plan.tool_info_block,
        planner_reasoning=_replace_local_ids_with_text(plan.chat_id, plan.merged_reasoning),
        identity_block=plan.effective_identity,
        reply_style_override=plan.effective_style,
        state_override=plan.state_text,
        request_id=plan.request_id,
        goal=plan.effective_goal,
    )
    if attempt.extra_check_hint:
        messages.append(ChatMessage(role="user", content=attempt.extra_check_hint))
    if plan.runtime.cfg.debug.show_reply_prompt:
        _log_prompt_audit_metadata(plan.context, messages, request_id=plan.request_id)
    plan.state.inc_stats(plan.chat_id, "calls")
    return [{"role": message.role, "content": message.content} for message in messages]


async def _request_reply_candidate(
    plan: _ReplyGenerationPlan,
    payload_messages: list[dict[str, str]],
) -> str | None:
    foreground = plan.foreground
    request_secrets = plan.secrets
    route_name = str(plan.secrets.get("_route") or "chat")
    request_max_tokens = int(plan.runtime.cfg.max_tokens)
    if (
        plan.secrets.get("_ai") is not None
        and not plan.secrets.get("_pinned_model")
        and _needs_reasoning_route(plan.text)
    ):
        request_secrets = dict(plan.secrets)
        request_secrets["_route"] = "reasoning"
        request_secrets["_pinned_model"] = None
        route_name = "reasoning"
        # DeepSeek 的 max_tokens 同时覆盖隐藏思考和最终回答。普通聊天无需放大，
        # 科学推理若仍用 512，可能只得到 reasoning_content 而没有可发送正文。
        request_max_tokens = max(request_max_tokens, 2048)
    _log_step(
        plan.context,
        plan.runtime,
        chat_id=plan.chat_id,
        step="reply.llm.start",
        fields={
            "model": plan.model,
            "route": route_name,
            "messages": len(payload_messages),
            "timeout_s": foreground.timeout_seconds,
            "max_retry": foreground.max_retry,
            "max_tokens": request_max_tokens,
        },
    )
    started_at = time.monotonic()
    raw, used_profile = await chat_completions_with_fallback_paths(
        secrets=request_secrets,
        messages=payload_messages,
        temperature=plan.chat_temperature,
        top_p=plan.runtime.cfg.top_p,
        max_tokens=request_max_tokens,
        **foreground.to_dict(),
    )
    if not isinstance(raw, str):
        raise TypeError("reply completion content must be a string")
    if _is_turn_stale(plan.chat_id, plan.event):
        _log_step(
            plan.context,
            plan.runtime,
            chat_id=plan.chat_id,
            step="reply.stale.abort",
            fields={},
        )
        return None
    _log_step(
        plan.context,
        plan.runtime,
        chat_id=plan.chat_id,
        step="reply.llm.ok",
        fields={
            "elapsed_s": round(time.monotonic() - started_at, 3),
            "used_profile": used_profile,
            "raw_chars": len(raw or ""),
        },
    )
    return raw


def _precheck_candidate(
    plan: _ReplyGenerationPlan,
    raw: str,
    trimmed_history: Any,
) -> _RejectedCandidate | None:
    if not raw or not plan.runtime.cfg.reply_check.enable_reply_checker:
        return None
    precheck_text = text_without_outbound_marker(raw)
    raw_parts = process_llm_response(
        precheck_text,
        plan.runtime.cfg.postprocess,
        bot_name=plan.bot_name,
    )
    raw_draft = _build_reply_draft(raw_parts, raw_text=raw, rewritten_text=raw)
    if raw_draft is None:
        return None
    result = _heuristic_check(
        reply=raw_draft.text,
        history=trimmed_history,
        bot_name=plan.bot_name,
        max_repeat_compare=plan.runtime.cfg.reply_check.max_repeat_compare,
        similarity_threshold=plan.runtime.cfg.reply_check.similarity_threshold,
        max_assistant_in_row=plan.runtime.cfg.reply_check.max_assistant_in_row,
    )
    if result is None or result.suitable:
        return None
    _log_step(
        plan.context,
        plan.runtime,
        chat_id=plan.chat_id,
        step="reply.checker.skip",
        fields={"stage": "pre_heuristic", "action": "reject", "reason": result.reason},
    )
    return _RejectedCandidate(
        text=raw_draft.text,
        result=result,
        draft=raw_draft,
        allow_after_regen_exhausted=not result.is_hard,
    )


async def _resolve_candidate_draft(
    plan: _ReplyGenerationPlan,
    raw: str,
    trimmed_history: Any,
) -> ReplyDraft | None:
    parsed_marker = parse_marker(raw)
    resolved_marker: ResolvedMarker | None = None
    marker_text = raw
    if parsed_marker is not None:
        _log_step(
            plan.context,
            plan.runtime,
            chat_id=plan.chat_id,
            step="reply.marker.parsed",
            fields={"kind": parsed_marker.kind, "hint": parsed_marker.hint},
        )
        marker_text = strip_outbound_marker_residue(strip_marker(raw, parsed_marker.raw_span))
        try:
            resolved_marker = await resolve_marker(
                parsed_marker,
                context=plan.context,
                runtime=plan.runtime,
                history=trimmed_history,
                chat_id=plan.chat_id,
                media_store=plan.state.media_store,
            )
        except Exception as exc:
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.marker.miss",
                fields={
                    "kind": parsed_marker.kind,
                    "hint": parsed_marker.hint,
                    "reason": type(exc).__name__,
                },
            )
        if resolved_marker is None:
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.marker.miss",
                fields={
                    "kind": parsed_marker.kind,
                    "hint": parsed_marker.hint,
                    "reason": "candidate_not_found",
                },
            )
        else:
            entry_id = (
                str(getattr(resolved_marker.entry, "media_hash", "") or "")
                or str(getattr(resolved_marker.entry, "face_id", "") or "")
                or str(getattr(resolved_marker.entry, "media_key", "") or "")
            )
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.marker.resolved",
                fields={
                    "kind": resolved_marker.kind,
                    "hint": resolved_marker.hint,
                    "entry_id": entry_id,
                },
            )
    else:
        cleaned_marker_text = strip_outbound_marker_residue(raw)
        if cleaned_marker_text != raw:
            marker_text = cleaned_marker_text
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.marker.miss",
                fields={"kind": "", "hint": "", "reason": "parse_failed"},
            )

    parts = process_llm_response(marker_text, plan.runtime.cfg.postprocess, bot_name=plan.bot_name)
    draft = _build_reply_draft(parts, raw_text=raw, rewritten_text=marker_text)
    if draft is None and resolved_marker is not None:
        draft = ReplyDraft(
            text=resolved_marker.marker,
            text_parts=(),
            parts=(),
            raw_text=raw,
            rewritten_text=marker_text,
        )
    if draft is None:
        return None
    return _attach_reply_media_marker(
        draft,
        context=plan.context,
        resolved_marker=resolved_marker,
    )


async def _check_candidate_draft(
    plan: _ReplyGenerationPlan,
    draft: ReplyDraft,
    trimmed_history: Any,
    memory_block: str,
) -> _RejectedCandidate | None:
    if not plan.runtime.cfg.reply_check.enable_reply_checker:
        return None
    _log_step(
        plan.context,
        plan.runtime,
        chat_id=plan.chat_id,
        step="reply.check.start",
        fields={"llm_checker": plan.runtime.cfg.reply_check.enable_llm_checker},
    )
    chat_history_text = build_dialogue_prompt(
        trimmed_history,
        bot_name=plan.bot_name,
        truncate=True,
    )
    check_reply_text, heuristic_reply_text = _reply_checker_inputs(draft)
    goal = plan.effective_goal or plan.merged_reasoning or "自然聊天"
    # 结构性检查已在 check_reply() 的远程调用之前完成。远程审查只占用一个短预算；
    # 超时或协议故障属于基础设施问题，不能触发多轮重生成并拖垮整个插件回调。
    checker_timeout = max(0.1, plan.runtime.cfg.reply_check.timeout_seconds)
    try:
        check = await asyncio.wait_for(
            check_reply(
                http_session=plan.context.http_session,
                secrets=plan.secrets,
                bot_name=plan.bot_name,
                reply=check_reply_text,
                heuristic_reply=heuristic_reply_text,
                current_text=plan.text,
                goal=goal,
                policy_text=plan.policy_block,
                grounding_text="\n".join(
                    part
                    for part in (
                        plan.effective_identity,
                        plan.state_text,
                        plan.profile_block,
                        str(memory_block or "")[:1200],
                    )
                    if str(part or "").strip()
                ),
                history=trimmed_history,
                chat_history_text=chat_history_text,
                enable_llm_checker=plan.runtime.cfg.reply_check.enable_llm_checker,
                max_repeat_compare=plan.runtime.cfg.reply_check.max_repeat_compare,
                similarity_threshold=plan.runtime.cfg.reply_check.similarity_threshold,
                max_assistant_in_row=plan.runtime.cfg.reply_check.max_assistant_in_row,
                max_tokens=plan.runtime.cfg.reply_check.max_tokens,
                timeout_seconds=checker_timeout,
                max_retry=0,
                retry_interval_seconds=0.2,
                llm_checker_mode=getattr(
                    plan.runtime.cfg.reply_check,
                    "llm_checker_mode",
                    "always",
                ),
                check_omitted_persona_episode=not plan.forced,
                allow_low_stakes_persona_fiction=_allows_low_stakes_persona_fiction(plan.runtime),
            ),
            timeout=checker_timeout + 0.25,
        )
    except asyncio.TimeoutError:
        _log_step(
            plan.context,
            plan.runtime,
            chat_id=plan.chat_id,
            step="reply.check.timeout",
            fields={},
        )
        check = ReplyCheckResult(
            suitable=True,
            reason="回复检查暂不可用",
            need_replan=False,
            severity="infra",
        )
    except Exception as exc:
        _log_step(
            plan.context,
            plan.runtime,
            chat_id=plan.chat_id,
            step="reply.check.error",
            fields={"error_type": type(exc).__name__},
        )
        check = ReplyCheckResult(
            suitable=True,
            reason="reply_checker unavailable",
            need_replan=False,
            severity="infra",
        )
    if (
        check.severity == "infra"
        and not _allows_low_stakes_persona_fiction(plan.runtime)
        and _needs_persona_guard_when_checker_unavailable(
            plan.text,
            bot_name=plan.bot_name,
        )
    ):
        check = ReplyCheckResult(
            suitable=False,
            reason="人物依据审查暂不可用",
            need_replan=True,
            severity="hard",
            failure_code="persona_grounding",
        )
    _log_step(
        plan.context,
        plan.runtime,
        chat_id=plan.chat_id,
        step="reply.check.result",
        fields={
            "suitable": bool(check.suitable),
            "need_replan": bool(check.need_replan),
            "severity": check.severity,
            "failure_code": check.failure_code,
            "persona_claim_count": check.persona_claim_count,
            "context_claim_count": check.context_claim_count,
            "reason": check.reason,
        },
    )
    if check.suitable:
        return None

    # 再跑确定性检查作为纵深防御；LLM 已通过 severity 明确区分事实/人物等硬错误
    # 与轻微风格问题，不能再把所有 LLM 拒绝一概当成可放行的软门禁。
    structural_rejection = _heuristic_check(
        reply=heuristic_reply_text,
        history=trimmed_history,
        bot_name=plan.bot_name,
        max_repeat_compare=plan.runtime.cfg.reply_check.max_repeat_compare,
        similarity_threshold=plan.runtime.cfg.reply_check.similarity_threshold,
        max_assistant_in_row=plan.runtime.cfg.reply_check.max_assistant_in_row,
    )
    media_rejection = _media_meta_reply_check(
        reply=heuristic_reply_text,
        current_text=plan.text,
    )
    return _RejectedCandidate(
        text=check_reply_text,
        result=check,
        draft=draft,
        allow_after_regen_exhausted=(
            not check.is_hard and structural_rejection is None and media_rejection is None
        ),
    )


def _queue_reply_regeneration(
    plan: _ReplyGenerationPlan,
    attempt: _ReplyAttemptState,
    rejected: _RejectedCandidate,
    *,
    step: str,
) -> bool:
    if attempt.regen_used >= max(0, int(plan.runtime.cfg.reply_check.max_regen)):
        return False
    if rejected.result.failure_code == "persona_grounding" and (
        _requires_configured_profile_boundary(
            str(getattr(plan, "text", "") or ""),
            str(getattr(plan, "effective_identity", "") or ""),
        )
    ):
        # 精确资料在配置中明确留空，重问主模型只会增加再次补写细节的机会。
        return False
    attempt.regen_used += 1
    correction = (
        "涉及角色、用户、第三方或早先对话的无依据信息时，不要用另一项肯定、否定、"
        "习惯、愿望、动机或背景替换原陈述；只承接可见输入明确支持的内容，并保留"
        "来源与不确定性。"
    )
    if rejected.result.failure_code == "persona_grounding":
        persona_question = _needs_persona_guard_when_checker_unavailable(
            str(getattr(plan, "text", "") or ""),
            bot_name=str(getattr(plan, "bot_name", "") or ""),
        )
        if _allows_low_stakes_persona_fiction(plan.runtime) and persona_question:
            correction = (
                "保留稳定人设和可靠记忆；若要讲自己的故事，只能改成普通、低风险、"
                "不可核验且与人设一致的日常小片段。删除精确身份、现实关系、重大经历、"
                "现实承诺以及拿个人故事给外部事实作证的内容。"
                "如果用户问的是普通知识、事情本身或当下看法，就直接回答，不要硬塞个人故事。"
            )
        elif _allows_low_stakes_persona_fiction(plan.runtime):
            correction = (
                "当前用户问的不是你的履历或生活故事。删除第一人称经历、关系、习惯、"
                "现实日程和自我举例，只针对用户当前说的具体内容直接表态、接梗或回答；"
                "不要把句首称呼误当成人物资料提问。"
            )
        else:
            correction = (
                "删除所有受控人物资料没有支持的个人经历、现实关系、长期履历、现实日程、"
                "线下义务和当前活动。"
                "如果用户问的是普通知识、事情本身或当下看法，就直接回答那个问题，不要把"
                "句首称呼误当成人物资料提问；开放话题可改成当下观点或条件式设想；"
                "如果确实问到未知履历，简短承认说不准即可。"
            )
    elif rejected.result.failure_code == "context_grounding":
        correction = (
            "删除所有对他人经历、习惯、动机、是否在场或当前状态的无依据推断；"
            "加上可能、大概或玩笑语气也不能替代证据。可以自然指出现有信息不足，"
            "或只回应当前对话明确可见的言行。"
        )
    elif rejected.result.failure_code == "instruction_following":
        correction = (
            "重新读取最新用户消息对本轮表达方式的明确要求，并在内容和语气上都遵守；"
            "不要用善意、安慰、玩笑或话题推进覆盖用户刚说的边界。"
        )
    attempt.extra_check_hint = (
        f'上一条候选回复"{rejected.text}"被检查拒绝:{rejected.result.reason}。\n'
        f"请按拒绝机制修正，而不是只换措辞。{correction}"
        "同时避免重复表达、自言自语和刷屏。"
    ).strip()
    _log_step(
        plan.context,
        plan.runtime,
        chat_id=plan.chat_id,
        step=step,
        fields={"regen_used": attempt.regen_used},
    )
    return True


def _finish_rejected_candidate(
    plan: _ReplyGenerationPlan,
    rejected: _RejectedCandidate,
) -> ReplyDraft | None:
    if plan.forced and rejected.allow_after_regen_exhausted and rejected.draft is not None:
        _log_step(
            plan.context,
            plan.runtime,
            chat_id=plan.chat_id,
            step="reply.check.exhausted.accept",
            fields={
                "reply_chars": len(rejected.draft.text),
                "need_replan": bool(rejected.result.need_replan),
            },
        )
        return rejected.draft

    fallback_texts: tuple[str, ...] = ()
    fallback_step = "reply.check.exhausted.forced_fallback"
    if plan.forced and rejected.result.failure_code == "persona_grounding":
        bot_name = str(getattr(plan, "bot_name", "") or "").strip()
        identity = str(getattr(plan, "effective_identity", "") or "").strip()
        if identity:
            identity_prefix = f"你叫{bot_name}" if bot_name else ""
            if identity_prefix and identity.startswith(identity_prefix):
                identity = f"我叫{bot_name}{identity[len(identity_prefix) :]}"
            elif identity.startswith("你是"):
                identity = f"我是{identity[2:]}"
            identity = identity.rstrip("。")
        public_identity = _concise_public_identity(identity)
        if bot_name and _KNOWN_NAME_QUERY_RE.search(str(plan.text or "")):
            fallback_texts = (f"我叫{bot_name}。",)
        elif _requires_configured_profile_boundary(
            str(getattr(plan, "text", "") or ""),
            str(getattr(plan, "effective_identity", "") or ""),
        ):
            fallback_texts = (
                (f"具体到现实资料我就不展开啦；按公开人设来说，{public_identity}。")
                if public_identity
                else "具体人设以当前配置为准，我不补写现实资料。",
            )
        elif _PERSONA_INTRO_QUERY_RE.search(str(plan.text or "")):
            fallback_texts = (
                (f"按公开人设来说，{public_identity}。别真把我当简历看就行。")
                if public_identity
                else "具体人设以当前配置为准，我不补写没给出的经历。",
            )
        else:
            start = sum(ord(char) for char in str(plan.request_id or plan.text)) % len(
                _PERSONA_GROUNDING_FALLBACKS
            )
            fallback_texts = tuple(
                _PERSONA_GROUNDING_FALLBACKS[(start + offset) % len(_PERSONA_GROUNDING_FALLBACKS)]
                for offset in range(len(_PERSONA_GROUNDING_FALLBACKS))
            )
        fallback_step = "reply.check.exhausted.persona_grounding"
    elif plan.forced and rejected.result.failure_code == "instruction_following":
        fallback_texts = ("行，按你刚说的来。",)
    elif plan.forced and rejected.result.failure_code == "context_grounding":
        opinion_fallback = _current_opinion_fallback(str(getattr(plan, "text", "") or ""))
        if opinion_fallback:
            fallback_texts = (opinion_fallback,)
        else:
            start = sum(ord(char) for char in str(plan.request_id or plan.text)) % len(
                _CONTEXT_GROUNDING_FALLBACKS
            )
            fallback_texts = tuple(
                _CONTEXT_GROUNDING_FALLBACKS[(start + offset) % len(_CONTEXT_GROUNDING_FALLBACKS)]
                for offset in range(len(_CONTEXT_GROUNDING_FALLBACKS))
            )
    elif plan.forced and _QUESTION_FORM_RE.search(str(getattr(plan, "text", "") or "")):
        fallback_texts = ("这个我还拿不准，先不瞎答。",)
    elif plan.forced:
        fallback_texts = ("看到了，我在。",)

    if plan.forced and fallback_texts:
        for fallback_text in fallback_texts:
            runtime = getattr(plan, "runtime", None)
            history = getattr(plan, "history", ())
            bot_name = str(getattr(plan, "bot_name", "") or "")
            reply_check_cfg = getattr(getattr(runtime, "cfg", None), "reply_check", None)
            structural_rejection = None
            if reply_check_cfg is not None:
                structural_rejection = _heuristic_check(
                    reply=fallback_text,
                    history=history,
                    bot_name=bot_name,
                    max_repeat_compare=reply_check_cfg.max_repeat_compare,
                    similarity_threshold=reply_check_cfg.similarity_threshold,
                    max_assistant_in_row=reply_check_cfg.max_assistant_in_row,
                )
            if structural_rejection is not None:
                continue
            fallback = _build_reply_draft(
                (fallback_text,),
                raw_text=rejected.text,
                rewritten_text=fallback_text,
            )
            if fallback is None:
                continue
            context = getattr(plan, "context", None)
            if context is not None and runtime is not None:
                _log_step(
                    context,
                    runtime,
                    chat_id=str(getattr(plan, "chat_id", "") or ""),
                    step=fallback_step,
                    fields={
                        "reply_chars": len(fallback.text),
                        "failure_code": rejected.result.failure_code,
                    },
                )
            return fallback
    if rejected.result.need_replan and not plan.forced:
        if rejected.result.failure_code in {
            "persona_grounding",
            "context_grounding",
            "instruction_following",
        }:
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.check.exhausted.proactive_silence",
                fields={"failure_code": rejected.result.failure_code},
            )
            return None
        raise ReplyRejected(rejected.result.reason or "回复被检查拒绝", True)
    if plan.forced:
        # 明确点名或私聊不能因为审查耗尽而整轮无响应。极端情况下只确认在场，
        # 不发送已知有问题的候选，也不继续向外抛错。
        return _build_reply_draft(
            ("看到了，我在。",),
            raw_text=rejected.text,
            rewritten_text="看到了，我在。",
        )
    return None


async def _generate_reply_draft(
    *,
    text: str,
    event: dict[str, Any],
    context,
    runtime: _ChatRuntime,
    state,
    forced: bool,
    action: PlannedAction,
    plan_reasoning: str,
    bot_name: str = "",
    secrets: dict[str, Any] | None = None,
    reply_style_override: str = "",
    state_text: str = "",
    is_brain_chat: bool = False,
    prefetched_memory_task: asyncio.Task[str] | None = None,
) -> ReplyDraft | None:
    plan = await _prepare_reply_generation(
        text=text,
        event=event,
        context=context,
        runtime=runtime,
        state=state,
        forced=forced,
        action=action,
        plan_reasoning=plan_reasoning,
        bot_name=bot_name,
        secrets=secrets,
        reply_style_override=reply_style_override,
        state_text=state_text,
        is_brain_chat=is_brain_chat,
    )
    attempt = _ReplyAttemptState(
        max_items=plan.max_context_size,
        prefetched_memory=prefetched_memory_task,
    )
    while True:
        if _is_turn_stale(plan.chat_id, plan.event):
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.stale.abort",
                fields={},
            )
            return None
        trimmed_history = plan.history[-attempt.max_items :] if attempt.max_items > 0 else []
        memory_block = await _load_attempt_memory(plan, attempt, trimmed_history)
        payload_messages = _build_attempt_messages(
            plan,
            attempt,
            trimmed_history,
            memory_block,
        )
        try:
            candidate = await _request_reply_candidate(plan, payload_messages)
        except LLMError as exc:
            error_code = str(exc)
            if error_code == "request_too_large" and attempt.max_items > 2:
                _log_step(
                    plan.context,
                    plan.runtime,
                    chat_id=plan.chat_id,
                    step="reply.llm.too_large",
                    fields={"max_items": attempt.max_items},
                )
                attempt.max_items = max(2, attempt.max_items // 2)
                attempt.cached_memory = None
                attempt.prefetched_memory = None
                continue
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.llm.error",
                fields={"error_type": type(exc).__name__},
            )
            raise
        if candidate is None:
            return None
        raw = candidate
        rejected = _precheck_candidate(plan, raw, trimmed_history)
        if rejected is not None:
            if _queue_reply_regeneration(
                plan,
                attempt,
                rejected,
                step="reply.pre_heuristic.regen",
            ):
                continue
            return _finish_rejected_candidate(plan, rejected)

        draft = await _resolve_candidate_draft(plan, raw, trimmed_history)
        if draft is not None:
            draft = _repair_no_question_draft(plan.text, draft)
            rejected = await _check_candidate_draft(
                plan,
                draft,
                trimmed_history,
                memory_block,
            )
            if rejected is not None:
                if _queue_reply_regeneration(
                    plan,
                    attempt,
                    rejected,
                    step="reply.check.regen",
                ):
                    continue
                return _finish_rejected_candidate(plan, rejected)
            _log_step(
                plan.context,
                plan.runtime,
                chat_id=plan.chat_id,
                step="reply.generate.done",
                fields={
                    "elapsed_s": round(time.monotonic() - plan.started_at, 3),
                    "reply_chars": len(draft.text),
                },
            )
            return draft

        if not plan.forced:
            return None
        raise ReplyRejected("未生成可发送的回复", True)
