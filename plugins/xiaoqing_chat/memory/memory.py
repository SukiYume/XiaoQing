from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.plugin_base import write_json
from ..message_parts import build_message_parts, message_parts_to_legacy, normalize_message_parts


MAX_CACHED_MESSAGES_PER_CHAT = 200


@dataclass(frozen=True)
class StoredMessage:
    role: str
    name: str
    ts: float
    user_id: Optional[int] = None
    message_id: Optional[int] = None
    local_id: str = ""
    parts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    content: str = ""
    media_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        normalized_parts = _normalize_parts(self.parts)
        if not normalized_parts:
            normalized_parts = build_message_parts(
                str(self.content or ""), _normalize_media_items(self.media_items)
            )
        object.__setattr__(self, "parts", normalized_parts)
        legacy_content, legacy_media = message_parts_to_legacy(normalized_parts)
        object.__setattr__(self, "content", legacy_content)
        object.__setattr__(self, "media_items", _normalize_media_items(legacy_media))


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


def _normalize_parts(values: Any) -> tuple[dict[str, Any], ...]:
    return normalize_message_parts(values)


def _serialize_message(message: StoredMessage) -> dict[str, Any]:
    parts = _normalize_parts(message.parts)
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
    """会话记忆存储，使用 asyncio 兼容的锁保护内部状态。

    注意：所有公开方法都是线程安全的（通过内部快照），
    persist() 是同步 I/O，可以在 asyncio.to_thread 中安全调用。
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._data_dir = data_dir
        self._messages: dict[str, list[StoredMessage]] = {}
        # 使用普通 Lock 而非 RLock，仅保护内存字典的短临界区，
        # 不在持有锁时执行 I/O，避免阻塞事件循环。
        self._lock = asyncio.Lock()
        # 同步快照锁：仅用于极短的字典读写，不做 I/O
        self._sync_lock = threading.Lock()
        self._write_locks: dict[str, threading.Lock] = {}

    def bind_data_dir(self, data_dir: Path) -> None:
        with self._sync_lock:
            if self._data_dir != data_dir:
                self._messages.clear()
                self._write_locks.clear()
            self._data_dir = data_dir

    @staticmethod
    def _trim_history(history: list[StoredMessage]) -> list[StoredMessage]:
        if len(history) <= MAX_CACHED_MESSAGES_PER_CHAT:
            return history
        return history[-MAX_CACHED_MESSAGES_PER_CHAT:]

    def clear(self, chat_id: str) -> None:
        with self._sync_lock:
            self._messages.pop(chat_id, None)
            data_dir = self._data_dir
        if data_dir:
            path = data_dir / f"{chat_id}.json"
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

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
        seen: set[tuple[Any, ...]] = set()
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
        user_id: Optional[int] = None,
        message_id: Optional[int] = None,
        local_id: str = "",
        content: str = "",
        media_items: Any = None,
        parts: Any = None,
        ts: Optional[float] = None,
    ) -> None:
        msg = StoredMessage(
            role=role,
            name=name,
            user_id=user_id,
            message_id=message_id,
            local_id=local_id or "",
            content=str(content or ""),
            media_items=media_items,
            parts=_normalize_parts(parts),
            ts=ts if ts is not None else time.time(),
        )
        with self._sync_lock:
            history = self._messages.setdefault(chat_id, [])
            history.append(msg)
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
                    role=message.role,
                    name=message.name,
                    ts=message.ts,
                    user_id=message.user_id,
                    message_id=normalized_id,
                    local_id=message.local_id,
                    parts=message.parts,
                )
                return True
        return False

    def get(self, chat_id: str) -> list[StoredMessage]:
        with self._sync_lock:
            cached = self._messages.get(chat_id)
        if cached is None:
            loaded = self._load(chat_id)
            with self._sync_lock:
                current = self._messages.get(chat_id, [])
                self._messages[chat_id] = self._merge_history(loaded or [], current)
                cached = self._messages.get(chat_id) or []
        return list(cached)

    async def get_async(self, chat_id: str) -> list[StoredMessage]:
        async with self._lock:
            with self._sync_lock:
                cached = self._messages.get(chat_id)
            if cached is None:
                loaded = await asyncio.to_thread(self._load, chat_id)
                with self._sync_lock:
                    current = self._messages.get(chat_id, [])
                    self._messages[chat_id] = self._merge_history(loaded or [], current)
                    cached = self._messages.get(chat_id) or []
        return list(cached)

    def get_recent(self, chat_id: str, *, max_items: int) -> list[StoredMessage]:
        history = self.get(chat_id)
        if max_items <= 0:
            return []
        return history[-max_items:]

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
                data_dir = self._data_dir
                history = self._messages.get(chat_id)
                if not data_dir or history is None:
                    return
                snapshot = list(history[-200:])
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / f"{chat_id}.json"
            payload = [_serialize_message(message) for message in snapshot]
            write_json(path, payload)

    def _load(self, chat_id: str) -> Optional[list[StoredMessage]]:
        with self._sync_lock:
            data_dir = self._data_dir
        if not data_dir:
            return None
        path = data_dir / f"{chat_id}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return None
            out: list[StoredMessage] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", ""))
                name = str(item.get("name", ""))
                content = str(item.get("content", ""))
                user_id_raw = item.get("user_id", None)
                user_id: Optional[int] = None
                if user_id_raw is not None:
                    try:
                        user_id = int(user_id_raw)
                    except (TypeError, ValueError):
                        user_id = None
                message_id_raw = item.get("message_id", None)
                message_id: Optional[int] = None
                if message_id_raw is not None:
                    try:
                        message_id = int(message_id_raw)
                    except (TypeError, ValueError):
                        message_id = None
                local_id = str(item.get("local_id", "") or "")
                media_items = _normalize_media_items(item.get("media_items", []))
                parts = _normalize_parts(item.get("parts", []))
                ts_val = item.get("ts", time.time())
                try:
                    ts = float(ts_val)
                except (TypeError, ValueError):
                    ts = time.time()
                if role and (content or media_items or parts):
                    out.append(
                        StoredMessage(
                            role=role,
                            name=name,
                            user_id=user_id,
                            message_id=message_id,
                            local_id=local_id,
                            parts=parts,
                            content=content,
                            media_items=media_items,
                            ts=ts,
                        )
                    )
            return out
        except Exception:
            return None
