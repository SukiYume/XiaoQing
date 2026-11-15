"""维护每个会话的回复节奏，并计算参与意愿的软评分。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..store_base import AsyncKeyedStore, coerce_finite_float, coerce_int, delete_json_artifacts


@dataclass
class HeartflowState:
    """一个会话用于调节回复频率的最小持久状态。"""

    last_user_ts   : float = 0.0
    last_bot_ts    : float = 0.0
    no_reply_streak: int   = 0


class HeartflowEngine(AsyncKeyedStore[HeartflowState]):
    """按会话隔离 Heartflow 状态，并提供异步更新和评分入口。"""

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, HeartflowState] = {}

    def _path(self, chat_id: str) -> Path | None:
        return cast(Path | None, self._resolve_path("heartflow", f"{chat_id}.json"))

    def get(self, chat_id: str) -> HeartflowState:
        if chat_id in self._cache:
            return self._cache[chat_id]
        st   = HeartflowState()
        path = self._path(chat_id)
        if path:
            obj = self._load_json(path, default=None)
            if isinstance(obj, dict):
                st.last_user_ts = coerce_finite_float(obj.get("last_user_ts"), default=0.0)
                st.last_bot_ts = coerce_finite_float(obj.get("last_bot_ts"), default=0.0)
                st.no_reply_streak = coerce_int(obj.get("no_reply_streak"), default=0)
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
            "last_user_ts"   : st.last_user_ts,
            "last_bot_ts"    : st.last_bot_ts,
            "no_reply_streak": st.no_reply_streak,
        }
        self._save_json(path, payload)

    async def get_async(self, chat_id: str) -> HeartflowState:
        cached = self._cache.get(chat_id)
        if cached is not None:
            return cached
        return cast(HeartflowState, await super().get_async(chat_id))

    async def on_user_message_async(self, *, chat_id: str) -> HeartflowState:
        st              = await self.get_async(chat_id)
        st.last_user_ts = time.time()
        await asyncio.to_thread(self._save, chat_id)
        return st

    async def on_bot_reply_async(self, *, chat_id: str) -> HeartflowState:
        st                 = await self.get_async(chat_id)
        st.last_bot_ts     = time.time()
        st.no_reply_streak = 0
        await asyncio.to_thread(self._save, chat_id)
        return st

    async def on_no_reply_async(self, *, chat_id: str) -> HeartflowState:
        st = await self.get_async(chat_id)
        st.no_reply_streak += 1
        await asyncio.to_thread(self._save, chat_id)
        return st

    def clear(self, chat_id: str) -> None:
        self._cache.pop(chat_id, None)
        path = self._path(chat_id)
        if path:
            delete_json_artifacts(path)

    async def score_async(
        self,
        *,
        chat_id: str,
        text: str,
        seconds_since_last_reply: float,
        base: float,
        weight_short_text: float,
        weight_no_reply_streak: float,
        weight_long_silence: float,
    ) -> float:
        """读取当前会话状态，按本轮信号调整基础分，返回归一化到 0~1 的软评分。"""

        st    = await self.get_async(chat_id)
        score = float(base)
        if len((text or "").strip()) <= 2:
            score += weight_short_text
        if st.no_reply_streak >= 3:
            score += weight_no_reply_streak
        if seconds_since_last_reply > 240:
            score += weight_long_silence
        return max(0.0, min(1.0, score))
