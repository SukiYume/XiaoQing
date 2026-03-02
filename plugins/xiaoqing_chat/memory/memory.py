from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

@dataclass(frozen=True)
class StoredMessage:
    role: str
    name: str
    content: str
    ts: float
    user_id: Optional[int] = None
    message_id: Optional[int] = None
    local_id: str = ""

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
        import threading
        self._sync_lock = threading.Lock()

    def bind_data_dir(self, data_dir: Path) -> None:
        with self._sync_lock:
            self._data_dir = data_dir

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

    def append(
        self,
        chat_id: str,
        *,
        role: str,
        name: str,
        user_id: Optional[int] = None,
        message_id: Optional[int] = None,
        local_id: str = "",
        content: str,
        ts: Optional[float] = None,
    ) -> None:
        msg = StoredMessage(
            role=role,
            name=name,
            user_id=user_id,
            message_id=message_id,
            local_id=local_id or "",
            content=content,
            ts=ts if ts is not None else time.time(),
        )
        with self._sync_lock:
            self._messages.setdefault(chat_id, []).append(msg)

    def get(self, chat_id: str) -> list[StoredMessage]:
        with self._sync_lock:
            cached = self._messages.get(chat_id)
        if cached is None:
            loaded = self._load(chat_id)
            with self._sync_lock:
                self._messages[chat_id] = loaded if loaded is not None else []
                cached = self._messages.get(chat_id) or []
        return list(cached)

    def get_recent(self, chat_id: str, *, max_items: int) -> list[StoredMessage]:
        history = self.get(chat_id)
        if max_items <= 0:
            return []
        return history[-max_items:]

    def persist(self, chat_id: str) -> None:
        """同步持久化方法，可在 asyncio.to_thread() 中安全调用。

        仅短暂持有 _sync_lock 获取快照，然后在持锁外做 I/O。
        """
        with self._sync_lock:
            data_dir = self._data_dir
            history = self._messages.get(chat_id)
            if not data_dir or history is None:
                return
            snapshot = list(history[-200:])
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / f"{chat_id}.json"
        payload = [asdict(m) for m in snapshot]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
                ts_val = item.get("ts", time.time())
                try:
                    ts = float(ts_val)
                except (TypeError, ValueError):
                    ts = time.time()
                if role and content:
                    out.append(
                        StoredMessage(
                            role=role,
                            name=name,
                            user_id=user_id,
                            message_id=message_id,
                            local_id=local_id,
                            content=content,
                            ts=ts,
                        )
                    )
            return out
        except Exception:
            return None
