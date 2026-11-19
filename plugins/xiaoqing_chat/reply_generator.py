"""构建、生成并审查一轮小青回复候选。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from core.ai_images import image_message_adapter
from core.log import LogEvent

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime

from .chat_event import event_sender_name
from .llm.check_result import ReplyCheckResult, ReplyRejected, checker_unavailable
from .llm.communication_constraints import mentions_length, requested_char_count
from .llm.decorations import strip_junk_tail, tame_decorations
from .llm.gateway import LLMError, chat_completions_with_profile
from .llm.local_checks import (
    media_meta_reply_check,
    narrated_motive_check,
    ungrounded_link_check,
)
from .llm.media_evidence import (
    event_has_current_image,
    media_part_is_readable,
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
from .media.marker_resolver import (
    COMMON_QQ_FACE_LABELS,
    ResolvedMarker,
    parse_marker,
    resolve_marker,
    strip_marker,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
)
from .memory.local_ids import replace_local_ids_with_text, reply_is_stale
from .memory.memory import is_conversation_reply
from .message_parts import normalize_message_parts, render_stored_message
from .planning.planned_action import PlannedAction
from .reply_draft import (
    RejectedCandidate,
    ReplyAttemptState,
    ReplyDraft,
    attach_reply_media_marker,
    build_reply_draft,
    reply_checker_inputs,
)
from .reply_fallbacks import (
    finish_rejected_candidate,
    queue_reply_regeneration,
    trim_unsupported_bubbles,
)
from .reply_plan import (
    ReplyGenerationPlan,
    heuristic_check_with_config,
    needs_persona_guard_when_checker_unavailable,
    prepare_reply_generation,
    with_requested_length_room,
)
from .timing import append_timing

log = logging.getLogger(__name__)

# 这些软问题仍要重写：没照对方明说的要求做（比如篇幅），或者读图结论有出入。
_REWRITE_SOFT_CODES = frozenset({"instruction_following", "media_grounding", "checker_unavailable"})
# 重写用完后这几类仍不发原稿：出戏或答应了线下、读图读错、没审成。其余的（接得不够准、
# 多说了一句没依据的话、没完全照要求）照发最后一稿：比“脑子卡壳了”这类兜底句更像人。
_NEVER_SEND_CODES = frozenset({"persona_grounding", "media_grounding", "checker_unavailable"})

_REASONING_TURN_RE = re.compile(
    r"(?:\d(?:[\d.,]*\d)?\s*(?:公斤|千克|克|厘米|毫米|米|公里|立方|每立方|%|％|kg|km|cm))"
    r"|(?:密度|半径|直径|重力|加速度|公式|计算|数量级|等于|换算)"
)


# 分段、篇幅与路由判断


def _allows_clause_bubbles(text: str) -> bool:
    """对方提到了几句话或多少字时按原句分段，分句气泡会让句数和篇幅看起来不对。"""

    return not mentions_length(text)


# 对方要的篇幅到这个字数，要的就是一篇成品。
_DOCUMENT_CHARS = 100


def _wants_whole_document(text: str) -> bool:
    """对方要一篇有篇幅的成品（“写个800字的检讨”）：整篇一条发出，段落和标点照原样。"""

    length = requested_char_count(text)
    return length is not None and length.mode != "at_most" and length.count >= _DOCUMENT_CHARS


def _split_reply_text(plan: ReplyGenerationPlan, text: str) -> list[str]:
    """按这一轮对篇幅和分段的要求，把候选文本整理成出站气泡。"""

    return process_llm_response(
        text,
        plan.runtime.cfg.postprocess,
        bot_name        = plan.bot_name,
        enable_splitter = not _wants_whole_document(plan.text),
        clause_split    = _allows_clause_bubbles(plan.text),
    )


def _needs_reasoning_route(text: str) -> bool:
    """数值、单位和科学关系优先交给推理模型，普通闲聊继续使用低延迟 route。"""

    return _REASONING_TURN_RE.search(str(text or "")) is not None


def _is_turn_stale(chat_id: str, event: dict[str, Any]) -> bool:
    """回复目标超出历史保留范围时提前结束生成与审查；消息语义在投递前复核。"""

    local_id = str(event.get("_xc_user_recorded_local_id") or "").strip()
    if not local_id:
        return False
    return reply_is_stale(chat_id, local_id)


# 提示词审计日志与生成


def _log_prompt_audit_metadata(messages: list[ChatMessage]) -> None:
    """调试级：每条提示词消息只记角色、长度和指纹，不写入提示词正文。"""

    if not log.isEnabledFor(logging.DEBUG):
        return
    for index, message in enumerate(messages):
        role = (
            message.role if message.role in {"system", "user", "assistant", "tool"} else "unknown"
        )
        content = message.content if isinstance(message.content, str) else ""
        log.debug(LogEvent("reply.prompt", role=role, role_index=index, content=content))


# 上下文准备与单次候选请求


def _strip_sender_vocative(text: str, sender: str) -> str:
    """回复本来就接着对方的话，句首再喊一次对方名字显得生硬；只去掉明确的称呼。"""

    name = str(sender or "").strip()
    if len(name) < 2:
        return text
    stripped = re.sub(rf"^\s*{re.escape(name)}\s*[，,、:：]\s*", "", text, count=1)
    return stripped if stripped.strip() else text


def _recent_reply_texts(plan: ReplyGenerationPlan) -> list[str]:
    """她在这段对话里已经发出的聊天回复（旧→新），用来看装饰是不是刚用过。"""

    return [
        render_stored_message(message) for message in plan.history if is_conversation_reply(message)
    ]


def _earlier_context(plan: ReplyGenerationPlan) -> str:
    return build_earlier_context_block(
        plan.earlier_history,
        bot_name=plan.bot_name,
    )


def _load_attempt_memory(plan: ReplyGenerationPlan) -> str:
    """这一轮交给生成的低信任参考：知识库、复盘策略、说话人页面、群页和相关旧事。"""

    parts = (
        plan.knowledge_block.strip(),
        plan.policy_block.strip(),
        plan.profile_block.strip(),
        plan.memory_block.strip(),
    )
    block = "\n\n".join(part for part in parts if part)
    return block + "\n" if block else ""


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
        persona_facts    = plan.effective_identity,
        persona_style    = plan.effective_style,
        turn_note        = plan.turn_note,
        state_override   = plan.state_text,
        request_id       = plan.request_id,
        goal             = plan.effective_goal,
        earlier_context  = _earlier_context(plan),
        media_candidates = plan.media_candidates,
        life_block       = plan.life_block,
        open_loop_block  = plan.open_loop_block,
    )
    if attempt.extra_check_hint:
        messages.append(ChatMessage(role="user", content=attempt.extra_check_hint))
    _log_prompt_audit_metadata(messages)
    plan.state.inc_stats(plan.chat_id, "calls")
    return [{"role": message.role, "content": message.content} for message in messages]


async def _request_reply_candidate(
    plan: ReplyGenerationPlan,
    payload_messages: list[dict[str, str]],
) -> str | None:
    foreground      = plan.foreground
    request_secrets = plan.secrets
    route_name      = str(plan.secrets.get("_route") or "chat")
    # 计划里的输出预算会为明确要求的长文放宽（见 reply_plan），不低于配置值。
    request_max_tokens = foreground.max_tokens
    if (
        plan.secrets.get("_ai") is not None
        and not plan.secrets.get("_pinned_model")
        and _needs_reasoning_route(plan.text)
    ):
        request_secrets = dict(plan.secrets)
        request_secrets["_route"] = "reasoning"
        request_secrets["_pinned_model"] = None
        route_name = "reasoning"
        foreground = with_requested_length_room(plan.runtime.cfg.llm_config("reasoning"), plan.text)
        request_max_tokens = foreground.max_tokens
    log.info(
        LogEvent(
            "reply.llm.start",
            chat_id    = plan.chat_id,
            model      = plan.model,
            route      = route_name,
            messages   = len(payload_messages),
            timeout_s  = foreground.timeout_seconds,
            max_retry  = foreground.max_retry,
            max_tokens = request_max_tokens,
        )
    )
    started_at     = time.monotonic()
    adapt_messages = None
    ai             = request_secrets.get("_ai")
    if ai is not None and event_has_current_image(plan.event):
        items = plan.event.get("_xc_rendered_media_items") or ()
        paths = [item.cached_path for item in items if item.cached_path is not None]
        if paths:
            parts     = normalize_message_parts(plan.event.get("_xc_effective_user_parts"))
            available = {part.get("file_path") for part in parts if media_part_is_readable(part)}
            # 本轮的话在第一条用户消息里；附图跟着它。
            anchor = next(message for message in payload_messages if message["role"] == "user")
            adapt_messages = await image_message_adapter(
                ai,
                paths,
                anchor,
                description_available=all(str(path) in available for path in paths),
            )
    raw, used_profile = await chat_completions_with_profile(
        secrets        = request_secrets,
        messages       = payload_messages,
        temperature    = foreground.temperature,
        top_p          = foreground.top_p,
        max_tokens     = request_max_tokens,
        adapt_messages = adapt_messages,
        **foreground.request_controls(),
    )
    raw = _strip_sender_vocative(raw, event_sender_name(plan.event))
    raw = strip_junk_tail(
        raw, plan.text + "".join(render_stored_message(message) for message in plan.history)
    )
    raw = tame_decorations(raw, _recent_reply_texts(plan), COMMON_QQ_FACE_LABELS)
    if _is_turn_stale(plan.chat_id, plan.event):
        log.info(LogEvent("reply.stale.abort", chat_id=plan.chat_id))
        return None
    log.info(
        LogEvent(
            "reply.llm.ok",
            chat_id      = plan.chat_id,
            elapsed_s    = round(time.monotonic() - started_at, 3),
            used_profile = used_profile,
            raw_chars    = len(raw or ""),
        )
    )
    return raw


def _link_sources(plan: ReplyGenerationPlan, trimmed_history: Any, memory_block: str) -> list[str]:
    """她能转述的网址和邮箱出自哪里：别人发的消息和插件通知、读图结果、生活记录、工具信息和记忆。

    她自己之前的聊天回复不算：上一条凭空带出的链接不能给下一条当依据。
    """

    said = [
        render_stored_message(message)
        for message in trimmed_history
        if not is_conversation_reply(message)
    ]
    return [
        *said,
        plan.text,
        plan.life_block,
        plan.tool_info_block,
        plan.knowledge_block,
        plan.profile_block,
        memory_block,
    ]


def _precheck_candidate(
    plan: ReplyGenerationPlan,
    raw: str,
    trimmed_history: Any,
    memory_block: str = "",
) -> RejectedCandidate | None:
    if not raw or not plan.runtime.cfg.reply_check.enable_reply_checker:
        return None
    precheck_text = text_without_outbound_marker(raw)
    raw_parts     = _split_reply_text(plan, precheck_text)
    raw_draft = build_reply_draft(raw_parts, raw_text=raw, rewritten_text=raw)
    if raw_draft is None:
        return None
    result = heuristic_check_with_config(
        plan.runtime, reply=raw_draft.text, history=trimmed_history
    )
    if result is None or result.suitable:
        result = ungrounded_link_check(
            reply=raw_draft.text, sources=_link_sources(plan, trimmed_history, memory_block)
        )
    if result is None or result.suitable:
        result = narrated_motive_check(reply=raw_draft.text, current_text=plan.text)
    if result is None or result.suitable:
        return None
    log.info(
        LogEvent(
            "reply.checker.skip",
            chat_id = plan.chat_id,
            stage   = "pre_heuristic",
            action  = "reject",
            reason  = result.reason,
        )
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
        log.info(
            LogEvent(
                "reply.marker.parsed",
                chat_id = plan.chat_id,
                kind    = parsed_marker.kind,
                hint    = parsed_marker.hint,
            )
        )
        marker_text = strip_outbound_marker_residue(strip_marker(raw, parsed_marker.raw_span))
        miss_reason = ""
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
            miss_reason = type(exc).__name__
        else:
            if resolved_marker is None:
                miss_reason = "candidate_not_found"
        if resolved_marker is None:
            log.info(
                LogEvent(
                    "reply.marker.miss",
                    chat_id     = plan.chat_id,
                    kind        = parsed_marker.kind,
                    hint        = parsed_marker.hint,
                    reason_code = miss_reason,
                )
            )
        else:
            entry_id = (
                str(getattr(resolved_marker.entry, "media_hash", "") or "")
                or str(getattr(resolved_marker.entry, "face_id", "") or "")
                or str(getattr(resolved_marker.entry, "media_key", "") or "")
            )
            log.info(
                LogEvent(
                    "reply.marker.resolved",
                    chat_id  = plan.chat_id,
                    kind     = resolved_marker.kind,
                    hint     = resolved_marker.hint,
                    entry_id = entry_id,
                )
            )
    else:
        cleaned_marker_text = strip_outbound_marker_residue(raw)
        if cleaned_marker_text != raw:
            marker_text = cleaned_marker_text
            log.info(
                LogEvent(
                    "reply.marker.miss",
                    chat_id     = plan.chat_id,
                    kind        = "",
                    hint        = "",
                    reason_code = "parse_failed",
                )
            )

    parts = _split_reply_text(plan, marker_text)
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
        plan.life_block,
        plan.open_loop_block,
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
    checker_llm = plan.runtime.cfg.llm_config("checker")
    timeout     = checker_llm.total_timeout_seconds
    try:
        return await asyncio.wait_for(
            check_reply(
                secrets                = plan.secrets,
                bot_name               = plan.bot_name,
                reply                  = check_reply_text,
                heuristic_reply        = heuristic_reply_text,
                current_text           = plan.text,
                current_sender         = event_sender_name(plan.event),
                jargon_explanation     = plan.jargon_explanation,
                current_image_attached = event_has_current_image(plan.event),
                goal                   = plan.effective_goal or plan.merged_reasoning or "自然聊天",
                policy_text            = plan.policy_block,
                grounding_text         = _checker_grounding_text(plan, memory_block),
                memory_text            = "\n".join(
                    part for part in (plan.profile_block, plan.memory_block) if part
                ),
                history                          = trimmed_history,
                chat_history_text                = _checker_history_text(plan, trimmed_history),
                enable_llm_checker               = cfg.enable_llm_checker,
                verify_turn_semantics            = cfg.enable_semantic_checker,
                max_repeat_compare               = cfg.max_repeat_compare,
                similarity_threshold             = cfg.similarity_threshold,
                max_assistant_in_row             = cfg.max_assistant_in_row,
                max_tokens                       = checker_llm.max_tokens,
                timeout_seconds                  = timeout,
                max_retry                        = checker_llm.max_retry,
                retry_interval_seconds           = checker_llm.retry_interval_seconds,
                llm_checker_mode                 = cfg.llm_checker_mode,
                check_omitted_persona_episode    = not plan.forced,
                allow_low_stakes_persona_fiction = (
                    plan.runtime.cfg.personality.allow_low_stakes_persona_fiction
                ),
                turn_reading=turn_reading,
            ),
            timeout=timeout + 0.25,
        )
    except TimeoutError:
        log.info(LogEvent("reply.check.timeout", chat_id=plan.chat_id))
        return checker_unavailable("回复检查暂不可用")
    except Exception as exc:
        log.info(LogEvent("reply.check.error", chat_id=plan.chat_id, error=exc))
        return checker_unavailable("reply_checker unavailable")


def _apply_unavailable_policy(
    plan: ReplyGenerationPlan, check: ReplyCheckResult
) -> ReplyCheckResult:
    """审查不可用时：always 模式不发未审查的候选；人设可能越界的问题按人设拒绝处理。"""

    if check.severity != "infra":
        return check
    if plan.runtime.cfg.reply_check.llm_checker_mode == "always":
        return ReplyCheckResult(
            suitable     = False,
            reason       = "回复检查暂不可用",
            need_replan  = False,
            severity     = "infra",
            failure_code = "checker_unavailable",
        )
    if not plan.runtime.cfg.personality.allow_low_stakes_persona_fiction and (
        needs_persona_guard_when_checker_unavailable(plan.text, bot_name=plan.bot_name)
    ):
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
    log.info(
        LogEvent("reply.check.start", chat_id=plan.chat_id, llm_checker=cfg.enable_llm_checker)
    )
    check = _apply_unavailable_policy(
        plan, await _run_checker(plan, draft, trimmed_history, memory_block, turn_reading)
    )
    log.info(
        LogEvent(
            "reply.check.result",
            chat_id             = plan.chat_id,
            suitable            = bool(check.suitable),
            need_replan         = bool(check.need_replan),
            severity            = check.severity,
            failure_code        = check.failure_code,
            persona_claim_count = check.persona_claim_count,
            context_claim_count = check.context_claim_count,
            reason              = check.reason,
        )
    )
    if check.suitable:
        return None

    # 再跑确定性检查作为纵深防御；LLM 已通过 severity 明确区分事实/人物等硬错误
    # 与轻微风格问题，不能再把所有 LLM 拒绝一概当成可放行的软门禁。
    check_reply_text, heuristic_reply_text = reply_checker_inputs(draft)
    deterministic_ok = (
        heuristic_check_with_config(
            plan.runtime, reply=heuristic_reply_text, history=trimmed_history
        )
        is None
        and media_meta_reply_check(reply=heuristic_reply_text, current_text=plan.text) is None
    )
    if (
        check.severity == "soft"
        and check.failure_code not in _REWRITE_SOFT_CODES
        and deterministic_ok
    ):
        # 软问题是口味上的挑剔（措辞、细节出入、两项审查分歧），照发不重写，免得把人味磨平。
        log.info(
            LogEvent(
                "reply.check.soft_pass",
                chat_id      = plan.chat_id,
                failure_code = check.failure_code,
                reason       = check.reason,
            )
        )
        return None
    return RejectedCandidate(
        text                        = check_reply_text,
        result                      = check,
        draft                       = draft,
        allow_after_regen_exhausted = (
            deterministic_ok
            and check.severity != "infra"
            and (not check.is_hard or check.failure_code not in _NEVER_SEND_CODES)
        ),
    )


def _shrink_context_after(
    exc: LLMError, plan: ReplyGenerationPlan, attempt: ReplyAttemptState
) -> bool:
    """请求过大时把历史减半，返回是否应当重试。"""
    if str(exc) != "request_too_large" or attempt.max_items <= 2:
        return False
    log.info(LogEvent("reply.llm.too_large", chat_id=plan.chat_id, max_items=attempt.max_items))
    attempt.max_items = max(2, attempt.max_items // 2)
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
    if attempt.turn_reading is None or cfg.llm_checker_mode != "always":
        return
    checker_llm = plan.runtime.cfg.llm_config("checker")
    attempt.turn_reading.prefetch(
        secrets                = plan.secrets,
        bot_name               = plan.bot_name,
        current_text           = plan.text,
        current_sender         = event_sender_name(plan.event),
        chat_history_text      = _checker_history_text(plan, trimmed_history),
        jargon_explanation     = plan.jargon_explanation,
        max_tokens             = checker_llm.max_tokens,
        timeout_seconds        = checker_llm.total_timeout_seconds,
        max_retry              = checker_llm.max_retry,
        retry_interval_seconds = checker_llm.retry_interval_seconds,
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
    bot_name: str                  = "",
    secrets: dict[str, Any] | None = None,
    turn_note: str                 = "",
    state_text: str                = "",
    is_brain_chat: bool            = False,
    timings: dict[str, Any] | None = None,
) -> ReplyDraft | None:
    plan = await prepare_reply_generation(
        text           = text,
        event          = event,
        context        = context,
        runtime        = runtime,
        state          = state,
        forced         = forced,
        action         = action,
        plan_reasoning = plan_reasoning,
        bot_name       = bot_name,
        secrets        = secrets,
        turn_note      = turn_note,
        state_text     = state_text,
        is_brain_chat  = is_brain_chat,
    )
    attempt = ReplyAttemptState(
        max_items    = plan.max_context_size,
        turn_reading = _semantic_reading_cache(plan),
        timings      = {} if timings is None else timings,
    )
    try:
        return await _generate_checked_draft(plan, attempt)
    finally:
        attempt.timings["regens"] = int(attempt.timings.get("regens", 0)) + attempt.regen_used
        if attempt.turn_reading is not None:
            await attempt.turn_reading.aclose()


async def _generate_checked_draft(
    plan: ReplyGenerationPlan, attempt: ReplyAttemptState
) -> ReplyDraft | None:
    while True:
        if _is_turn_stale(plan.chat_id, plan.event):
            log.info(LogEvent("reply.stale.abort", chat_id=plan.chat_id))
            return None
        trimmed_history = plan.history[-attempt.max_items :] if attempt.max_items > 0 else []
        _prefetch_turn_reading(plan, attempt, trimmed_history)
        memory_block = _load_attempt_memory(plan)
        if _is_turn_stale(plan.chat_id, plan.event):
            return None
        payload_messages   = _build_attempt_messages(plan, attempt, trimmed_history, memory_block)
        generation_started = time.monotonic()
        try:
            candidate = await _request_reply_candidate(plan, payload_messages)
        except LLMError as exc:
            if _shrink_context_after(exc, plan, attempt):
                continue
            log.info(LogEvent("reply.llm.error", chat_id=plan.chat_id, error=exc))
            raise
        append_timing(attempt.timings, "gen_s", round(time.monotonic() - generation_started, 3))
        if candidate is None:
            return None

        rejected   = _precheck_candidate(plan, candidate, trimmed_history, memory_block)
        regen_step = "reply.pre_heuristic.regen"
        if rejected is None:
            draft = await _resolve_candidate_draft(plan, candidate, trimmed_history)
            if draft is None:
                if not plan.forced:
                    return None
                raise ReplyRejected("未生成可发送的回复")
            check_started = time.monotonic()
            rejected      = await _check_candidate_draft(
                plan, draft, trimmed_history, memory_block, attempt.turn_reading
            )
            if rejected is not None and (trimmed := trim_unsupported_bubbles(rejected)):
                log.info(
                    LogEvent(
                        "reply.check.trimmed",
                        chat_id      = plan.chat_id,
                        failure_code = rejected.result.failure_code,
                        dropped      = len(draft.text_parts) - len(trimmed.text_parts),
                    )
                )
                draft, rejected = trimmed, None
            append_timing(
                attempt.timings,
                "check",
                {
                    "s"          : round(time.monotonic() - check_started, 3),
                    "ok"         : rejected is None,
                    "reason_code": rejected.result.failure_code if rejected else None,
                },
            )
            regen_step = "reply.check.regen"
            if rejected is None:
                log.info(
                    LogEvent(
                        "reply.generate.done",
                        chat_id     = plan.chat_id,
                        elapsed_s   = round(time.monotonic() - plan.started_at, 3),
                        reply_chars = len(draft.text),
                    )
                )
                return draft
        if queue_reply_regeneration(plan, attempt, rejected, step=regen_step):
            continue
        return finish_rejected_candidate(plan, rejected)
