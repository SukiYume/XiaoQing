from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config.config import XiaoQingChatConfig
from .memory.memory_db import MemoryDB
from .memory.memory import MemoryStore
from .planning.action_history import ActionHistoryStore, ActionRecord
from .planning.plan_reply_logger import PlanReplyLogger
from .planning.heartflow import HeartflowEngine
from .planning.goal_state import GoalStore
from .memory.review_sessions import ReviewStore
from .planning.pfc_state import PFCStateStore
from .expression.bw_expression_store import ExpressionStore
from .expression.bw_reflect_tracker import ReflectTrackerStore
from .expression.bw_message_recorder import MessageRecorder
from .expression.bw_jargon_store import JargonStore

@dataclass
class _ChatRuntime:
    cfg: XiaoQingChatConfig
    compiled_ban_regex: list[re.Pattern]

@dataclass
class _PerChatState:
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    reply_timestamps: dict[str, list[float]] = field(default_factory=dict)
    last_reply_ts: dict[str, float] = field(default_factory=dict)
    continuous_reply_count: dict[str, int] = field(default_factory=dict)
    continuous_cooldown_until: dict[str, float] = field(default_factory=dict)
    stats: dict[str, dict[str, int]] = field(default_factory=dict)
    persist_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    next_local_id: dict[str, int] = field(default_factory=dict)
    # chat_id -> (mood_text, expires_at_timestamp)
    mood_state: dict[str, tuple[str, float]] = field(default_factory=dict)

class ChatRuntimeState:
    __slots__ = (
        "_memory_store",
        "_memory_db",
        "_action_history",
        "_plan_reply_logger",
        "_heartflow",
        "_goal_store",
        "_review_store",
        "_pfc_state_store",
        "_bw_expr_store",
        "_bw_tracker_store",
        "_bw_recorder",
        "_bw_jargon_store",
        "_runtime_cache",
        "_runtime_mtime",
        "_per_chat",
        "_bg_tasks",
        "_vdb_save_task",
        "_active_provider",
    )

    def __init__(self) -> None:
        self._memory_store = MemoryStore()
        self._memory_db = MemoryDB()
        self._action_history = ActionHistoryStore()
        self._plan_reply_logger = PlanReplyLogger()
        self._heartflow = HeartflowEngine()
        self._goal_store = GoalStore()
        self._review_store = ReviewStore()
        self._pfc_state_store = PFCStateStore()
        self._bw_expr_store = ExpressionStore()
        self._bw_tracker_store = ReflectTrackerStore()
        self._bw_recorder = MessageRecorder()
        self._bw_jargon_store = JargonStore()

        self._runtime_cache: dict[str, _ChatRuntime] = {}
        self._runtime_mtime: dict[str, int] = {}

        self._per_chat = _PerChatState()
        self._bg_tasks: set[asyncio.Task] = set()
        self._vdb_save_task: Optional[asyncio.Task] = None
        self._active_provider: Optional[str] = None

    @property
    def memory_store(self) -> MemoryStore:
        return self._memory_store

    @property
    def memory_db(self) -> MemoryDB:
        return self._memory_db

    @property
    def action_history(self) -> ActionHistoryStore:
        return self._action_history

    @property
    def plan_reply_logger(self) -> PlanReplyLogger:
        return self._plan_reply_logger

    @property
    def heartflow(self) -> HeartflowEngine:
        return self._heartflow

    @property
    def goal_store(self) -> GoalStore:
        return self._goal_store

    @property
    def review_store(self) -> ReviewStore:
        return self._review_store

    @property
    def pfc_state_store(self) -> PFCStateStore:
        return self._pfc_state_store

    @property
    def bw_expr_store(self) -> ExpressionStore:
        return self._bw_expr_store

    @property
    def bw_tracker_store(self) -> ReflectTrackerStore:
        return self._bw_tracker_store

    @property
    def bw_recorder(self) -> MessageRecorder:
        return self._bw_recorder

    @property
    def bw_jargon_store(self) -> JargonStore:
        return self._bw_jargon_store

    def get_runtime(self, config_key: str) -> Optional[_ChatRuntime]:
        return self._runtime_cache.get(config_key)

    def set_runtime(self, config_key: str, runtime: _ChatRuntime, mtime: int) -> None:
        self._runtime_cache[config_key] = runtime
        self._runtime_mtime[config_key] = mtime

    def get_runtime_mtime(self, config_key: str) -> Optional[int]:
        return self._runtime_mtime.get(config_key)

    def get_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._per_chat.locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._per_chat.locks[chat_id] = lock
        return lock

    def get_reply_timestamps(self, chat_id: str) -> list[float]:
        return self._per_chat.reply_timestamps.get(chat_id, [])

    def set_reply_timestamps(self, chat_id: str, timestamps: list[float]) -> None:
        self._per_chat.reply_timestamps[chat_id] = timestamps

    def get_last_reply_ts(self, chat_id: str) -> float:
        return self._per_chat.last_reply_ts.get(chat_id, 0.0)

    def set_last_reply_ts(self, chat_id: str, ts: float) -> None:
        self._per_chat.last_reply_ts[chat_id] = ts

    def get_continuous_reply_count(self, chat_id: str) -> int:
        return self._per_chat.continuous_reply_count.get(chat_id, 0)

    def set_continuous_reply_count(self, chat_id: str, count: int) -> None:
        self._per_chat.continuous_reply_count[chat_id] = count

    def get_continuous_cooldown_until(self, chat_id: str) -> float:
        return self._per_chat.continuous_cooldown_until.get(chat_id, 0.0)

    def set_continuous_cooldown_until(self, chat_id: str, ts: float) -> None:
        self._per_chat.continuous_cooldown_until[chat_id] = ts

    def get_stats(self, chat_id: str) -> dict[str, int]:
        return self._per_chat.stats.get(chat_id, {"replies": 0, "calls": 0})

    def set_stats(self, chat_id: str, stats: dict[str, int]) -> None:
        self._per_chat.stats[chat_id] = stats

    def inc_stats(self, chat_id: str, key: str) -> None:
        d = self._per_chat.stats.setdefault(chat_id, {"replies": 0, "calls": 0})
        d[key] = int(d.get(key, 0)) + 1

    def get_persist_task(self, chat_id: str) -> Optional[asyncio.Task]:
        return self._per_chat.persist_tasks.get(chat_id)

    def set_persist_task(self, chat_id: str, task: asyncio.Task) -> None:
        self._per_chat.persist_tasks[chat_id] = task

    def add_bg_task(self, task: asyncio.Task) -> None:
        self._bg_tasks.add(task)

    def remove_bg_task(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)

    def get_vdb_save_task(self) -> Optional[asyncio.Task]:
        return self._vdb_save_task

    def set_vdb_save_task(self, task: Optional[asyncio.Task]) -> None:
        self._vdb_save_task = task

    def get_next_local_id(self, chat_id: str) -> int:
        return self._per_chat.next_local_id.get(chat_id, 1)

    def set_next_local_id(self, chat_id: str, next_id: int) -> None:
        self._per_chat.next_local_id[chat_id] = next_id

    def get_mood_state(self, chat_id: str) -> str:
        """Return current mood text if still active, else empty string."""
        entry = self._per_chat.mood_state.get(chat_id)
        if not entry:
            return ""
        mood_text, expires_at = entry
        if time.time() > expires_at:
            del self._per_chat.mood_state[chat_id]
            return ""
        return mood_text

    def set_mood_state(self, chat_id: str, mood_text: str, duration_seconds: float = 1800.0) -> None:
        """Persist a mood state for this chat for the given duration."""
        self._per_chat.mood_state[chat_id] = (mood_text, time.time() + duration_seconds)

    @property
    def active_provider(self) -> Optional[str]:
        return self._active_provider

    @active_provider.setter
    def active_provider(self, name: Optional[str]) -> None:
        self._active_provider = name

_global_state: Optional[ChatRuntimeState] = None

def get_global_state() -> ChatRuntimeState:
    global _global_state
    if _global_state is None:
        _global_state = ChatRuntimeState()
    return _global_state

def get_state() -> ChatRuntimeState:
    return get_global_state()

def reset_global_state() -> None:
    global _global_state
    _global_state = None
