"""定义聊天消息记录，并提供按会话持久化的近期记忆存储。"""

from __future__ import annotations

import asyncio
import math
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.plugin_base import load_json, write_json

from ..message_parts import build_message_parts, message_parts_to_legacy, normalize_message_parts
from ..store_base import coerce_finite_float, coerce_optional_int, delete_json_artifacts

MAX_CACHED_MESSAGES_PER_CHAT = 200


@dataclass(frozen=True)
class StoredMessage:
    role: str
    name: str
    ts: float
    user_id: int | None    = None
    message_id: int | None = None
    local_id: str          = ""
    parts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    content: str = ""
    media_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        normalized_parts = normalize_message_parts(self.parts)
        if not normalized_parts:
            normalized_parts = build_message_parts(
                str(self.content or ""), _normalize_media_items(self.media_items)
            )
        object.__setattr__(self, "parts", normalized_parts)
        legacy_content, legacy_media = message_parts_to_legacy(normalized_parts)
        object.__setattr__(self, "content", legacy_content)
        object.__setattr__(self, "media_items", _normalize_media_items(legacy_media))


def _valid_message_ts(value: Any) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(timestamp) or timestamp <= 0:
        return 0.0
    return timestamp


def active_conversation_suffix(
    history: list[StoredMessage] | tuple[StoredMessage, ...],
    *,
    idle_gap_seconds: float,
) -> list[StoredMessage]:
    """返回最近一次长空档后的连续会话片段。

    原始 ``MemoryStore`` 不会被裁剪；这里只为即时生成与规划建立短期会话边界，
    因而用户明确追溯旧事时仍可走持久记忆检索。
    """

    items     = list(history)
    threshold = max(0.0, float(idle_gap_seconds or 0.0))
    if len(items) < 2 or threshold <= 0:
        return items
    for index in range(len(items) - 1, 0, -1):
        current_ts  = _valid_message_ts(getattr(items[index], "ts", 0.0))
        previous_ts = _valid_message_ts(getattr(items[index - 1], "ts", 0.0))
        if current_ts and previous_ts and current_ts - previous_ts > threshold:
            return items[index:]
    return items


def idle_gap_before_turn(
    history: list[StoredMessage] | tuple[StoredMessage, ...],
    *,
    current_local_id: str = "",
    now: float | None     = None,
) -> float:
    """计算本轮消息与上一条消息之间的空档；没有可靠时间时返回零。"""

    items = list(history)
    if not items:
        return 0.0

    local_id = str(current_local_id or "").strip()
    if local_id:
        for index in range(len(items) - 1, -1, -1):
            if str(getattr(items[index], "local_id", "") or "").strip() != local_id:
                continue
            if index == 0:
                return 0.0
            current_ts  = _valid_message_ts(getattr(items[index], "ts", 0.0))
            previous_ts = _valid_message_ts(getattr(items[index - 1], "ts", 0.0))
            if not current_ts or not previous_ts:
                return 0.0
            return max(0.0, current_ts - previous_ts)

    previous_ts = _valid_message_ts(getattr(items[-1], "ts", 0.0))
    current_ts  = _valid_message_ts(time.time() if now is None else now)
    if not current_ts or not previous_ts:
        return 0.0
    return max(0.0, current_ts - previous_ts)


def _normalize_media_items(values: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, (list, tuple)):
        return ()

    normalized_items: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        normalized: dict[str, Any] = {}
        for key, value in item.items():
            field = str(key or "").strip()
            if not field:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                normalized[field] = value
                continue
            if isinstance(value, (list, tuple)):
                normalized[field] = [
                    element
                    for element in value
                    if isinstance(element, (str, int, float, bool)) or element is None
                ]
                continue
            normalized[field] = str(value)
        if normalized:
            normalized_items.append(normalized)
    return tuple(normalized_items)


def _serialize_message(message: StoredMessage) -> dict[str, Any]:
    parts                   = normalize_message_parts(message.parts)
    payload: dict[str, Any] = {
        "role": str(message.role or ""),
        "name": str(message.name or ""),
        "parts": [dict(part) for part in parts],
        "ts": float(message.ts),
    }
    if message.user_id is not None:
        payload["user_id"] = int(message.user_id)
    if message.message_id is not None:
        payload["message_id"] = int(message.message_id)
    if message.local_id:
        payload["local_id"] = str(message.local_id)
    return payload


class MemoryStore:
    """会话记忆存储，使用按会话隔离的异步锁保护冷加载。

    注意：所有公开方法都是线程安全的（通过内部快照），
    persist() 是同步 I/O，可以在 asyncio.to_thread 中安全调用。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir                                 = data_dir
        self._messages: dict[str, list[StoredMessage]] = {}
        # 同步快照锁：仅用于极短的字典读写，不做 I/O
        self._sync_lock                                                  = threading.Lock()
        self._load_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._write_locks: dict[str, threading.Lock] = {}
        self._generations: dict[str, int]            = {}
        self._tombstones: set[str]                   = set()
        self._dirty: set[str]                        = set()

    def bind_data_dir(self, data_dir: Path) -> None:
        with self._sync_lock:
            if self._data_dir != data_dir:
                self._messages.clear()
                self._load_locks.clear()
                self._write_locks.clear()
                self._generations.clear()
                self._tombstones.clear()
                self._dirty.clear()
            self._data_dir = data_dir

    @staticmethod
    def _trim_history(history: list[StoredMessage]) -> list[StoredMessage]:
        if len(history) <= MAX_CACHED_MESSAGES_PER_CHAT:
            return history
        return history[-MAX_CACHED_MESSAGES_PER_CHAT:]

    def clear(self, chat_id: str) -> None:
        with self._sync_lock:
            write_lock = self._write_locks.setdefault(chat_id, threading.Lock())
        # 持久化线程会持有同一把锁直至原子提交完成；此处等待可确保删除操作
        # 成为旧数据世代的最后一次 I/O。
        with write_lock:
            with self._sync_lock:
                self._generations[chat_id] = self._generations.get(chat_id, 0) + 1
                self._tombstones.add(chat_id)
                self._messages.pop(chat_id, None)
                self._dirty.discard(chat_id)
                data_dir = self._data_dir
            if data_dir:
                path = data_dir / f"{chat_id}.json"
                delete_json_artifacts(path)

    @staticmethod
    def _message_identity(message: StoredMessage) -> tuple[Any, ...]:
        if message.local_id:
            return ("local", message.local_id)
        if message.message_id is not None:
            return ("remote", message.message_id)
        return (
            "fallback",
            message.role,
            message.name,
            message.user_id,
            round(message.ts, 6),
            message.content,
        )

    @classmethod
    def _merge_history(
        cls, loaded: list[StoredMessage], current: list[StoredMessage]
    ) -> list[StoredMessage]:
        merged: list[StoredMessage] = []
        seen: set[tuple[Any, ...]]  = set()
        for message in [*loaded, *current]:
            identity = cls._message_identity(message)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(message)
        merged.sort(key=lambda message: (message.ts, message.local_id))
        return cls._trim_history(merged)

    def append(
        self,
        chat_id: str,
        *,
        role: str,
        name: str,
        user_id: int | None    = None,
        message_id: int | None = None,
        local_id: str          = "",
        content: str           = "",
        media_items: Any       = None,
        parts: Any             = None,
        ts: float | None       = None,
    ) -> None:
        msg = StoredMessage(
            role        = role,
            name        = name,
            user_id     = user_id,
            message_id  = message_id,
            local_id    = local_id or "",
            content     = str(content or ""),
            media_items = media_items,
            parts       = normalize_message_parts(parts),
            ts          = ts if ts is not None else time.time(),
        )
        with self._sync_lock:
            self._tombstones.discard(chat_id)
            self._generations[chat_id] = self._generations.get(chat_id, 0) + 1
            history                    = self._messages.setdefault(chat_id, [])
            history.append(msg)
            self._dirty.add(chat_id)
            if len(history) > MAX_CACHED_MESSAGES_PER_CHAT:
                del history[:-MAX_CACHED_MESSAGES_PER_CHAT]

    def attach_latest_assistant_message_id(self, chat_id: str, message_id: Any) -> bool:
        try:
            normalized_id = int(message_id)
        except (TypeError, ValueError):
            return False
        with self._sync_lock:
            history = self._messages.get(chat_id, [])
            for index in range(len(history) - 1, -1, -1):
                message = history[index]
                if message.role != "assistant" or message.message_id is not None:
                    continue
                history[index] = StoredMessage(
                    role       = message.role,
                    name       = message.name,
                    ts         = message.ts,
                    user_id    = message.user_id,
                    message_id = normalized_id,
                    local_id   = message.local_id,
                    parts      = message.parts,
                )
                self._generations[chat_id] = self._generations.get(chat_id, 0) + 1
                self._dirty.add(chat_id)
                return True
        return False

    def get(self, chat_id: str) -> list[StoredMessage]:
        with self._sync_lock:
            cached     = self._messages.get(chat_id)
            generation = self._generations.get(chat_id, 0)
            tombstoned = chat_id in self._tombstones
        if tombstoned:
            return []
        if cached is None:
            loaded = self._load(chat_id)
            with self._sync_lock:
                current = self._messages.get(chat_id, [])
                if (
                    self._generations.get(chat_id, 0) == generation
                    and chat_id not in self._tombstones
                ):
                    self._messages[chat_id] = self._merge_history(loaded or [], current)
                cached = self._messages.get(chat_id) or []
        return list(cached)

    def _async_load_lock(self, chat_id: str) -> asyncio.Lock:
        with self._sync_lock:
            lock = self._load_locks.get(chat_id)
            if lock is None:
                lock                      = asyncio.Lock()
                self._load_locks[chat_id] = lock
            return lock

    async def get_async(self, chat_id: str) -> list[StoredMessage]:
        async with self._async_load_lock(chat_id):
            with self._sync_lock:
                cached     = self._messages.get(chat_id)
                generation = self._generations.get(chat_id, 0)
                tombstoned = chat_id in self._tombstones
            if tombstoned:
                return []
            if cached is None:
                loaded = await asyncio.to_thread(self._load, chat_id)
                with self._sync_lock:
                    current = self._messages.get(chat_id, [])
                    if (
                        self._generations.get(chat_id, 0) == generation
                        and chat_id not in self._tombstones
                    ):
                        self._messages[chat_id] = self._merge_history(loaded or [], current)
                    cached = self._messages.get(chat_id) or []
        return list(cached)

    async def get_recent_async(self, chat_id: str, *, max_items: int) -> list[StoredMessage]:
        history = await self.get_async(chat_id)
        if max_items <= 0:
            return []
        return history[-max_items:]

    def persist(self, chat_id: str) -> None:
        """同步持久化方法，可在 asyncio.to_thread() 中安全调用。

        仅短暂持有 _sync_lock 获取快照，然后在持锁外做 I/O。
        """
        with self._sync_lock:
            write_lock = self._write_locks.setdefault(chat_id, threading.Lock())
        with write_lock:
            with self._sync_lock:
                data_dir   = self._data_dir
                history    = self._messages.get(chat_id)
                generation = self._generations.get(chat_id, 0)
                if not data_dir or history is None or chat_id in self._tombstones:
                    return
                snapshot = list(history[-200:])
            data_dir.mkdir(parents=True, exist_ok=True)
            path    = data_dir / f"{chat_id}.json"
            payload = [_serialize_message(message) for message in snapshot]
            with self._sync_lock:
                if (
                    self._generations.get(chat_id, 0) != generation
                    or chat_id in self._tombstones
                    or self._messages.get(chat_id) is None
                ):
                    return
            write_json(path, payload)
            with self._sync_lock:
                if (
                    self._generations.get(chat_id, 0) == generation
                    and chat_id not in self._tombstones
                    and self._messages.get(chat_id) is not None
                ):
                    self._dirty.discard(chat_id)

    def persist_all(self) -> None:
        """持久化全部脏会话，供插件关闭时绕过尚未触发的防抖任务。"""
        with self._sync_lock:
            dirty_chat_ids = list(self._dirty)
        for chat_id in dirty_chat_ids:
            self.persist(chat_id)

    def _load(self, chat_id: str) -> list[StoredMessage] | None:
        with self._sync_lock:
            data_dir = self._data_dir
        if not data_dir:
            return None
        path = data_dir / f"{chat_id}.json"
        if not path.exists():
            return None
        try:
            raw = load_json(path, default=None)
        except OSError:
            return None
        if not isinstance(raw, list):
            return None

        out: list[StoredMessage] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role        = str(item.get("role", ""))
            name        = str(item.get("name", ""))
            content     = str(item.get("content", ""))
            media_items = _normalize_media_items(item.get("media_items", []))
            parts       = normalize_message_parts(item.get("parts", []))
            if role and (content or media_items or parts):
                out.append(
                    StoredMessage(
                        role        = role,
                        name        = name,
                        user_id     = coerce_optional_int(item.get("user_id")),
                        message_id  = coerce_optional_int(item.get("message_id")),
                        local_id    = str(item.get("local_id", "") or ""),
                        parts       = parts,
                        content     = content,
                        media_items = media_items,
                        ts=coerce_finite_float(item.get("ts"), default=time.time(), minimum=0.0),
                    )
                )
        return out
