"""为回复生成组装记忆、资料、知识、表达和工具上下文。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.public_errors import public_error_message

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime

from .chat_runtime import llm_call_config
from .constants import (
    EXPRESSION_MAX_INJ_DEFAULT,
    MEMORY_RETRIEVAL_TIMEOUT,
    UNKNOWN_WORDS_MAX,
)
from .logging_utils import log_step
from .memory.memory import is_conversation_reply
from .memory.memory_retrieval import build_memory_block
from .memory.person_profile import build_profile_block
from .message_parts import render_stored_message
from .planning.goal_state import load_latest_topic_and_summary
from .utils.wall_clock import describe_now, format_local


async def retrieve_memory_context(
    *,
    context,
    runtime: ChatRuntime,
    state,
    secrets: dict[str, Any],
    data_dir,
    chat_id: str,
    history,
    current_text: str,
    planner_question: str,
    bot_name: str,
) -> str:
    """检索相关会话记忆，并构造成供模型使用的上下文片段。"""
    mem_t0       = time.monotonic()
    memory_block = ""
    try:
        llm = llm_call_config(runtime.cfg, foreground=False)
        memory_block = await asyncio.wait_for(
            build_memory_block(
                data_dir         = data_dir,
                chat_id          = chat_id,
                secrets          = secrets,
                cfg              = runtime.cfg.memory,
                bot_name         = bot_name,
                history          = history,
                current_text     = current_text,
                planner_question = planner_question,
                memory_db        = state.memory_db,
                llm              = llm,
            ),
            timeout=MEMORY_RETRIEVAL_TIMEOUT,
        )
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "reply.memory.ok",
            fields  = {
                "elapsed_s"   : round(time.monotonic() - mem_t0, 3),
                "memory_chars": len(memory_block or ""),
            },
        )
    except TimeoutError:
        memory_block = ""
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "reply.memory.fail",
            fields  = {
                "elapsed_s": round(time.monotonic() - mem_t0, 3),
                "reason"   : "timeout",
            },
        )
    except Exception as exc:
        memory_block = ""
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "xiaoqing_chat.memory_context",
        )
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "reply.memory.fail",
            fields  = {
                "elapsed_s": round(time.monotonic() - mem_t0, 3),
                "reason"   : type(exc).__name__,
            },
        )
    return memory_block


def sender_profile_context(state, data_dir, chat_id: str, event: dict[str, Any]) -> str:
    """读取当前发送者画像并构造上下文片段。"""
    state.memory_db.bind(data_dir)
    return build_profile_block(state.memory_db, chat_id=chat_id, subject_id=event.get("user_id"))


def _context_overlap(query: str, description: str) -> int:
    """用词组和汉字二元组筛选候选，最终语义适配由回复模型判断。"""

    def tokens(value: str) -> set[str]:
        words = set(re.findall(r"[a-z0-9_]{2,}", value.casefold()))
        for phrase in re.findall(r"[\u4e00-\u9fff]+", value):
            words.update(phrase[i : i + 2] for i in range(len(phrase) - 1))
        return words - {"自己", "这个", "一个", "时候", "可以", "就是", "什么", "我们"}

    return len(tokens(query) & tokens(description))


def expression_context(runtime: ChatRuntime, state, data_dir, chat_id: str, text: str = "") -> str:
    if not runtime.cfg.expression.enable_expression_selector:
        return ""
    state.bw_expr_store.bind(data_dir)
    expr_items = state.bw_expr_store.load()
    candidates = []
    recent     = state.memory_store.get(chat_id)[-8:] if text else []
    recent_bot = "\n".join(
        render_stored_message(message) for message in recent if is_conversation_reply(message)
    )
    for ex in expr_items:
        if ex.rejected:
            continue
        if ex.chat_id != chat_id:
            continue
        require_checked = runtime.cfg.reflection.require_approval_for_injection
        if require_checked and not ex.checked:
            # 表达先通过自动自审或管理员审查，使用次数保持独立。
            continue
        relevance = _context_overlap(text, ex.situation)
        if text and (relevance == 0 or ex.style in recent_bot):
            continue
        candidates.append((relevance, ex))
    candidates.sort(key=lambda item: (-item[0], -item[1].last_active_time, -item[1].count))
    # 每轮只给一个贴合语境的表达提示。
    max_inj = min(
        1,
        max(0, int(runtime.cfg.expression.max_injected or EXPRESSION_MAX_INJ_DEFAULT)),
    )
    picked = candidates[:max_inj] if max_inj else []
    if not picked:
        return ""
    lines = []
    for _, ex in picked:
        situation = str(ex.situation or "").strip()
        prefix    = "" if situation.startswith("当") else "当"
        lines.append(f"- {prefix}{situation}：{ex.style}")
    return "表达习惯（可参考，别生硬照抄）：\n" + "\n".join(lines)


def knowledge_context(runtime: ChatRuntime, state, text: str) -> str:
    """查询全局知识库并构造提示词片段。"""
    if not runtime.cfg.knowledge.enable_knowledge or runtime.cfg.knowledge.top_k <= 0:
        return ""
    kb_items = state.memory_db.query_global(
        text,
        top_k       = runtime.cfg.knowledge.top_k,
        min_score   = runtime.cfg.memory.min_score,
        type_filter = "knowledge",
    )
    if not kb_items:
        return ""
    kb_lines = [f"- {it.text.strip()}" for it in kb_items if it.text.strip()]
    if not kb_lines:
        return ""
    return "你掌握的相关知识：\n" + "\n".join(kb_lines) + "\n"


def jargon_context(state, data_dir, chat_id: str, unknown_words: list[str], text: str = "") -> str:
    """优先读取本群释义，并提供少量可迁移的表达候选。"""
    state.bw_jargon_store.bind(data_dir)
    jargon_db  = state.bw_jargon_store.load()
    recent     = state.memory_store.get(chat_id)[-8:] if text else []
    recent_bot = "\n".join(
        render_stored_message(message) for message in recent if is_conversation_reply(message)
    )
    scoped = [
        rec
        for rec in jargon_db.values()
        if rec.meaning and (rec.is_global or rec.scope_chat_id == chat_id)
    ]
    scoped.sort(
        key=lambda rec: (
            rec.content in text,
            _context_overlap(text, rec.meaning + " " + rec.usage),
            rec.updated_at,
        ),
        reverse=True,
    )
    # 小批候选让模型判断场景迁移；近期用过的梗仅在本轮明确提及时重现。
    candidates = [rec for rec in scoped if rec.content in text or rec.content not in recent_bot]
    selected   = list(
        dict.fromkeys([*unknown_words, *[rec.content for rec in candidates[:3] if text]])
    )
    items = []
    for w in selected[:UNKNOWN_WORDS_MAX]:
        rec = jargon_db.get(state.bw_jargon_store.key_for(w, chat_id)) or jargon_db.get(
            state.bw_jargon_store.key_for(w)
        )
        if rec and (rec.is_global or rec.scope_chat_id == chat_id) and rec.meaning:
            details = f"{w}：{rec.meaning}"
            if rec.usage:
                details += f"；适用场景：{rec.usage}"
            if rec.tone:
                details += f"；情绪：{rec.tone}"
            items.append(details)
        elif w in unknown_words:
            hits = state.memory_db.query_global(w, top_k=1, min_score=0.0, type_filter="word_def")
            items.extend(hit.text.strip() for hit in hits)
    if not items:
        return ""
    return (
        "本群梗与缩写候选（先匹配含义和情绪，贴合时最多活用一个；自然回应优先，认真和难过时收住玩笑）：\n- "
        + "\n- ".join(items)
    )


async def tool_status_context(
    state,
    data_dir,
    bot_name: str,
    chat_id: str,
    event: dict[str, Any],
    goal: str,
) -> str:
    """只读构造工具状态片段；时间窗口清理由调用方负责。"""
    now            = time.time()
    last           = state.get_last_reply_ts(chat_id)
    cooldown_until = state.get_continuous_cooldown_until(chat_id)
    cooldown_left  = max(0.0, cooldown_until - now)
    # 这里只过滤快照，不在构造提示词时修改持久状态。
    window         = [t for t in state.get_reply_timestamps(chat_id) if now - t < 60.0]
    recent_actions = []
    for r in await state.action_history.get_recent_async(chat_id, max_items=8):
        ts  = format_local("%H:%M:%S", r.ts)
        tgt = r.local_target or "-"
        recent_actions.append(f"{ts} {r.action} {tgt} {r.reasoning}".strip())
    return _format_tool_status(
        data_dir               = data_dir,
        bot_name               = bot_name,
        chat_id                = chat_id,
        event                  = event,
        goal                   = goal,
        last_reply_ts          = last,
        replies_last_minute    = len(window),
        continuous_reply_count = state.get_continuous_reply_count(chat_id),
        cooldown_left_seconds  = cooldown_left,
        recent_actions         = recent_actions,
    )


def _format_tool_status(
    *,
    data_dir: Path,
    bot_name: str,
    chat_id: str,
    event: dict[str, Any],
    goal: str = "",
    last_reply_ts: float,
    replies_last_minute: int,
    continuous_reply_count: int,
    cooldown_left_seconds: float,
    recent_actions: Sequence[str],
) -> str:
    now      = describe_now()
    group_id = event.get("group_id")
    user_id  = event.get("user_id")
    channel  = "私聊" if group_id is None else f"群聊(g{group_id})"

    lines: list[str] = []
    lines.append("可用工具信息：")
    lines.append(f"- 当前时间：{now}")
    lines.append(f"- 场景：{channel}")
    lines.append(f"- 会话：{chat_id}")
    if user_id is not None:
        lines.append(f"- 说话人：{user_id}")
    lines.append(f"- 机器人：{bot_name}")
    if goal.strip():
        lines.append(f"- 当前目标：{goal.strip()}")

    since = max(0.0, time.time() - float(last_reply_ts or 0.0)) if last_reply_ts else 0.0
    lines.append(
        f"- 频率：距上次回复{since:.0f}s，60秒内回复{int(replies_last_minute)}，连续回复{int(continuous_reply_count)}，冷却剩余{max(0.0, cooldown_left_seconds):.0f}s"
    )

    topic, summary = load_latest_topic_and_summary(data_dir, chat_id)
    topic_text = ""
    if topic and summary:
        topic_text = f"最新话题：{topic}\n摘要：{summary}"
    elif topic:
        topic_text = f"最新话题：{topic}"
    if topic_text:
        lines.append("- " + topic_text.replace("\n", "\n  "))

    if recent_actions:
        lines.append("- 最近动作：")
        for a in list(recent_actions)[-6:]:
            a = (a or "").strip()
            if a:
                lines.append(f"  - {a}")

    return "\n".join(lines).strip() + "\n"
