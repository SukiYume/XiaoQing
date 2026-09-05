"""按会话持久化规划动作及其执行结果，供后续决策复盘。"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.plugin_base import load_json, write_json

from ..store_base import coerce_finite_float, coerce_json_bool, delete_json_artifacts


@dataclass(frozen=True)
class ActionRecord:
    ts: float
    local_target: str
    action: str
    reasoning: str
    detail: dict[str, Any]
    executed: bool


class ActionHistoryStore:
    """按会话保存动作记录，并采用防抖持久化。

    使用脏标记和防抖写入，而不是每次追加都落盘，避免高频对话中的 I/O 瓶颈。
    """

    def __init__(self) -> None:
        self._data_dir: Path | None                = None
        self._cache: dict[str, list[ActionRecord]] = {}
        self._dirty: set[str]                      = set()
        self._async_loading: set[str]              = set()
        self._state_version: dict[str, int]        = {}
        self._lock                                 = threading.RLock()
        self._write_lock                           = threading.Lock()

    def bind(self, data_dir: Path) -> None:
        with self._lock:
            self._data_dir = data_dir

    def append(self, chat_id: str, record: ActionRecord) -> None:
        with self._lock:
            if chat_id not in self._cache:
                if chat_id in self._async_loading:
                    self._cache[chat_id] = []
                else:
                    loaded               = self._load(chat_id)
                    self._cache[chat_id] = loaded or []
            self._cache[chat_id].append(record)
            self._dirty.add(chat_id)
            self._state_version[chat_id] = self._state_version.get(chat_id, 0) + 1

    def clear(self, chat_id: str) -> None:
        # 与正在执行的防抖刷盘串行，避免重置删除后旧快照又被后台线程写回。
        with self._write_lock:
            with self._lock:
                self._cache.pop(chat_id, None)
                self._dirty.discard(chat_id)
                self._state_version[chat_id] = self._state_version.get(chat_id, 0) + 1
                path                         = self._path(chat_id)
            if path:
                delete_json_artifacts(path)

    def flush(self, chat_id: str | None = None) -> None:
        """把脏会话持久化到磁盘，由防抖调度器调用。"""
        if chat_id is not None:
            self._persist(chat_id)
        else:
            with self._lock:
                dirty = list(self._dirty)
            for cid in dirty:
                self._persist(cid)

    async def get_recent_async(self, chat_id: str, *, max_items: int = 20) -> list[ActionRecord]:
        if chat_id not in self._cache:
            start_version = self._state_version.get(chat_id, 0)
            self._async_loading.add(chat_id)
            try:
                loaded = await asyncio.to_thread(self._load, chat_id)
            finally:
                self._async_loading.discard(chat_id)
            if chat_id not in self._cache and self._state_version.get(chat_id, 0) == start_version:
                self._cache[chat_id] = loaded or []
        if max_items <= 0:
            return []
        return list(self._cache.get(chat_id, [])[-max_items:])

    def _path(self, chat_id: str) -> Path | None:
        if not self._data_dir:
            return None
        return self._data_dir / "action_history" / f"{chat_id}.json"

    def _persist(self, chat_id: str) -> None:
        with self._write_lock:
            with self._lock:
                if chat_id not in self._dirty:
                    return
                path = self._path(chat_id)
                if not path:
                    return
                version = self._state_version.get(chat_id, 0)
                payload = [asdict(x) for x in self._cache.get(chat_id, [])[-200:]]
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, payload)
            with self._lock:
                if self._state_version.get(chat_id, 0) == version:
                    self._dirty.discard(chat_id)

    def _load(self, chat_id: str) -> list[ActionRecord] | None:
        path = self._path(chat_id)
        if not path or not path.exists():
            return None
        try:
            raw = load_json(path, default=None)
        except OSError:
            return None
        if not isinstance(raw, list):
            return None

        out: list[ActionRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            detail_raw             = item.get("detail")
            detail: dict[str, Any] = detail_raw if isinstance(detail_raw, dict) else {}
            out.append(
                ActionRecord(
                    ts=coerce_finite_float(item.get("ts"), default=time.time(), minimum=0.0),
                    local_target = str(item.get("local_target", "") or ""),
                    action       = str(item.get("action", "") or ""),
                    reasoning    = str(item.get("reasoning", "") or ""),
                    detail       = detail,
                    executed=coerce_json_bool(item.get("executed"), default=False),
                )
            )
        return out
