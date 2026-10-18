"""构建、生成并审查一轮小青回复候选。"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from core.sensitive_audit import summarize_sensitive

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime

from .chat_event import event_sender_name
from .context_builder import (
    retrieve_memory_context,
)
from .llm.check_result import ReplyCheckResult, ReplyRejected, checker_unavailable
from .llm.gateway import LLMError, chat_completions_with_fallback_paths
from .llm.local_checks import (
    heuristic_check,
    media_meta_reply_check,
)
from .llm.media_evidence import (
    event_has_current_image,
)
from .llm.postprocess import process_llm_response
from .llm.prompt_builder import (
    ChatMessage,
    build_dialogue_prompt,
    build_earlier_context_block,
    build_prompt_messages,
    build_recent_media_block,
)
from .llm.reply_checker import TurnReadingCache, check_reply
from .logging_utils import log_step, safe_correlation_id
from .media.marker_resolver import (
    ResolvedMarker,
    parse_marker,
    resolve_marker,
    strip_marker,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
)
from .memory.local_ids import most_recent_user_local_id, replace_local_ids_with_text
from .message_parts import (
    normalize_message_parts,
)
from .planning.planned_action import PlannedAction
from .reply_draft import (
    RejectedCandidate,
    ReplyAttemptState,
    ReplyDraft,
    attach_reply_media_marker,
    build_reply_draft,
    reply_checker_inputs,
)
from .reply_fallbacks import finish_rejected_candidate, queue_reply_regeneration
from .reply_plan import (
    ReplyGenerationPlan,
    allows_low_stakes_persona_fiction,
    needs_persona_guard_when_checker_unavailable,
    prepare_reply_generation,
)

_REASONING_TURN_RE = re.compile(
    r"(?:\d(?:[\d.,]*\d)?\s*(?:公斤|千克|克|厘米|毫米|米|公里|立方|每立方|%|％|kg|km|cm))"
    r"|(?:密度|半径|直径|重力|加速度|公式|计算|数量级|等于|换算)"
)


# 路由、人物边界与确定性文本修复


def _needs_reasoning_route(text: str) -> bool:
    """数值、单位和科学关系优先交给推理模型，普通闲聊继续使用低延迟 route。"""

    return _REASONING_TURN_RE.search(str(text or "")) is not None


def _is_turn_stale(chat_id: str, event: dict[str, Any]) -> bool:
    local_id = str(event.get("_xc_user_recorded_local_id") or "").strip()
    if not local_id:
        return False
    return most_recent_user_local_id(chat_id) != local_id


# 草稿规范化、媒体合并与审查输入


def _log_prompt_audit_metadata(
    context: Any,
    messages: list[ChatMessage],
    *,
    request_id: str,
) -> None:
    """保留提示词调试指标，但不写入提示词正文。"""

    safe_request_id = safe_correlation_id(request_id)
    for index, message in enumerate(messages):
        content = message.content if isinstance(message.content, str) else ""
        summary = summarize_sensitive(content)
        role    = (
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


# 上下文准备与单次候选请求


def _strip_sender_vocative(text: str, sender: str) -> str:
    """回复本来就接着对方的话，句首再喊一次对方名字显得生硬；只去掉明确的称呼。"""

    name = str(sender or "").strip()
    if len(name) < 2:
        return text
    stripped = re.sub(rf"^\s*{re.escape(name)}\s*[，,、:：]\s*", "", text, count=1)
    return stripped if stripped.strip() else text


def _earlier_context(plan: ReplyGenerationPlan) -> str:
    return build_earlier_context_block(
        plan.earlier_history,
        bot_name=plan.bot_name,
    )


async def _load_attempt_memory(
    plan: ReplyGenerationPlan,
    attempt: ReplyAttemptState,
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
        attempt.cached_memory     = memory_block
    else:
        memory_block = await retrieve_memory_context(
            context          = plan.context,
            runtime          = plan.runtime,
            state            = plan.state,
            secrets          = plan.secrets,
            data_dir         = plan.context.data_dir,
            chat_id          = plan.chat_id,
            history          = trimmed_history,
            current_text     = plan.text,
            planner_question = plan.action.question,
            bot_name         = plan.bot_name,
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
    plan: ReplyGenerationPlan,
    attempt: ReplyAttemptState,
    trimmed_history: Any,
    memory_block: str,
) -> list[dict[str, str]]:
    messages = build_prompt_messages(
        is_private=plan.is_private,
        bot_name=plan.bot_name,
        sender_name=event_sender_name(plan.event),
        think_level=plan.action.think_level,
        history=trimmed_history,
        current_text=plan.text,
        personality=plan.runtime.cfg.personality,
        keyword_rules=plan.keyword_rules,
        regex_rules=plan.regex_rules,
        current_parts=normalize_message_parts(plan.event.get("_xc_effective_user_parts")),
        current_image_attached=event_has_current_image(plan.event),
        memory_block=memory_block,
        expression_habits_block=plan.expression_block,
        jargon_explanation=plan.jargon_explanation,
        tool_info_block=plan.tool_info_block,
        planner_reasoning=replace_local_ids_with_text(
            plan.chat_id,
            plan.merged_reasoning,
            bot_name=plan.bot_name,
        ),
        identity_block       = plan.effective_identity,
        reply_style_override = plan.effective_style,
        state_override       = plan.state_text,
        request_id           = plan.request_id,
        goal                 = plan.effective_goal,
        earlier_context      = _earlier_context(plan),
        media_candidates     = plan.media_candidates,
    )
    if attempt.extra_check_hint:
        messages.append(ChatMessage(role="user", content=attempt.extra_check_hint))
    if plan.runtime.cfg.debug.show_reply_prompt:
        _log_prompt_audit_metadata(plan.context, messages, request_id=plan.request_id)
    plan.state.inc_stats(plan.chat_id, "calls")
    return [{"role": message.role, "content": message.content} for message in messages]


async def _request_reply_candidate(
    plan: ReplyGenerationPlan,
    payload_messages: list[dict[str, str]],
) -> str | None:
    foreground         = plan.foreground
    request_secrets    = plan.secrets
    route_name         = str(plan.secrets.get("_route") or "chat")
    request_max_tokens = int(plan.runtime.cfg.max_tokens)
    if (
        plan.secrets.get("_ai") is not None
        and not plan.secrets.get("_pinned_model")
        and _needs_reasoning_route(plan.text)
    ):
        request_secrets                  = dict(plan.secrets)
        request_secrets["_route"]        = "reasoning"
        request_secrets["_pinned_model"] = None
        route_name                       = "reasoning"
        # DeepSeek 的 max_tokens 同时覆盖隐藏思考和最终回答。普通聊天无需放大，
        # 科学推理若仍用 512，可能只得到 reasoning_content 而没有可发送正文。
        request_max_tokens = max(request_max_tokens, 2048)
    log_step(
        plan.context,
        plan.runtime,
        chat_id = plan.chat_id,
        step    = "reply.llm.start",
        fields  = {
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
        secrets     = request_secrets,
        messages    = payload_messages,
        temperature = plan.chat_temperature,
        top_p       = plan.runtime.cfg.top_p,
        max_tokens  = request_max_tokens,
        **foreground.request_controls(),
    )
    raw = _strip_sender_vocative(raw, event_sender_name(plan.event))
    if _is_turn_stale(plan.chat_id, plan.event):
        log_step(
            plan.context,
            plan.runtime,
            chat_id = plan.chat_id,
            step    = "reply.stale.abort",
            fields  = {},
        )
        return None
    log_step(
        plan.context,
        plan.runtime,
        chat_id = plan.chat_id,
        step    = "reply.llm.ok",
        fields  = {
            "elapsed_s": round(time.monotonic() - started_at, 3),
            "used_profile": used_profile,
            "raw_chars": len(raw or ""),
        },
    )
    return raw


def _precheck_candidate(
    plan: ReplyGenerationPlan,
    raw: str,
    trimmed_history: Any,
) -> RejectedCandidate | None:
    if not raw or not plan.runtime.cfg.reply_check.enable_reply_checker:
        return None
    precheck_text = text_without_outbound_marker(raw)
    raw_parts     = process_llm_response(
        precheck_text,
        plan.runtime.cfg.postprocess,
        bot_name=plan.bot_name,
    )
    raw_draft = build_reply_draft(raw_parts, raw_text=raw, rewritten_text=raw)
    if raw_draft is None:
        return None
    result = heuristic_check(
        reply                = raw_draft.text,
        history              = trimmed_history,
        max_repeat_compare   = plan.runtime.cfg.reply_check.max_repeat_compare,
        similarity_threshold = plan.runtime.cfg.reply_check.similarity_threshold,
        max_assistant_in_row = plan.runtime.cfg.reply_check.max_assistant_in_row,
    )
    if result is None or result.suitable:
        return None
    log_step(
        plan.context,
        plan.runtime,
        chat_id = plan.chat_id,
        step    = "reply.checker.skip",
        fields  = {"stage": "pre_heuristic", "action": "reject", "reason": result.reason},
    )
    return RejectedCandidate(
        text                        = raw_draft.text,
        result                      = result,
        draft                       = raw_draft,
        allow_after_regen_exhausted = not result.is_hard,
    )


async def _resolve_candidate_draft(
    plan: ReplyGenerationPlan,
    raw: str,
    trimmed_history: Any,
) -> ReplyDraft | None:
    parsed_marker                          = parse_marker(raw)
    resolved_marker: ResolvedMarker | None = None
    marker_text                            = raw
    if parsed_marker is not None:
        log_step(
            plan.context,
            plan.runtime,
            chat_id = plan.chat_id,
            step    = "reply.marker.parsed",
            fields  = {"kind": parsed_marker.kind, "hint": parsed_marker.hint},
        )
        marker_text = strip_outbound_marker_residue(strip_marker(raw, parsed_marker.raw_span))
        try:
            resolved_marker = await resolve_marker(
                parsed_marker,
                context     = plan.context,
                runtime     = plan.runtime,
                history     = trimmed_history,
                chat_id     = plan.chat_id,
                media_store = plan.state.media_store,
            )
        except Exception as exc:
            log_step(
                plan.context,
                plan.runtime,
                chat_id = plan.chat_id,
                step    = "reply.marker.miss",
                fields  = {
                    "kind": parsed_marker.kind,
                    "hint": parsed_marker.hint,
                    "reason": type(exc).__name__,
                },
            )
        if resolved_marker is None:
            log_step(
                plan.context,
                plan.runtime,
                chat_id = plan.chat_id,
                step    = "reply.marker.miss",
                fields  = {
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
            log_step(
                plan.context,
                plan.runtime,
                chat_id = plan.chat_id,
                step    = "reply.marker.resolved",
                fields  = {
                    "kind": resolved_marker.kind,
                    "hint": resolved_marker.hint,
                    "entry_id": entry_id,
                },
            )
    else:
        cleaned_marker_text = strip_outbound_marker_residue(raw)
        if cleaned_marker_text != raw:
            marker_text = cleaned_marker_text
            log_step(
                plan.context,
                plan.runtime,
                chat_id = plan.chat_id,
                step    = "reply.marker.miss",
                fields  = {"kind": "", "hint": "", "reason": "parse_failed"},
            )

    parts = process_llm_response(marker_text, plan.runtime.cfg.postprocess, bot_name=plan.bot_name)
    draft = build_reply_draft(parts, raw_text=raw, rewritten_text=marker_text)
    if draft is None and resolved_marker is not None:
        draft = ReplyDraft(
            text           = resolved_marker.marker,
            text_parts     = (),
            parts          = (),
            raw_text       = raw,
            rewritten_text = marker_text,
        )
    if draft is None:
        return None
    return attach_reply_media_marker(
        draft,
        context         = plan.context,
        resolved_marker = resolved_marker,
    )


def _checker_history_text(plan: ReplyGenerationPlan, trimmed_history: Any) -> str:
    dialogue = build_dialogue_prompt(trimmed_history, bot_name=plan.bot_name, truncate=True)
    recent_media = build_recent_media_block(
        trimmed_history, bot_name=plan.bot_name, dialogue=dialogue
    )
    return "\n".join(part for part in (_earlier_context(plan), recent_media, dialogue) if part)


def _checker_grounding_text(plan: ReplyGenerationPlan, memory_block: str) -> str:
    parts = (
        plan.effective_identity,
        plan.state_text,
        plan.profile_block,
        str(memory_block or "")[:1200],
    )
    return "\n".join(part for part in parts if str(part or "").strip())


async def _run_checker(
    plan: ReplyGenerationPlan,
    draft: ReplyDraft,
    trimmed_history: Any,
    memory_block: str,
    turn_reading: TurnReadingCache | None,
) -> ReplyCheckResult:
    """调用审查；超时或异常一律记为审查不可用，由调用方按模式决定是否放行。"""

    cfg = plan.runtime.cfg.reply_check
    check_reply_text, heuristic_reply_text = reply_checker_inputs(draft)
    # 结构性检查已在 check_reply() 的远程调用之前完成。远程审查只占用一个短预算；
    # 超时或协议故障属于基础设施问题，不能触发多轮重生成并拖垮整个插件回调。
    timeout = max(0.1, cfg.timeout_seconds)
    try:
        return await asyncio.wait_for(
            check_reply(
                secrets=plan.secrets,
                bot_name=plan.bot_name,
                reply=check_reply_text,
                heuristic_reply=heuristic_reply_text,
                current_text=plan.text,
                current_sender=event_sender_name(plan.event),
                jargon_explanation=plan.jargon_explanation,
                current_image_attached=event_has_current_image(plan.event),
                goal=plan.effective_goal or plan.merged_reasoning or "自然聊天",
                policy_text=plan.policy_block,
                grounding_text=_checker_grounding_text(plan, memory_block),
                history=trimmed_history,
                chat_history_text=_checker_history_text(plan, trimmed_history),
                enable_llm_checker=cfg.enable_llm_checker,
                verify_turn_semantics=cfg.enable_semantic_checker,
                max_repeat_compare=cfg.max_repeat_compare,
                similarity_threshold=cfg.similarity_threshold,
                max_assistant_in_row=cfg.max_assistant_in_row,
                max_tokens=cfg.max_tokens,
                timeout_seconds=timeout,
                max_retry=0,
                retry_interval_seconds=0.2,
                llm_checker_mode=cfg.llm_checker_mode,
                check_omitted_persona_episode=not plan.forced,
                allow_low_stakes_persona_fiction=allows_low_stakes_persona_fiction(plan.runtime),
                turn_reading=turn_reading,
            ),
            timeout=timeout + 0.25,
        )
    except TimeoutError:
        log_step(
            plan.context, plan.runtime, chat_id=plan.chat_id, step="reply.check.timeout", fields={}
        )
        return checker_unavailable("回复检查暂不可用")
    except Exception as exc:
        log_step(
            plan.context,
            plan.runtime,
            chat_id = plan.chat_id,
            step    = "reply.check.error",
            fields  = {"error_type": type(exc).__name__},
        )
        return checker_unavailable("reply_checker unavailable")


def _apply_unavailable_policy(
    plan: ReplyGenerationPlan, check: ReplyCheckResult
) -> ReplyCheckResult:
    """审查不可用时：always 模式不发未审查的候选；人设可能越界的问题按人设拒绝处理。"""

    if check.severity != "infra":
        return check
    if plan.runtime.cfg.reply_check.llm_checker_mode.strip().lower() == "always":
        return ReplyCheckResult(
            suitable     = False,
            reason       = "回复检查暂不可用",
            need_replan  = False,
            severity     = "infra",
            failure_code = "checker_unavailable",
        )
    if not allows_low_stakes_persona_fiction(
        plan.runtime
    ) and needs_persona_guard_when_checker_unavailable(plan.text, bot_name=plan.bot_name):
        return ReplyCheckResult(
            suitable     = False,
            reason       = "人物依据审查暂不可用",
            need_replan  = True,
            severity     = "hard",
            failure_code = "persona_grounding",
        )
    return check


async def _check_candidate_draft(
    plan: ReplyGenerationPlan,
    draft: ReplyDraft,
    trimmed_history: Any,
    memory_block: str,
    turn_reading: TurnReadingCache | None = None,
) -> RejectedCandidate | None:
    cfg = plan.runtime.cfg.reply_check
    if not cfg.enable_reply_checker:
        return None
    log_step(
        plan.context,
        plan.runtime,
        chat_id = plan.chat_id,
        step    = "reply.check.start",
        fields  = {"llm_checker": cfg.enable_llm_checker},
    )
    check = _apply_unavailable_policy(
        plan, await _run_checker(plan, draft, trimmed_history, memory_block, turn_reading)
    )
    log_step(
        plan.context,
        plan.runtime,
        chat_id = plan.chat_id,
        step    = "reply.check.result",
        fields  = {
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
    check_reply_text, heuristic_reply_text = reply_checker_inputs(draft)
    deterministic_ok = (
        heuristic_check(
            reply                = heuristic_reply_text,
            history              = trimmed_history,
            max_repeat_compare   = cfg.max_repeat_compare,
            similarity_threshold = cfg.similarity_threshold,
            max_assistant_in_row = cfg.max_assistant_in_row,
        )
        is None
        and media_meta_reply_check(reply=heuristic_reply_text, current_text=plan.text) is None
    )
    return RejectedCandidate(
        text                        = check_reply_text,
        result                      = check,
        draft                       = draft,
        allow_after_regen_exhausted = (
            check.failure_code != "checker_unavailable" and not check.is_hard and deterministic_ok
        ),
    )


def _shrink_context_after(
    exc: LLMError, plan: ReplyGenerationPlan, attempt: ReplyAttemptState
) -> bool:
    """请求过大时把历史减半并作废已取的记忆，返回是否应当重试。"""
    if str(exc) != "request_too_large" or attempt.max_items <= 2:
        return False
    log_step(
        plan.context,
        plan.runtime,
        chat_id = plan.chat_id,
        step    = "reply.llm.too_large",
        fields  = {"max_items": attempt.max_items},
    )
    attempt.max_items         = max(2, attempt.max_items // 2)
    attempt.cached_memory     = None
    attempt.prefetched_memory = None
    return True


def _semantic_reading_cache(plan: ReplyGenerationPlan) -> TurnReadingCache | None:
    """开启语义审查时为本轮建一份阅读缓存，供审查和重生成共用。"""
    cfg = plan.runtime.cfg.reply_check
    if cfg.enable_reply_checker and cfg.enable_llm_checker and cfg.enable_semantic_checker:
        return TurnReadingCache()
    return None


def _prefetch_turn_reading(
    plan: ReplyGenerationPlan, attempt: ReplyAttemptState, trimmed_history: Any
) -> None:
    """always 模式每条候选都要语义审查，阅读与候选无关，可与生成同时进行。"""
    cfg = plan.runtime.cfg.reply_check
    if attempt.turn_reading is None or cfg.llm_checker_mode.strip().lower() != "always":
        return
    attempt.turn_reading.prefetch(
        secrets            = plan.secrets,
        bot_name           = plan.bot_name,
        current_text       = plan.text,
        current_sender     = event_sender_name(plan.event),
        chat_history_text  = _checker_history_text(plan, trimmed_history),
        jargon_explanation = plan.jargon_explanation,
        max_tokens         = cfg.max_tokens,
        timeout_seconds    = max(0.1, cfg.timeout_seconds),
    )


async def generate_reply_draft(
    *,
    text: str,
    event: dict[str, Any],
    context,
    runtime: ChatRuntime,
    state,
    forced: bool,
    action: PlannedAction,
    plan_reasoning: str,
    bot_name: str                                    = "",
    secrets: dict[str, Any] | None                   = None,
    reply_style_override: str                        = "",
    state_text: str                                  = "",
    is_brain_chat: bool                              = False,
    prefetched_memory_task: asyncio.Task[str] | None = None,
) -> ReplyDraft | None:
    plan = await prepare_reply_generation(
        text                 = text,
        event                = event,
        context              = context,
        runtime              = runtime,
        state                = state,
        forced               = forced,
        action               = action,
        plan_reasoning       = plan_reasoning,
        bot_name             = bot_name,
        secrets              = secrets,
        reply_style_override = reply_style_override,
        state_text           = state_text,
        is_brain_chat        = is_brain_chat,
    )
    attempt = ReplyAttemptState(
        max_items         = plan.max_context_size,
        prefetched_memory = prefetched_memory_task,
        turn_reading      = _semantic_reading_cache(plan),
    )
    try:
        return await _generate_checked_draft(plan, attempt)
    finally:
        if attempt.turn_reading is not None:
            await attempt.turn_reading.aclose()


async def _generate_checked_draft(
    plan: ReplyGenerationPlan, attempt: ReplyAttemptState
) -> ReplyDraft | None:
    while True:
        if _is_turn_stale(plan.chat_id, plan.event):
            log_step(plan.context, plan.runtime, chat_id=plan.chat_id, step="reply.stale.abort")
            return None
        trimmed_history = plan.history[-attempt.max_items :] if attempt.max_items > 0 else []
        _prefetch_turn_reading(plan, attempt, trimmed_history)
        memory_block = await _load_attempt_memory(plan, attempt, trimmed_history)
        if _is_turn_stale(plan.chat_id, plan.event):
            return None
        payload_messages = _build_attempt_messages(plan, attempt, trimmed_history, memory_block)
        try:
            candidate = await _request_reply_candidate(plan, payload_messages)
        except LLMError as exc:
            if _shrink_context_after(exc, plan, attempt):
                continue
            log_step(
                plan.context,
                plan.runtime,
                chat_id = plan.chat_id,
                step    = "reply.llm.error",
                fields  = {"error_type": type(exc).__name__},
            )
            raise
        if candidate is None:
            return None

        rejected   = _precheck_candidate(plan, candidate, trimmed_history)
        regen_step = "reply.pre_heuristic.regen"
        if rejected is None:
            draft = await _resolve_candidate_draft(plan, candidate, trimmed_history)
            if draft is None:
                if not plan.forced:
                    return None
                raise ReplyRejected("未生成可发送的回复", True)
            rejected = await _check_candidate_draft(
                plan, draft, trimmed_history, memory_block, attempt.turn_reading
            )
            regen_step = "reply.check.regen"
            if rejected is None:
                log_step(
                    plan.context,
                    plan.runtime,
                    chat_id = plan.chat_id,
                    step    = "reply.generate.done",
                    fields  = {
                        "elapsed_s": round(time.monotonic() - plan.started_at, 3),
                        "reply_chars": len(draft.text),
                    },
                )
                return draft
        if queue_reply_regeneration(plan, attempt, rejected, step=regen_step):
            continue
        return finish_rejected_candidate(plan, rejected)
