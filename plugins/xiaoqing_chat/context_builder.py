from __future__ import annotations

import asyncio
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime

from .constants import (
    EXPRESSION_MAX_INJ_DEFAULT,
    MEMORY_RETRIEVAL_TIMEOUT,
    UNKNOWN_WORDS_MAX,
)
from .logging_utils import _log_step, _short_text
from .llm.prompt_builder import build_dialogue_prompt
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
    memory_task = asyncio.create_task(
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
            timeout_seconds=float(getattr(runtime.cfg, "foreground_timeout_seconds", runtime.cfg.timeout_seconds)),
            max_retry=int(getattr(runtime.cfg, "foreground_max_retry", runtime.cfg.max_retry)),
            retry_interval_seconds=float(getattr(runtime.cfg, "foreground_retry_interval_seconds", runtime.cfg.retry_interval_seconds)),
            proxy=secrets.get("proxy", "") or "",
            endpoint_path=secrets.get("endpoint_path", "") or runtime.cfg.endpoint_path,
        )
    )
    mem_t0 = time.monotonic()
    memory_block = ""
    try:
        memory_block = await asyncio.wait_for(memory_task, timeout=MEMORY_RETRIEVAL_TIMEOUT)
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.memory.ok",
            fields={"elapsed_s": round(time.monotonic() - mem_t0, 3), "memory_chars": len(memory_block or "")},
        )
    except Exception as exc:
        try:
            memory_task.cancel()
        except Exception:
            pass
        memory_block = ""
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.memory.fail",
            fields={"elapsed_s": round(time.monotonic() - mem_t0, 3)},
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
    for ex in expr_items:
        if ex.rejected:
            continue
        if ex.chat_id != chat_id:
            continue
        if runtime.cfg.reflection.enable_expression_reflection and not ex.checked:
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
    kb_items = state.memory_db.query(
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


def _build_jargon_explanation(runtime: _ChatRuntime, state, data_dir, unknown_words: list) -> str:
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
    state.bw_jargon_store.bind(data_dir)
    jargon_db = state.bw_jargon_store.load()
    items = []
    for w in unknown_words[:UNKNOWN_WORDS_MAX]:
        hits = state.memory_db.query(w, top_k=1, min_score=0.0, type_filter="word_def")
        if hits:
            items.append(hits[0].text.strip())
        else:
            rec = jargon_db.get(w)
            if rec and rec.meaning:
                items.append(f"{w}：{rec.meaning}".strip())
    if not items:
        return ""
    return "黑话/缩写解释：\n- " + "\n- ".join(items)


def _build_tool_info_block(
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
    for r in state.action_history.get_recent(chat_id, max_items=8):
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
