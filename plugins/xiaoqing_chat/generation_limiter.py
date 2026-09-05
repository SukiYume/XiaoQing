"""按全局、会话、用户和日配额限制并发生成请求。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


class GenerationLimitExceeded(RuntimeError):
    """生成请求超过配置硬限制。"""


@dataclass
class GenerationLimiter:
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _global_inflight: int = 0
    _chat_inflight: dict[str, int] = field(default_factory=dict)
    _user_inflight: dict[str, int] = field(default_factory=dict)
    _user_calls: dict[str, deque[float]] = field(default_factory=dict)
    max_tracked_users: int        = 10_000
    sweep_interval_seconds: float = 300.0
    _last_sweep_at: float         = 0.0

    def _sweep_user_calls(self, cutoff: float) -> None:
        for tracked_user, tracked_calls in tuple(self._user_calls.items()):
            while tracked_calls and tracked_calls[0] < cutoff:
                tracked_calls.popleft()
            if not tracked_calls:
                self._user_calls.pop(tracked_user, None)

    @asynccontextmanager
    async def admit(
        self,
        *,
        chat_id: str,
        user_id: str,
        max_global: int,
        max_per_chat: int,
        max_per_user: int,
        max_calls_per_user_per_day: int,
    ) -> AsyncIterator[None]:
        now    = time.time()
        cutoff = now - 86400.0
        async with self._lock:
            if now - self._last_sweep_at >= max(0.0, self.sweep_interval_seconds) or len(
                self._user_calls
            ) >= max(1, self.max_tracked_users):
                self._sweep_user_calls(cutoff)
                self._last_sweep_at = now
            if user_id not in self._user_calls and len(self._user_calls) >= max(
                1, self.max_tracked_users
            ):
                raise GenerationLimitExceeded("daily_user_capacity")
            calls = self._user_calls.setdefault(user_id, deque())
            while calls and calls[0] < cutoff:
                calls.popleft()
            if max_global > 0 and self._global_inflight >= max_global:
                raise GenerationLimitExceeded("global_inflight")
            if max_per_chat > 0 and self._chat_inflight.get(chat_id, 0) >= max_per_chat:
                raise GenerationLimitExceeded("chat_inflight")
            if max_per_user > 0 and self._user_inflight.get(user_id, 0) >= max_per_user:
                raise GenerationLimitExceeded("user_inflight")
            if max_calls_per_user_per_day > 0 and len(calls) >= max_calls_per_user_per_day:
                raise GenerationLimitExceeded("daily_user_calls")
            self._global_inflight += 1
            self._chat_inflight[chat_id] = self._chat_inflight.get(chat_id, 0) + 1
            self._user_inflight[user_id] = self._user_inflight.get(user_id, 0) + 1
            calls.append(now)
        try:
            yield
        finally:
            async with self._lock:
                self._global_inflight = max(0, self._global_inflight - 1)
                self._decrement(self._chat_inflight, chat_id)
                self._decrement(self._user_inflight, user_id)

    @staticmethod
    def _decrement(values: dict[str, int], key: str) -> None:
        remaining = values.get(key, 0) - 1
        if remaining > 0:
            values[key] = remaining
        else:
            values.pop(key, None)
