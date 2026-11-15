"""为回复生成组装知识、表达、黑话和工具上下文；记忆和人物资料由统一记忆的取用给出。"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime

from .constants import REPLY_RATE_WINDOW_SECONDS, UNKNOWN_WORDS_MAX
from .memory.memory import is_conversation_reply
from .message_parts import render_stored_message
from .utils.wall_clock import describe_now, format_local


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
    picked = candidates[:1]
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
    kb_items = state.memory_db.query_knowledge(
        text,
        top_k     = runtime.cfg.knowledge.top_k,
        min_score = runtime.cfg.memory.min_score,
    )
    if not kb_items:
        return ""
    kb_lines = [f"- {it.text.strip()}" for it in kb_items if it.text.strip()]
    if not kb_lines:
        return ""
    return "你掌握的相关知识：\n" + "\n".join(kb_lines) + "\n"


def jargon_block(state, data_dir, chat_id: str, text: str = "") -> tuple[str, tuple[str, ...]]:
    """（本群黑话块, 块里按本群已确认的释义解释了的说法）。

    优先读取本群释义，并提供少量可迁移的表达候选。解释了的说法交给网上生活，同一个词不再解释
    第二遍；本群只有猜测的不算，网上生活查实的意思照常给。
    """
    state.bw_jargon_store.bind(data_dir)
    jargon_db  = state.bw_jargon_store.load()
    recent     = state.memory_store.get(chat_id)[-8:] if text else []
    recent_bot = "\n".join(
        render_stored_message(message) for message in recent if is_conversation_reply(message)
    )
    visible = [rec for rec in jargon_db.values() if rec.meaning and rec.visible_to(chat_id)]
    # 主动活用的候选只取释义有依据的；没有依据的释义仅在本轮提到时作为猜测给出。
    scoped = [rec for rec in visible if rec.confirmed]
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
    guesses = [rec.content for rec in visible if not rec.confirmed and rec.content in text]
    selected = list(dict.fromkeys([*[rec.content for rec in candidates[:3] if text], *guesses]))
    items = []
    words: list[str] = []
    for w in selected[:UNKNOWN_WORDS_MAX]:
        rec = jargon_db.get(state.bw_jargon_store.key_for(w, chat_id))
        if rec and rec.visible_to(chat_id) and rec.meaning:
            details = f"{w}：{rec.meaning}"
            if not rec.confirmed:
                details += "（未经本群确认的猜测，不当作共识，也不主动玩这个梗）"
            else:
                if rec.usage:
                    details += f"；适用场景：{rec.usage}"
                if rec.tone:
                    details += f"；情绪：{rec.tone}"
                words.append(w)
            items.append(details)
    if not items:
        return "", ()
    return (
        "本群梗与缩写候选（先匹配含义和情绪，贴合时最多活用一个；自然回应优先，认真和难过时收住玩笑）：\n- "
        + "\n- ".join(items)
    ), tuple(words)


async def tool_status_context(
    state,
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
    window = [t for t in state.get_reply_timestamps(chat_id) if now - t < REPLY_RATE_WINDOW_SECONDS]
    recent_actions = []
    for r in await state.action_history.get_recent_async(chat_id, max_items=8):
        ts  = format_local("%H:%M:%S", r.ts)
        tgt = r.local_target or "-"
        recent_actions.append(f"{ts} {r.action} {tgt} {r.reasoning}".strip())
    return _format_tool_status(
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

    if recent_actions:
        lines.append("- 最近动作：")
        for a in list(recent_actions)[-6:]:
            a = (a or "").strip()
            if a:
                lines.append(f"  - {a}")

    return "\n".join(lines).strip() + "\n"
