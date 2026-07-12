from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from core.public_errors import public_error_message

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime

from .constants import (
    EXPRESSION_MAX_INJ_DEFAULT,
    MEMORY_RETRIEVAL_TIMEOUT,
    UNKNOWN_WORDS_MAX,
)
from .helper_utils import _resolve_llm_config
from .logging_utils import _log_step
from .memory.memory_retrieval import build_memory_block
from .memory.person_profile import build_profile_block
from .utils.tool_info import build_tool_info_block


async def _build_memory_block(
    *,
    context,
    runtime: _ChatRuntime,
    state,
    secrets: dict[str, Any],
    data_dir,
    chat_id: str,
    history,
    current_text: str,
    planner_question: str,
    bot_name: str,
) -> str:
    """
    Build the memory context block by retrieving relevant conversational memories.

    This function performs vector similarity search to find relevant past
    conversations and returns them as a formatted context block for the LLM.

    Args:
        context: The plugin context
        runtime: The chat runtime configuration
        state: The global state object
        secrets: API secrets for LLM calls
        data_dir: The data directory for persistence
        chat_id: The chat/group identifier
        history: The conversation history
        current_text: The current user message
        planner_question: The question from the planner (for retrieval)
        bot_name: The bot's name

    Returns:
        A formatted string containing relevant memories, or empty string if retrieval fails.
    """
    mem_t0 = time.monotonic()
    memory_block = ""
    try:
        fg = _resolve_llm_config(runtime.cfg, secrets, foreground=True)
        memory_block = await asyncio.wait_for(
            build_memory_block(
                data_dir=data_dir,
                chat_id=chat_id,
                http_session=context.http_session,
                secrets=secrets,
                cfg=runtime.cfg.memory,
                bot_name=bot_name,
                history=history,
                current_text=current_text,
                planner_question=planner_question,
                memory_db=state.memory_db,
                temperature=runtime.cfg.temperature,
                top_p=runtime.cfg.top_p,
                max_tokens=runtime.cfg.max_tokens,
                **fg.to_dict(),
            ),
            timeout=MEMORY_RETRIEVAL_TIMEOUT,
        )
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.memory.ok",
            fields={
                "elapsed_s": round(time.monotonic() - mem_t0, 3),
                "memory_chars": len(memory_block or ""),
            },
        )
    except asyncio.TimeoutError:
        memory_block = ""
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.memory.fail",
            fields={
                "elapsed_s": round(time.monotonic() - mem_t0, 3),
                "reason": "timeout",
            },
        )
    except Exception as exc:
        memory_block = ""
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="xiaoqing_chat.memory_context",
        )
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.memory.fail",
            fields={
                "elapsed_s": round(time.monotonic() - mem_t0, 3),
                "reason": type(exc).__name__,
            },
        )
    return memory_block


def _build_profile_block(state, data_dir, chat_id: str, event: dict[str, Any]) -> str:
    """
    Build the user profile context block.

    Args:
        state: The global state object
        data_dir: The data directory for persistence
        chat_id: The chat/group identifier
        event: The OneBot event dictionary

    Returns:
        A formatted string containing user profile information.
    """
    state.memory_db.bind(data_dir)
    return build_profile_block(state.memory_db, chat_id=chat_id, subject_id=event.get("user_id"))


def _build_expression_block(runtime: _ChatRuntime, state, data_dir, chat_id: str) -> str:
    if not runtime.cfg.expression.enable_expression_selector:
        return ""
    state.bw_expr_store.bind(data_dir)
    expr_items = state.bw_expr_store.load()
    candidates = []
    auto_min = max(0, int(getattr(runtime.cfg.expression, "auto_inject_min_count", 0)))
    for ex in expr_items:
        if ex.rejected:
            continue
        if ex.chat_id != chat_id:
            continue
        reflection_cfg = getattr(runtime.cfg, "reflection", None)
        require_checked = bool(
            getattr(reflection_cfg, "enable_expression_reflection", False)
            or getattr(reflection_cfg, "require_approval_for_injection", True)
        )
        if require_checked and not ex.checked:
            # auto_inject_min_count 阈值：count 足够高时跳过审核要求
            if auto_min <= 0 or int(getattr(ex, "count", 0) or 0) < auto_min:
                continue
        candidates.append(ex)
    candidates.sort(key=lambda x: (-x.count, -x.last_active_time))
    max_inj = max(0, int(runtime.cfg.expression.max_injected or EXPRESSION_MAX_INJ_DEFAULT))
    picked = candidates[:max_inj] if max_inj else []
    if not picked:
        return ""
    lines = []
    for ex in picked:
        lines.append(f"- 当{ex.situation}：{ex.style}")
    return "表达习惯（可参考，别生硬照抄）：\n" + "\n".join(lines)


def _build_knowledge_block(runtime: _ChatRuntime, state, data_dir, chat_id: str, text: str) -> str:
    """
    Build the knowledge base context block.

    Queries the vector knowledge base for relevant entries and formats them.

    Args:
        runtime: The chat runtime configuration
        state: The global state object
        data_dir: The data directory for persistence
        chat_id: The chat/group identifier
        text: The text to query against

    Returns:
        A formatted string with relevant knowledge entries, or empty string if none found.
    """
    if not runtime.cfg.knowledge.enable_knowledge or runtime.cfg.knowledge.top_k <= 0:
        return ""
    kb_items = state.memory_db.query_global(
        text,
        top_k=runtime.cfg.knowledge.top_k,
        min_score=runtime.cfg.memory.min_score,
        type_filter="knowledge",
    )
    if not kb_items:
        return ""
    kb_lines = [f"- {it.text.strip()}" for it in kb_items if it.text.strip()]
    if not kb_lines:
        return ""
    kb_block = "你掌握的相关知识：\n" + "\n".join(kb_lines) + "\n"
    return kb_block


def _build_jargon_explanation(
    runtime: _ChatRuntime, state, data_dir, chat_id: str, unknown_words: list[str]
) -> str:
    """
    Build the jargon/slang explanation context block.

    Looks up definitions for unknown words detected by the planner.

    Args:
        runtime: The chat runtime configuration
        state: The global state object
        data_dir: The data directory for persistence
        unknown_words: List of words to look up

    Returns:
        A formatted string with word definitions, or empty string if none found.
    """
    if not unknown_words:
        return ""
    jargon_db = None
    items = []
    for w in unknown_words[:UNKNOWN_WORDS_MAX]:
        hits = state.memory_db.query_global(w, top_k=1, min_score=0.0, type_filter="word_def")
        if hits:
            items.append(hits[0].text.strip())
        else:
            if jargon_db is None:
                state.bw_jargon_store.bind(data_dir)
                jargon_db = state.bw_jargon_store.load()
            rec = jargon_db.get(state.bw_jargon_store.key_for(w, chat_id))
            if rec is None:
                rec = jargon_db.get(state.bw_jargon_store.key_for(w))
            if rec and (rec.is_global or rec.scope_chat_id == chat_id) and rec.meaning:
                items.append(f"{w}：{rec.meaning}".strip())
    if not items:
        return ""
    return "黑话/缩写解释：\n- " + "\n- ".join(items)


async def _build_tool_info_block(
    runtime: _ChatRuntime,
    state,
    data_dir,
    bot_name: str,
    chat_id: str,
    event: dict[str, Any],
    goal: str,
) -> str:
    """Build the tool info context block.

    Note: This is a pure builder — it does NOT modify external state.
    Timestamp window cleanup is the caller's responsibility.
    """
    now = time.time()
    last = state.get_last_reply_ts(chat_id)
    cooldown_until = state.get_continuous_cooldown_until(chat_id)
    cooldown_left = max(0.0, cooldown_until - now)
    # Read-only: filter but don't persist the cleaned window
    window = [t for t in state.get_reply_timestamps(chat_id) if now - t < 60.0]
    recent_actions = []
    for r in await state.action_history.get_recent_async(chat_id, max_items=8):
        ts = time.strftime("%H:%M:%S", time.localtime(r.ts))
        tgt = r.local_target or "-"
        recent_actions.append(f"{ts} {r.action} {tgt} {r.reasoning}".strip())
    return build_tool_info_block(
        data_dir=data_dir,
        bot_name=bot_name,
        chat_id=chat_id,
        event=event,
        goal=goal,
        last_reply_ts=last,
        replies_last_minute=len(window),
        continuous_reply_count=state.get_continuous_reply_count(chat_id),
        cooldown_left_seconds=cooldown_left,
        recent_actions=recent_actions,
    )
