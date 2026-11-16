"""维护会话目标，并从当前消息和历史上下文派生目标。"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..participation import QQ_FACE_TOKEN_RE
from ..store_base import AsyncKeyedStore, coerce_finite_float, delete_json_artifacts


@dataclass
class GoalState:
    ts    : float = 0.0
    goal  : str   = ""
    source: str   = ""


class GoalStore(AsyncKeyedStore[GoalState]):
    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, GoalState] = {}

    def _path(self, chat_id: str) -> Path | None:
        return self._resolve_path("goal_state", f"{chat_id}.json")

    def get(self, chat_id: str) -> GoalState:
        if chat_id in self._cache:
            return self._cache[chat_id]
        st   = GoalState()
        path = self._path(chat_id)
        if path:
            obj = cast(object, self._load_json(path, default=None))
            if isinstance(obj, dict):
                payload = cast(dict[str, object], obj)
                st.ts = coerce_finite_float(payload.get("ts"), default=0.0)
                st.goal   = str(payload.get("goal", "") or "").strip()
                st.source = str(payload.get("source", "") or "").strip()
        self._cache[chat_id] = st
        return st

    def set(self, chat_id: str, *, goal: str, source: str) -> GoalState:
        g = (goal or "").strip()
        if not g:
            return self.get(chat_id)
        if len(g) > 80:
            # 优先在自然停顿处截断，避免把短语切在中间。
            cut = g[:80]
            for sep in ("。", "，", "；", "！", "？", " ", "、"):
                idx = cut.rfind(sep)
                if idx >= 20:  # 至少保留 20 个字符。
                    cut = cut[: idx + 1].rstrip()
                    break
            else:
                cut = cut[:77].rstrip()
            g = cut + "…"
        st = GoalState(ts=time.time(), goal=g, source=(source or "").strip())
        self._cache[chat_id] = st
        path                 = self._path(chat_id)
        if path:
            _ = self._save_json(path, {"ts": st.ts, "goal": st.goal, "source": st.source})
        return st

    async def set_async(self, chat_id: str, *, goal: str, source: str) -> GoalState:
        return await asyncio.to_thread(self.set, chat_id, goal=goal, source=source)

    def clear(self, chat_id: str) -> None:
        _    = self._cache.pop(chat_id, None)
        path = self._path(chat_id)
        if path:
            delete_json_artifacts(path)


_LOW_INFORMATION_TURN_RE = re.compile(
    r"^(?:乐+|哈+|哈哈哈*|呵+|嘿+|草+|啊+|哦+|嗯+|噔噔咚|笑死|行|好|qs)$",
    re.IGNORECASE,
)


def is_low_information_turn(text: str) -> bool:
    """判断不能独立成为话题目标的纯反应消息。"""

    raw = str(text or "").strip()
    if QQ_FACE_TOKEN_RE.fullmatch(raw):
        return True
    normalized = re.sub(r"[\s~～!！?？。,.，…]+", "", raw).strip()
    return bool(normalized and _LOW_INFORMATION_TURN_RE.fullmatch(normalized))


def derive_goal(current_text: str) -> str:
    """根据当前消息推导本轮目标；纯反应消息由调用方先判断（它们保留原目标）。"""
    t = (current_text or "").strip()
    if t and len(t) <= 14:
        return f'围绕"{t}"继续聊'
    return "自然聊天"
