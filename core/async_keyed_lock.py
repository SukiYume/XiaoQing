"""Bounded, reference-counted asyncio keyed lock pool."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class _Entry:
    lock: asyncio.Lock
    users: int = 0


class AsyncKeyedLockPool:
    def __init__(self, *, max_keys: int = 4096, max_key_length: int = 256) -> None:
        if max_keys <= 0 or max_key_length <= 0:
            raise ValueError("keyed lock limits must be positive")
        self.max_keys = max_keys
        self.max_key_length = max_key_length
        self._guard = threading.RLock()
        self._entries: dict[str, _Entry] = {}

    @property
    def active_key_count(self) -> int:
        with self._guard:
            return len(self._entries)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        normalized = str(key)
        if not normalized or len(normalized) > self.max_key_length:
            raise ValueError("invalid keyed lock key length")
        with self._guard:
            entry = self._entries.get(normalized)
            if entry is None:
                if len(self._entries) >= self.max_keys:
                    raise RuntimeError("keyed lock pool capacity exceeded")
                entry = _Entry(asyncio.Lock())
                self._entries[normalized] = entry
            entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(normalized, None)
