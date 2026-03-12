from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..constants import is_question
from ..memory.topic_summary_cache import load_topic_summary_entries
from ..store_base import StoreBase


@dataclass
class GoalState:
    ts: float = 0.0
    goal: str = ""
    source: str = ""


class GoalStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, GoalState] = {}

    def _path(self, chat_id: str) -> Path | None:
        return self._resolve_path("goal_state", f"{chat_id}.json")

    def get(self, chat_id: str) -> GoalState:
        if chat_id in self._cache:
            return self._cache[chat_id]
        st = GoalState()
        path = self._path(chat_id)
        if path:
            obj = cast(object, self._load_json(path, default=None))
            if isinstance(obj, dict):
                payload = cast(dict[str, object], obj)
                ts_raw = payload.get("ts", 0.0)
                if isinstance(ts_raw, (int, float)):
                    st.ts = ts_raw
                elif isinstance(ts_raw, str):
                    st.ts = float(ts_raw) if ts_raw else 0.0
                else:
                    st.ts = 0.0
                st.goal = str(payload.get("goal", "") or "").strip()
                st.source = str(payload.get("source", "") or "").strip()
        self._cache[chat_id] = st
        return st

    def set(self, chat_id: str, *, goal: str, source: str) -> GoalState:
        g = (goal or "").strip()
        if not g:
            return self.get(chat_id)
        if len(g) > 80:
            # Truncate at sentence/phrase boundary to avoid cutting mid-word
            cut = g[:80]
            # Try to find a natural break point
            for sep in ("。", "，", "；", "！", "？", " ", "、"):
                idx = cut.rfind(sep)
                if idx >= 20:  # at least keep 20 chars
                    cut = cut[: idx + 1].rstrip()
                    break
            else:
                cut = cut[:77].rstrip()
            g = cut + "…"
        st = GoalState(ts=time.time(), goal=g, source=(source or "").strip())
        self._cache[chat_id] = st
        path = self._path(chat_id)
        if path:
            _ = self._save_json(path, {"ts": st.ts, "goal": st.goal, "source": st.source})
        return st

    async def get_async(self, chat_id: str) -> GoalState:
        return await asyncio.to_thread(self.get, chat_id)

    async def set_async(self, chat_id: str, *, goal: str, source: str) -> GoalState:
        return await asyncio.to_thread(self.set, chat_id, goal=goal, source=source)

    def clear(self, chat_id: str) -> None:
        _ = self._cache.pop(chat_id, None)
        path = self._path(chat_id)
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    async def clear_async(self, chat_id: str) -> None:
        await asyncio.to_thread(self.clear, chat_id)


def load_latest_topic_and_summary(data_dir: Path, chat_id: str) -> tuple[str, str]:
    entries = load_topic_summary_entries(data_dir, chat_id)
    for item in reversed(entries):
        if item.topic and item.summary:
            return item.topic, item.summary
    return "", ""


def load_latest_topic_summary(data_dir: Path, chat_id: str) -> str:
    topic, _ = load_latest_topic_and_summary(data_dir, chat_id)
    return topic


async def load_latest_topic_and_summary_async(data_dir: Path, chat_id: str) -> tuple[str, str]:
    return await asyncio.to_thread(load_latest_topic_and_summary, data_dir, chat_id)


async def load_latest_topic_summary_async(data_dir: Path, chat_id: str) -> str:
    return await asyncio.to_thread(load_latest_topic_summary, data_dir, chat_id)


_RE_GOAL = re.compile(r"(?:目标|要点|意图)[:：]\s*(.{2,80})")


def _derive_goal_from_context(
    current_text: str,
    planner_reasoning: str,
    topic: str,
) -> str:
    """Pure derivation logic shared by sync and async variants."""
    pr = (planner_reasoning or "").strip()
    if pr:
        m = _RE_GOAL.search(pr)
        if m:
            g = (m.group(1) or "").strip()
            if g:
                return g
        if len(pr) <= 60:
            return pr
    t = (current_text or "").strip()
    if t:
        if is_question(t):
            if len(t) <= 28:
                return f"回答用户问题：{t}"
            return "回答用户问题"
        if len(t) <= 14:
            return f'围绕"{t}"继续聊'
        return "自然聊天"
    if topic:
        return f'围绕话题"{topic}"自然聊天'
    return "自然聊天"


def derive_goal(
    *,
    data_dir: Path,
    chat_id: str,
    current_text: str,
    planner_reasoning: str,
) -> str:
    topic = load_latest_topic_summary(data_dir, chat_id)
    return _derive_goal_from_context(current_text, planner_reasoning, topic)


async def derive_goal_async(
    *,
    data_dir: Path,
    chat_id: str,
    current_text: str,
    planner_reasoning: str,
) -> str:
    topic = await load_latest_topic_summary_async(data_dir, chat_id)
    return _derive_goal_from_context(current_text, planner_reasoning, topic)
