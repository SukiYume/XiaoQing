from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
    _get_bot_name,
    _get_llm_secrets,
    _is_private,
    _most_recent_user_local_id,
    _replace_local_ids_with_text,
    _resolve_llm_config,
)
from .llm.llm_client import LLMError, chat_completions_with_fallback_paths
from .llm.postprocess import join_reply, process_llm_response
from .llm.prompt_builder import ChatMessage, build_dialogue_prompt, build_prompt_messages
from .llm.reply_checker import ReplyCheckResult, ReplyRejected, _heuristic_check, check_reply
from .logging_utils import _log_step
from .media.marker_resolver import (
    ResolvedMarker,
    marker_media_part,
    parse_marker,
    resolve_marker,
    strip_marker,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
)
from .memory.review_sessions import build_policy_block
from .message_parts import (
    build_text_message_parts,
    merge_reply_media_parts,
    normalize_message_parts,
)
from .planning.planner import PlannedAction
from .reply_payload import build_reply_payload_from_parts

_RE_GOAL = re.compile(r"(?:目标|要点|意图)[:：]\s*(.{2,120})")
_SAFE_FORCED_REPLY_FALLBACK = "嗯，我先换个说法。"


def _is_turn_stale(chat_id: str, event: dict[str, Any]) -> bool:
    local_id = str(event.get("_xc_user_recorded_local_id") or "").strip()
    if not local_id:
        return False
    try:
        return _most_recent_user_local_id(chat_id) != local_id
    except Exception:
        return False


@dataclass(frozen=True)
class ReplyDraft:
    text: str
    text_parts: tuple[str, ...]
    parts: tuple[dict[str, Any], ...]
    raw_text: str = ""
    rewritten_text: str = ""
    media_marker: ResolvedMarker | None = None


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


def _forced_reply_draft() -> ReplyDraft:
    return ReplyDraft(
        text=_SAFE_FORCED_REPLY_FALLBACK,
        text_parts=(_SAFE_FORCED_REPLY_FALLBACK,),
        parts=build_text_message_parts(_SAFE_FORCED_REPLY_FALLBACK),
        raw_text="",
        rewritten_text=_SAFE_FORCED_REPLY_FALLBACK,
    )


def _draft_has_media(parts: Any) -> bool:
    return any(
        str(part.get("kind", "") or "").strip() != "text"
        for part in normalize_message_parts(parts)
    )


async def _attach_reply_media_marker(
    draft: ReplyDraft,
    *,
    context,
    runtime,
    history,
    resolved_marker: ResolvedMarker | None = None,
    chat_id: str,
) -> ReplyDraft:
    if resolved_marker is None or _draft_has_media(draft.parts):
        return draft

    media_part = marker_media_part(context, resolved_marker)
    if media_part is None:
        return draft

    merged_parts = merge_reply_media_parts(
        draft.parts,
        (media_part,),
        suppress_text=False,
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
    if not context.http_session:
        raise RuntimeError("http_session not available")

    bot_name = bot_name or _get_bot_name(context)
    chat_id = _chat_id(event)
    is_private = _is_private(event)

    max_context_size = get_brain_chat_max_context(runtime, is_brain_chat)
    chat_temperature = get_brain_chat_temperature(runtime, is_brain_chat)

    history = await state.memory_store.get_recent_async(chat_id, max_items=max_context_size)
    t_start = time.monotonic()
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

    secrets = dict(secrets or _get_llm_secrets(context, chat_id=chat_id))
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    fg = _resolve_llm_config(runtime.cfg, secrets, foreground=True)
    proxy = fg.proxy
    endpoint_path = fg.endpoint_path

    max_items = max_context_size
    request_id = str(getattr(context, "request_id", "") or "")
    regen_used = 0
    extra_check_hint = ""
    _prefetched_mem = prefetched_memory_task
    _cached_memory: str | None = None

    profile_block = _build_profile_block(state, context.data_dir, chat_id, event)
    state.review_store.bind(context.data_dir)
    _cached_policy_block = ""
    if runtime.cfg.reflection.enable_review_sessions:
        _cached_policy_block = build_policy_block(state.review_store, chat_id)

    kb_block = _build_knowledge_block(runtime, state, context.data_dir, chat_id, text)
    if kb_block:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.knowledge.query",
            fields={"kb_hits": len(kb_block or "")},
        )

    expression_block = _build_expression_block(runtime, state, context.data_dir, chat_id)
    if expression_block:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.expression.pick",
            fields={"picked": len(expression_block)},
        )

    jargon_explanation = _build_jargon_explanation(
        runtime, state, context.data_dir, chat_id, action.unknown_words
    )

    style_override = (reply_style_override or "").strip()
    if (
        not style_override
        and runtime.cfg.personality.multiple_reply_style
        and random.random() < max(0.0, min(1.0, runtime.cfg.personality.multiple_probability))
    ):
        style_override = random.choice(runtime.cfg.personality.multiple_reply_style).strip()

    keyword_rules = []
    regex_rules = []
    src_kw = runtime.cfg.keyword_reaction.keyword_rules
    src_rg = runtime.cfg.keyword_reaction.regex_rules
    for rule in src_kw:
        if (
            rule.keyword
            and rule.keyword in text
            and random.random() < max(0.0, min(1.0, rule.probability))
        ):
            keyword_rules.append(rule)
    for rule in src_rg:
        try:
            if (
                rule.pattern
                and re.search(rule.pattern, text)
                and random.random() < max(0.0, min(1.0, rule.probability))
            ):
                regex_rules.append(rule)
        except re.error:
            continue

    merged_reasoning = _merge_planner_reasoning(action.reasoning, plan_reasoning)
    st = await state.goal_store.get_async(chat_id)
    current_goal = st.goal if runtime.cfg.goal.enable_goal and st.goal else ""
    planner_goal = _extract_planner_goal(merged_reasoning)
    effective_goal = planner_goal or current_goal
    tool_info_block = await _build_tool_info_block(
        runtime=runtime,
        state=state,
        data_dir=context.data_dir,
        bot_name=bot_name,
        chat_id=chat_id,
        event=event,
        goal=effective_goal,
    )

    effective_identity = get_brain_chat_identity(runtime, is_brain_chat)
    effective_style = style_override or get_brain_chat_reply_style(runtime, is_brain_chat)

    while True:
        if _is_turn_stale(chat_id, event):
            _log_step(context, runtime, chat_id=chat_id, step="reply.stale.abort", fields={})
            return None
        trimmed_history = history[-max_items:] if max_items > 0 else []

        # Memory block: use prefetched (from parallel PFC), cached (regen), or fetch new
        if _cached_memory is not None:
            memory_block = _cached_memory
        elif _prefetched_mem is not None:
            try:
                memory_block = await _prefetched_mem
            except Exception:
                memory_block = ""
            _prefetched_mem = None
            _cached_memory = memory_block
        else:
            memory_block = await _build_memory_block(
                context=context,
                runtime=runtime,
                state=state,
                secrets=secrets,
                data_dir=context.data_dir,
                chat_id=chat_id,
                history=trimmed_history,
                current_text=text,
                planner_question=action.question,
                bot_name=bot_name,
            )
            _cached_memory = memory_block

        full_memory_block = memory_block
        if profile_block:
            full_memory_block = (profile_block + "\n" + (full_memory_block or "")).strip() + "\n"
        if _cached_policy_block.strip():
            full_memory_block = (
                _cached_policy_block.strip() + "\n\n" + (full_memory_block or "").strip()
            ).strip() + "\n"
        if kb_block:
            full_memory_block = (kb_block + "\n" + (full_memory_block or "")).strip() + "\n"

        msgs = build_prompt_messages(
            is_private=is_private,
            bot_name=bot_name,
            sender_name=_extract_sender_name(event),
            think_level=action.think_level,
            history=trimmed_history,
            current_text=text,
            personality=runtime.cfg.personality,
            keyword_rules=keyword_rules,
            regex_rules=regex_rules,
            current_parts=normalize_message_parts(event.get("_xc_effective_user_parts")),
            memory_block=full_memory_block,
            expression_habits_block=expression_block,
            jargon_explanation=jargon_explanation,
            tool_info_block=tool_info_block,
            planner_reasoning=_replace_local_ids_with_text(chat_id, merged_reasoning),
            identity_block=effective_identity,
            reply_style_override=effective_style,
            state_override=state_text,
            request_id=request_id,
            goal=effective_goal,
        )
        if extra_check_hint:
            msgs.append(ChatMessage(role="user", content=extra_check_hint))
        payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
        if runtime.cfg.debug.show_reply_prompt:
            context.logger.info("reply_prompt.system=%s", msgs[0].content)
            context.logger.info("reply_prompt.user=%s", msgs[1].content)
        state.inc_stats(chat_id, "calls")
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.llm.start",
            fields={
                "model": model,
                "messages": len(payload_msgs),
                "timeout_s": fg.timeout_seconds,
                "max_retry": fg.max_retry,
            },
        )
        llm_t0 = time.monotonic()
        try:
            raw, _used_path = await chat_completions_with_fallback_paths(
                session=context.http_session,
                api_base=api_base,
                api_key=api_key,
                model=model,
                messages=payload_msgs,
                temperature=chat_temperature,
                top_p=runtime.cfg.top_p,
                max_tokens=runtime.cfg.max_tokens,
                **fg.to_dict(),
            )
            if _is_turn_stale(chat_id, event):
                _log_step(context, runtime, chat_id=chat_id, step="reply.stale.abort", fields={})
                return None
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.llm.ok",
                fields={
                    "elapsed_s": round(time.monotonic() - llm_t0, 3),
                    "used_path": _used_path,
                    "raw_chars": len(raw or ""),
                },
            )
        except LLMError as exc:
            if str(exc) == "request_too_large" and max_items > 2:
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.llm.too_large",
                    fields={"max_items": max_items},
                )
                max_items = max(2, max_items // 2)
                _cached_memory = None
                _prefetched_mem = None
                continue
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.llm.error",
                fields={"error": str(exc)},
            )
            raise

        # ── Pre-heuristic: fast local check before checker / replan. ──
        if raw and runtime.cfg.reply_check.enable_reply_checker:
            _precheck_text = text_without_outbound_marker(raw)
            _raw_parts = process_llm_response(_precheck_text, runtime.cfg.postprocess, bot_name=bot_name)
            _raw_draft = _build_reply_draft(_raw_parts, raw_text=raw, rewritten_text=raw)
            if _raw_draft is not None:
                _pre_h = _heuristic_check(
                    reply=_raw_draft.text,
                    history=trimmed_history,
                    bot_name=bot_name,
                    max_repeat_compare=runtime.cfg.reply_check.max_repeat_compare,
                    similarity_threshold=runtime.cfg.reply_check.similarity_threshold,
                    max_assistant_in_row=runtime.cfg.reply_check.max_assistant_in_row,
                )
                if _pre_h is not None and not _pre_h.suitable:
                    _log_step(
                        context,
                        runtime,
                        chat_id=chat_id,
                        step="reply.checker.skip",
                        fields={
                            "stage": "pre_heuristic",
                            "action": "reject",
                            "reason": _pre_h.reason,
                        },
                    )
                    # Always try regen first (with feedback), even for need_replan
                    if regen_used < max(0, int(runtime.cfg.reply_check.max_regen)):
                        regen_used += 1
                        extra_check_hint = (
                            f'上一条候选回复"{_raw_draft.text}"被检查拒绝:{_pre_h.reason}。\n'
                            "请换一种更自然、更贴合对话上下文的说法，避免重复表达，避免自言自语，也不要刷屏。"
                        ).strip()
                        _log_step(
                            context,
                            runtime,
                            chat_id=chat_id,
                            step="reply.pre_heuristic.regen",
                            fields={"regen_used": regen_used},
                        )
                        continue
                    # Regen budget exhausted
                    if _pre_h.need_replan and not forced:
                        raise ReplyRejected(_pre_h.reason or "回复被预检查拒绝", True)
                    if forced:
                        return _forced_reply_draft()
                    return None

        parsed_marker = parse_marker(raw)
        resolved_marker: ResolvedMarker | None = None
        marker_text = raw
        if parsed_marker is not None:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.marker.parsed",
                fields={"kind": parsed_marker.kind, "hint": parsed_marker.hint},
            )
            marker_text = strip_marker(raw, parsed_marker.raw_span)
            try:
                resolved_marker = await resolve_marker(
                    parsed_marker,
                    context=context,
                    runtime=runtime,
                    history=trimmed_history,
                    chat_id=chat_id,
                )
            except Exception as exc:
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.marker.miss",
                    fields={
                        "kind": parsed_marker.kind,
                        "hint": parsed_marker.hint,
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                )
                resolved_marker = None
            if resolved_marker is None:
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
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
                    context,
                    runtime,
                    chat_id=chat_id,
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
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.marker.miss",
                    fields={"kind": "", "hint": "", "reason": "parse_failed"},
                )

        parts = process_llm_response(marker_text, runtime.cfg.postprocess, bot_name=bot_name)
        draft = _build_reply_draft(parts, raw_text=raw, rewritten_text=marker_text)
        if draft is None and resolved_marker is not None:
            draft = ReplyDraft(
                text=resolved_marker.marker,
                text_parts=(),
                parts=(),
                raw_text=raw,
                rewritten_text=marker_text,
            )
        if draft is not None:
            draft = await _attach_reply_media_marker(
                draft,
                context=context,
                runtime=runtime,
                history=trimmed_history,
                resolved_marker=resolved_marker,
                chat_id=chat_id,
            )
            if runtime.cfg.reply_check.enable_reply_checker:
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.check.start",
                    fields={"llm_checker": runtime.cfg.reply_check.enable_llm_checker},
                )
                chat_history_text = build_dialogue_prompt(
                    trimmed_history, bot_name=bot_name, truncate=True
                )
                check_reply_text, heuristic_reply_text = _reply_checker_inputs(draft)
                # 复用已由 handlers.py 设置好的 goal，避免重复 LLM 调用
                goal = effective_goal or merged_reasoning or "自然聊天"
                try:
                    check = await asyncio.wait_for(
                        check_reply(
                            http_session=context.http_session,
                            secrets=secrets,
                            bot_name=bot_name,
                            reply=check_reply_text,
                            heuristic_reply=heuristic_reply_text,
                            current_text=text,
                            goal=goal,
                            policy_text=_cached_policy_block,
                            history=trimmed_history,
                            chat_history_text=chat_history_text,
                            enable_llm_checker=runtime.cfg.reply_check.enable_llm_checker,
                            max_repeat_compare=runtime.cfg.reply_check.max_repeat_compare,
                            similarity_threshold=runtime.cfg.reply_check.similarity_threshold,
                            max_assistant_in_row=runtime.cfg.reply_check.max_assistant_in_row,
                            timeout_seconds=min(6.0, float(runtime.cfg.timeout_seconds)),
                            max_retry=0,
                            retry_interval_seconds=0.2,
                            proxy=proxy,
                            endpoint_path=endpoint_path,
                            extra_payload=getattr(fg, "extra_payload", {}) or {},
                        ),
                        timeout=6.0,
                    )
                except asyncio.TimeoutError:
                    _log_step(
                        context, runtime, chat_id=chat_id, step="reply.check.timeout", fields={}
                    )
                    check = ReplyCheckResult(
                        suitable=False,
                        reason="回复检查暂不可用",
                        need_replan=True,
                    )
                except Exception as exc:
                    _log_step(
                        context,
                        runtime,
                        chat_id=chat_id,
                        step="reply.check.error",
                        fields={"error": f"{type(exc).__name__}: {exc}"},
                    )
                    check = ReplyCheckResult(
                        suitable=False,
                        reason="reply_checker unavailable",
                        need_replan=True,
                    )
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.check.result",
                    fields={
                        "suitable": bool(check.suitable),
                        "need_replan": bool(check.need_replan),
                        "reason": getattr(check, "reason", ""),
                    },
                )
                if not check.suitable:
                    # Always try regen first (with feedback), even for need_replan
                    if regen_used < max(0, int(runtime.cfg.reply_check.max_regen)):
                        regen_used += 1
                        extra_check_hint = (
                            f'上一条候选回复"{check_reply_text}"被检查拒绝:{check.reason}。\n'
                            "请换一种更自然、更贴合对话上下文的说法，避免重复表达，避免自言自语，也不要刷屏。"
                        ).strip()
                        _log_step(
                            context,
                            runtime,
                            chat_id=chat_id,
                            step="reply.check.regen",
                            fields={"regen_used": regen_used},
                        )
                        continue
                    # Regen budget exhausted
                    if check.need_replan and not forced:
                        raise ReplyRejected(check.reason or "回复被检查拒绝", True)
                    if forced:
                        return _forced_reply_draft()
                    return None
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.generate.done",
                fields={
                    "elapsed_s": round(time.monotonic() - t_start, 3),
                    "reply_chars": len(draft.text),
                },
            )
            return draft

        if not forced:
            return None
        return _forced_reply_draft()
