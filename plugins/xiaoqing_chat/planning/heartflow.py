from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..constants import is_question
from ..store_base import StoreBase


@dataclass
class HeartflowState:
    last_user_ts: float = 0.0
    last_bot_ts: float = 0.0
    reply_streak: int = 0
    no_reply_streak: int = 0


class HeartflowEngine(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, HeartflowState] = {}

    def _path(self, chat_id: str) -> Optional[Path]:
        return self._resolve_path("heartflow", f"{chat_id}.json")

    def _load(self, chat_id: str) -> HeartflowState:
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
        if chat_id in self._cache:
            return self._cache[chat_id]
        st = await asyncio.to_thread(self._load, chat_id)
        self._cache[chat_id] = st
        return st

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

    @staticmethod
    def _calculate_score(
        st: HeartflowState,
        *,
        text: str,
        goal: str,
        mentioned: bool,
        is_private: bool,
        replies_last_minute: int,
        max_replies_per_minute: int,
        cooldown_left_seconds: float,
        min_reply_interval_seconds: float,
        seconds_since_last_reply: float,
        base: float,
        weight_private: float = 0.55,
        weight_mentioned: float = 0.45,
        weight_question: float = 0.12,
        weight_goal_match: float = 0.06,
        weight_short_text: float = -0.08,
        weight_rate_limit: float = -0.35,
        weight_cooldown: float = -0.45,
        weight_interval: float = -0.25,
        weight_no_reply_streak: float = 0.05,
        weight_long_silence: float = 0.08,
    ) -> float:
        s = float(base)
        if is_private:
            s += weight_private
        if mentioned:
            s += weight_mentioned
        t = (text or "").strip()
        if is_question(t):
            s += weight_question
        g = (goal or "").strip()
        if g and ("回答" in g or "澄清" in g):
            s += weight_goal_match
        if len(t) <= 2:
            s += weight_short_text
        if max_replies_per_minute > 0 and replies_last_minute >= max_replies_per_minute:
            s += weight_rate_limit
        if cooldown_left_seconds > 0:
            s += weight_cooldown
        if min_reply_interval_seconds > 0 and seconds_since_last_reply < min_reply_interval_seconds:
            s += weight_interval
        if st.no_reply_streak >= 3:
            s += weight_no_reply_streak
        if seconds_since_last_reply > 240:
            s += weight_long_silence
        return max(0.0, min(1.0, s))

    def score(self, *, chat_id: str, **kwargs: Any) -> float:
        st = self._load(chat_id)
        # Remove params not used by _calculate_score
        kwargs.pop("threshold", None)
        kwargs.pop("enable_random", None)
        return self._calculate_score(st, **kwargs)

    async def score_async(self, *, chat_id: str, **kwargs: Any) -> float:
        st = await self.get_async(chat_id)
        kwargs.pop("threshold", None)
        kwargs.pop("enable_random", None)
        return self._calculate_score(st, **kwargs)
