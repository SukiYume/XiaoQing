from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime

from .brain_chat import (
    get_brain_chat_identity,
    get_brain_chat_max_context,
    get_brain_chat_reply_style,
    get_brain_chat_temperature,
    get_brain_chat_think_level,
    is_brain_chat_active,
)
from .config.config import XiaoQingChatConfig
from .constants import (
    EXPRESSION_MAX_INJ_DEFAULT,
    REGENERATION_MAX_ATTEMPTS,
    UNKNOWN_WORDS_MAX,
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
    _find_by_local_id,
    _get_bot_name,
    _get_llm_secrets,
    _is_private,
    _replace_local_ids_with_text,
)
from .logging_utils import _log_step
from .llm.llm_client import LLMError, chat_completions_with_fallback_paths
from .llm.postprocess import join_reply, process_llm_response
from .llm.prompt_builder import ChatMessage, build_dialogue_prompt, build_prompt_messages
from .llm.reply_checker import ReplyCheckResult, ReplyRejected, _heuristic_check, check_reply
from .llm.rewrite import maybe_rewrite_reply
from .memory.memory_retrieval import build_memory_block
from .memory.review_sessions import build_policy_block, get_goal_override
from .planning.goal_state import derive_goal
from .planning.planner import PlannedAction


async def _generate_reply(
    *,
    text: str,
    event: dict[str, Any],
    context,
    runtime: _ChatRuntime,
    state,
    forced: bool,
    action: PlannedAction,
    plan_reasoning: str,
    reply_style_override: str = "",
    state_text: str = "",
    is_brain_chat: bool = False,
    prefetched_memory_task: Optional["asyncio.Task[str]"] = None,
) -> Optional[str]:
    if not context.http_session:
        raise RuntimeError("http_session not available")

    bot_name = _get_bot_name(context)
    chat_id = _chat_id(event)
    is_private = _is_private(event)

    # 检测是否为深度对话模式
    if not is_brain_chat:
        is_brain_chat = is_brain_chat_active(runtime, is_private, forced)

    # 使用深度对话设置或默认设置
    max_context_size = get_brain_chat_max_context(runtime) if is_brain_chat else runtime.cfg.max_context_size
    brain_identity = get_brain_chat_identity(runtime) if is_brain_chat else None
    brain_reply_style = get_brain_chat_reply_style(runtime) if is_brain_chat else None
    brain_temperature = get_brain_chat_temperature(runtime) if is_brain_chat else None

    history = state.memory_store.get_recent(chat_id, max_items=max_context_size)
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

    secrets = _get_llm_secrets(context)
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    proxy = secrets.get("proxy", "") or ""
    endpoint_path = secrets.get("endpoint_path", "") or runtime.cfg.endpoint_path

    max_items = max_context_size
    request_id = str(getattr(context, "request_id", "") or "")
    regen_used = 0
    extra_check_hint = ""
    _prefetched_mem = prefetched_memory_task
    _cached_memory: Optional[str] = None

    while True:
        trimmed_history = history[-max_items:] if max_items > 0 else []

        profile_block = _build_profile_block(state, context.data_dir, chat_id, event)
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
        if profile_block:
            memory_block = (profile_block + "\n" + (memory_block or "")).strip() + "\n"

        state.review_store.bind(context.data_dir)
        if runtime.cfg.reflection.enable_review_sessions:
            policy_block = build_policy_block(state.review_store, chat_id)
            if policy_block.strip():
                memory_block = (policy_block.strip() + "\n\n" + (memory_block or "").strip()).strip() + "\n"

        kb_block = _build_knowledge_block(runtime, state, context.data_dir, chat_id, text)
        if kb_block:
            memory_block = (kb_block + "\n" + memory_block).strip() + "\n"
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

        jargon_explanation = _build_jargon_explanation(runtime, state, context.data_dir, action.unknown_words)

        style_override = (reply_style_override or "").strip()
        if not style_override and runtime.cfg.personality.multiple_reply_style and random.random() < max(0.0, min(1.0, runtime.cfg.personality.multiple_probability)):
            style_override = random.choice(runtime.cfg.personality.multiple_reply_style).strip()

        keyword_rules = []
        regex_rules = []
        src_kw = runtime.cfg.keyword_reaction.keyword_rules
        src_rg = runtime.cfg.keyword_reaction.regex_rules
        for rule in src_kw:
            if rule.keyword and rule.keyword in text and random.random() < max(0.0, min(1.0, rule.probability)):
                keyword_rules.append(rule)
        for rule in src_rg:
            try:
                if rule.pattern and re.search(rule.pattern, text) and random.random() < max(0.0, min(1.0, rule.probability)):
                    regex_rules.append(rule)
            except re.error:
                continue

        st = state.goal_store.get(chat_id)
        tool_info_block = _build_tool_info_block(
            runtime=runtime,
            state=state,
            data_dir=context.data_dir,
            bot_name=bot_name,
            chat_id=chat_id,
            event=event,
            goal=st.goal if runtime.cfg.goal.enable_goal else "",
        )

        # 使用深度对话设置或默认设置
        effective_identity = brain_identity or runtime.cfg.personality.identity
        effective_style = brain_reply_style or style_override or runtime.cfg.personality.reply_style

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
            memory_block=memory_block,
            expression_habits_block=expression_block,
            jargon_explanation=jargon_explanation,
            tool_info_block=tool_info_block,
            planner_reasoning=_replace_local_ids_with_text(chat_id, action.reasoning or plan_reasoning),
            identity_block=effective_identity,
            reply_style_override=effective_style if not reply_style_override else style_override,
            state_override=state_text,
            request_id=request_id,
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
                "timeout_s": float(getattr(runtime.cfg, "foreground_timeout_seconds", runtime.cfg.timeout_seconds)),
                "max_retry": int(getattr(runtime.cfg, "foreground_max_retry", runtime.cfg.max_retry)),
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
                temperature=brain_temperature if is_brain_chat else runtime.cfg.temperature,
                top_p=runtime.cfg.top_p,
                max_tokens=runtime.cfg.max_tokens,
                timeout_seconds=float(getattr(runtime.cfg, "foreground_timeout_seconds", runtime.cfg.timeout_seconds)),
                max_retry=int(getattr(runtime.cfg, "foreground_max_retry", runtime.cfg.max_retry)),
                retry_interval_seconds=float(getattr(runtime.cfg, "foreground_retry_interval_seconds", runtime.cfg.retry_interval_seconds)),
                proxy=proxy,
                endpoint_path=endpoint_path,
            )
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.llm.ok",
                fields={"elapsed_s": round(time.monotonic() - llm_t0, 3), "used_path": _used_path, "raw_chars": len(raw or "")},
            )
        except LLMError as exc:
            if str(exc) == "request_too_large" and max_items > 2:
                _log_step(context, runtime, chat_id=chat_id, step="reply.llm.too_large", fields={"max_items": max_items})
                max_items = max(2, max_items // 2)
                continue
            _log_step(context, runtime, chat_id=chat_id, step="reply.llm.error", fields={"error": str(exc)})
            raise

        # ── Pre-heuristic: fast check on raw reply to skip rewrite if bad ──
        if raw and runtime.cfg.reply_check.enable_reply_checker:
            _raw_parts = process_llm_response(raw, runtime.cfg.postprocess, bot_name=bot_name)
            _raw_reply = join_reply(_raw_parts)
            if _raw_reply:
                _pre_h = _heuristic_check(
                    reply=_raw_reply,
                    history=trimmed_history,
                    bot_name=bot_name,
                    max_repeat_compare=runtime.cfg.reply_check.max_repeat_compare,
                    similarity_threshold=runtime.cfg.reply_check.similarity_threshold,
                    max_assistant_in_row=runtime.cfg.reply_check.max_assistant_in_row,
                )
                if _pre_h is not None and not _pre_h.suitable:
                    _log_step(context, runtime, chat_id=chat_id, step="reply.pre_heuristic.reject",
                              fields={"reason": _pre_h.reason})
                    if _pre_h.need_replan and not forced:
                        raise ReplyRejected(_pre_h.reason or "回复被预检查拒绝", True)
                    if regen_used < max(0, int(runtime.cfg.reply_check.max_regen)):
                        regen_used += 1
                        extra_check_hint = (
                            f"上一条候选回复被检查拒绝:{_pre_h.reason}。\n"
                            "请换一种更自然、更简短的说法，避免重复表达，避免自言自语，也不要刷屏。"
                        ).strip()
                        _log_step(context, runtime, chat_id=chat_id, step="reply.pre_heuristic.regen",
                                  fields={"regen_used": regen_used})
                        continue
                    if forced:
                        return random.choice(["嗯…", "行", "我在听", "你继续", "有点卡，等下"])
                    return None

        try:
            rw_t0 = time.monotonic()
            effective_style = brain_reply_style or runtime.cfg.personality.reply_style
            effective_temp = brain_temperature if is_brain_chat else runtime.cfg.temperature
            rewritten = await asyncio.wait_for(
                maybe_rewrite_reply(
                    http_session=context.http_session,
                    secrets=secrets,
                    cfg=runtime.cfg.rewrite,
                    style=effective_style,
                    user_text=text,
                    reply_text=raw,
                    temperature=effective_temp,
                    top_p=runtime.cfg.top_p,
                    max_tokens=runtime.cfg.max_tokens,
                    timeout_seconds=min(8.0, float(runtime.cfg.timeout_seconds)),
                    max_retry=0,
                    retry_interval_seconds=0.2,
                    proxy=proxy,
                    endpoint_path=endpoint_path,
                ),
                timeout=4.0,
            )
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.rewrite.ok",
                fields={"elapsed_s": round(time.monotonic() - rw_t0, 3), "rewritten_chars": len(rewritten or "")},
            )
        except Exception as exc:
            rewritten = raw
            _log_step(context, runtime, chat_id=chat_id, step="reply.rewrite.skip", fields={})
        parts = process_llm_response(rewritten, runtime.cfg.postprocess, bot_name=bot_name)
        reply = join_reply(parts)
        if reply:
            if runtime.cfg.reply_check.enable_reply_checker:
                _log_step(context, runtime, chat_id=chat_id, step="reply.check.start", fields={"llm_checker": runtime.cfg.reply_check.enable_llm_checker})
                chat_history_text = build_dialogue_prompt(trimmed_history, bot_name=bot_name, truncate=True)
                if runtime.cfg.goal.enable_goal:
                    g = derive_goal(
                        data_dir=context.data_dir,
                        chat_id=chat_id,
                        current_text=text,
                        planner_reasoning=(action.reasoning or plan_reasoning or ""),
                    )
                    if runtime.cfg.reflection.enable_review_sessions:
                        og = get_goal_override(state.review_store, chat_id)
                        if og:
                            g = og
                    state.goal_store.bind(context.data_dir)
                    state.goal_store.set(chat_id, goal=g, source="planner")
                    goal = state.goal_store.get(chat_id).goal or "自然聊天"
                else:
                    goal = (action.reasoning or plan_reasoning or "").strip() or "自然聊天"
                try:
                    check = await asyncio.wait_for(
                        check_reply(
                            http_session=context.http_session,
                            secrets=secrets,
                            bot_name=bot_name,
                            reply=reply,
                            goal=goal,
                            policy_text=(build_policy_block(state.review_store, chat_id) if runtime.cfg.reflection.enable_review_sessions else ""),
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
                        ),
                        timeout=6.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    _log_step(context, runtime, chat_id=chat_id, step="reply.check.timeout", fields={})
                    check = ReplyCheckResult(suitable=True, reason="", need_replan=False)
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.check.result",
                    fields={"suitable": bool(check.suitable), "need_replan": bool(check.need_replan), "reason": getattr(check, "reason", "")},
                )
                if not check.suitable:
                    if check.need_replan and not forced:
                        raise ReplyRejected(check.reason or "回复被检查拒绝", True)
                    if regen_used < max(0, int(runtime.cfg.reply_check.max_regen)):
                        regen_used += 1
                        extra_check_hint = (
                            f"上一条候选回复被检查拒绝:{check.reason}。\n"
                            "请换一种更自然、更简短的说法，避免重复表达，避免自言自语，也不要刷屏。"
                        ).strip()
                        _log_step(context, runtime, chat_id=chat_id, step="reply.check.regen", fields={"regen_used": regen_used})
                        continue
                    if forced:
                        return random.choice(["嗯…", "行", "我在听", "你继续", "有点卡，等下"])
                    return None
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.generate.done",
                fields={"elapsed_s": round(time.monotonic() - t_start, 3), "reply_chars": len(reply)},
            )
            return reply

        if not forced:
            return None
        fallback = random.choice(["嗯…", "行", "我在听", "你继续", "有点卡，等下"])
        return fallback
