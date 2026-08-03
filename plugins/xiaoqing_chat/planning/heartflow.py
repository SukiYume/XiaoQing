from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constants import is_question
from ..store_base import AsyncKeyedStore, delete_json_artifacts

_LEGACY_SCORE_KWARGS = (
    "threshold",
    "enable_random",
    "mentioned",
    "weight_mentioned",
    "is_private",
    "replies_last_minute",
    "max_replies_per_minute",
    "cooldown_left_seconds",
    "min_reply_interval_seconds",
    "weight_private",
    "weight_rate_limit",
    "weight_cooldown",
    "weight_interval",
)


@dataclass
class HeartflowState:
    last_user_ts: float = 0.0
    last_bot_ts: float = 0.0
    reply_streak: int = 0
    no_reply_streak: int = 0


class HeartflowEngine(AsyncKeyedStore[HeartflowState]):
    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, HeartflowState] = {}

    def _path(self, chat_id: str) -> Path | None:
        return self._resolve_path("heartflow", f"{chat_id}.json")

    def get(self, chat_id: str) -> HeartflowState:
        if chat_id in self._cache:
            return self._cache[chat_id]
        st = HeartflowState()
        path = self._path(chat_id)
        if path:
            obj = self._load_json(path, default=None)
            if isinstance(obj, dict):
                st.last_user_ts = float(obj.get("last_user_ts", 0.0) or 0.0)
                st.last_bot_ts = float(obj.get("last_bot_ts", 0.0) or 0.0)
                st.reply_streak = int(obj.get("reply_streak", 0) or 0)
                st.no_reply_streak = int(obj.get("no_reply_streak", 0) or 0)
        self._cache[chat_id] = st
        return st

    def _save(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if not path:
            return
        st = self._cache.get(chat_id)
        if not st:
            return
        payload = {
            "last_user_ts": st.last_user_ts,
            "last_bot_ts": st.last_bot_ts,
            "reply_streak": st.reply_streak,
            "no_reply_streak": st.no_reply_streak,
        }
        self._save_json(path, payload)

    async def get_async(self, chat_id: str) -> HeartflowState:
        cached = self._cache.get(chat_id)
        if cached is not None:
            return cached
        return await super().get_async(chat_id)

    async def on_user_message_async(self, *, chat_id: str) -> HeartflowState:
        st = await self.get_async(chat_id)
        st.last_user_ts = time.time()
        await asyncio.to_thread(self._save, chat_id)
        return st

    async def on_bot_reply_async(self, *, chat_id: str) -> HeartflowState:
        st = await self.get_async(chat_id)
        st.last_bot_ts = time.time()
        st.reply_streak += 1
        st.no_reply_streak = 0
        await asyncio.to_thread(self._save, chat_id)
        return st

    async def on_no_reply_async(self, *, chat_id: str) -> HeartflowState:
        st = await self.get_async(chat_id)
        st.no_reply_streak += 1
        st.reply_streak = 0
        await asyncio.to_thread(self._save, chat_id)
        return st

    def clear(self, chat_id: str) -> None:
        self._cache.pop(chat_id, None)
        path = self._path(chat_id)
        if path:
            delete_json_artifacts(path)

    @staticmethod
    def _calculate_score(
        st: HeartflowState,
        *,
        text: str,
        goal: str,
        seconds_since_last_reply: float,
        base: float,
        weight_question: float = 0.12,
        weight_goal_match: float = 0.06,
        weight_short_text: float = -0.08,
        weight_no_reply_streak: float = 0.05,
        weight_long_silence: float = 0.08,
    ) -> float:
        s = float(base)
        t = (text or "").strip()
        if is_question(t):
            s += weight_question
        g = (goal or "").strip()
        if g and ("回答" in g or "澄清" in g):
            s += weight_goal_match
        if len(t) <= 2:
            s += weight_short_text
        if st.no_reply_streak >= 3:
            s += weight_no_reply_streak
        if seconds_since_last_reply > 240:
            s += weight_long_silence
        return max(0.0, min(1.0, s))

    def score(self, *, chat_id: str, **kwargs: Any) -> float:
        st = self.get(chat_id)
        # 丢弃已经移到 heartflow 之前处理的旧门控参数。
        _drop_legacy_score_kwargs(kwargs)
        return self._calculate_score(st, **kwargs)

    async def score_async(self, *, chat_id: str, **kwargs: Any) -> float:
        st = await self.get_async(chat_id)
        _drop_legacy_score_kwargs(kwargs)
        return self._calculate_score(st, **kwargs)


def _drop_legacy_score_kwargs(kwargs: dict[str, Any]) -> None:
    for name in _LEGACY_SCORE_KWARGS:
        kwargs.pop(name, None)
