"""Bounded, reference-counted asyncio keyed lock pool."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    lock: asyncio.Lock
    users: int = 0
    owner: asyncio.Task[Any] | None = None
    depth: int = 0


class AsyncKeyedLockPool:
    def __init__(self, *, max_keys: int = 4096, max_key_length: int = 256) -> None:
        if max_keys <= 0 or max_key_length <= 0:
            raise ValueError("keyed lock limits must be positive")
        self.max_keys = max_keys
        self.max_key_length = max_key_length
        self._guard = threading.RLock()
        self._entries: dict[Hashable, _Entry] = {}

    @property
    def active_key_count(self) -> int:
        with self._guard:
            return len(self._entries)

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("keyed locks require an asyncio task")
        normalized = str(key)
        if not normalized or len(normalized) > self.max_key_length:
            raise ValueError("invalid keyed lock key length")
        try:
            hash(key)
        except TypeError as exc:
            raise ValueError("keyed lock keys must be hashable") from exc
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self.max_keys:
                    raise RuntimeError("keyed lock pool capacity exceeded")
                entry = _Entry(asyncio.Lock())
                self._entries[key] = entry
            entry.users += 1
            reentrant = entry.owner is task
            if reentrant:
                entry.depth += 1
        acquired = reentrant
        try:
            if not reentrant:
                await entry.lock.acquire()
                with self._guard:
                    entry.owner = task
                    entry.depth = 1
                acquired = True
            yield
        finally:
            if acquired:
                with self._guard:
                    if entry.owner is not task or entry.depth <= 0:
                        raise RuntimeError("keyed lock released by a non-owner task")
                    entry.depth -= 1
                    release_underlying = entry.depth == 0
                    if release_underlying:
                        entry.owner = None
                if release_underlying:
                    entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    if entry.owner is not None or entry.depth != 0 or entry.lock.locked():
                        raise RuntimeError("idle keyed lock entry still has an owner")
                    if self._entries.get(key) is entry:
                        self._entries.pop(key, None)
