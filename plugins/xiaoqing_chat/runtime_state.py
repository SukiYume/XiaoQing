from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config.config import XiaoQingChatConfig
from .expression.bw_expression_store import ExpressionStore
from .expression.bw_jargon_store import JargonStore
from .expression.bw_message_recorder import MessageRecorder
from .expression.bw_reflect_tracker import ReflectTrackerStore
from .generation_limiter import GenerationLimiter
from .media_registry import MediaRegistryStore
from .memory.memory import MemoryStore
from .memory.memory_db import MemoryDB
from .memory.review_sessions import ReviewStore
from .planning.action_history import ActionHistoryStore
from .planning.goal_state import GoalStore
from .planning.heartflow import HeartflowEngine
from .planning.pfc_state import PFCStateStore
from .planning.plan_reply_logger import PlanReplyLogger


@dataclass
class _ChatRuntime:
    cfg: XiaoQingChatConfig
    compiled_ban_regex: list[re.Pattern[str]]


@dataclass
class _PerChatState:
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    reply_timestamps: dict[str, list[float]] = field(default_factory=dict)
    last_reply_ts: dict[str, float] = field(default_factory=dict)
    last_observe_ts: dict[str, float] = field(default_factory=dict)
    continuous_reply_count: dict[str, int] = field(default_factory=dict)
    continuous_cooldown_until: dict[str, float] = field(default_factory=dict)
    stats: dict[str, dict[str, int]] = field(default_factory=dict)
    persist_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    next_local_id: dict[str, int] = field(default_factory=dict)
    # chat_id -> (mood_text, expires_at_timestamp)
    mood_state: dict[str, tuple[str, float]] = field(default_factory=dict)
    # chat_id -> (caller_user_id, expires_at_timestamp)
    pending_bot_name_call: dict[str, tuple[int | None, float]] = field(default_factory=dict)
    reply_gate_decision: dict[str, Any] = field(default_factory=dict)


class ChatRuntimeState:
    __slots__ = (
        "_memory_store",
        "_memory_db",
        "_media_store",
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
        "_global_active_provider",
        "_active_provider_by_chat",
        "_generation_limiter",
    )

    def __init__(self) -> None:
        self._memory_store = MemoryStore()
        self._memory_db = MemoryDB()
        self._media_store = MediaRegistryStore()
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
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._vdb_save_task: asyncio.Task[Any] | None = None
        self._global_active_provider: str | None = None
        self._active_provider_by_chat: dict[str, str] = {}
        self._generation_limiter = GenerationLimiter()

    @property
    def memory_store(self) -> MemoryStore:
        return self._memory_store

    @property
    def memory_db(self) -> MemoryDB:
        return self._memory_db

    @property
    def media_store(self) -> MediaRegistryStore:
        return self._media_store

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

    @property
    def generation_limiter(self) -> GenerationLimiter:
        return self._generation_limiter

    def get_runtime(self, config_key: str) -> _ChatRuntime | None:
        return self._runtime_cache.get(config_key)

    def set_runtime(self, config_key: str, runtime: _ChatRuntime, mtime: int) -> None:
        self._runtime_cache[config_key] = runtime
        self._runtime_mtime[config_key] = mtime

    def get_runtime_mtime(self, config_key: str) -> int | None:
        return self._runtime_mtime.get(config_key)

    _MAX_TRACKED_CHATS = 500

    def get_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._per_chat.locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._per_chat.locks[chat_id] = lock
        # Periodic cleanup: only run every 100 new chats
        if len(self._per_chat.locks) % 100 == 0:
            self.cleanup_stale_chats()
        return lock

    def cleanup_stale_chats(self) -> None:
        """Evict per-chat entries beyond the limit, keeping those with recent activity."""
        pc = self._per_chat
        all_ids: set[str] = set()
        for d in (
            pc.locks,
            pc.reply_timestamps,
            pc.last_reply_ts,
            pc.last_observe_ts,
            pc.continuous_reply_count,
            pc.continuous_cooldown_until,
            pc.stats,
            pc.persist_tasks,
            pc.next_local_id,
            pc.mood_state,
            pc.pending_bot_name_call,
            pc.reply_gate_decision,
        ):
            all_ids.update(d.keys())
        if len(all_ids) <= self._MAX_TRACKED_CHATS:
            return
        scored = []
        for cid in all_ids:
            ts = max(pc.last_reply_ts.get(cid, 0.0), pc.last_observe_ts.get(cid, 0.0))
            scored.append((ts, cid))
        scored.sort(reverse=True)
        keep = {cid for _, cid in scored[: self._MAX_TRACKED_CHATS]}
        for cid, lock in pc.locks.items():
            if lock.locked():
                keep.add(cid)
        for cid, task in pc.persist_tasks.items():
            if not task.done():
                keep.add(cid)

        for d in (
            pc.reply_timestamps,
            pc.last_reply_ts,
            pc.last_observe_ts,
            pc.continuous_reply_count,
            pc.continuous_cooldown_until,
            pc.stats,
            pc.next_local_id,
            pc.mood_state,
            pc.pending_bot_name_call,
            pc.reply_gate_decision,
        ):
            for cid in list(d.keys()):
                if cid not in keep:
                    del d[cid]

        for cid in list(pc.persist_tasks.keys()):
            if cid in keep:
                continue
            task = pc.persist_tasks.pop(cid)
            if not task.done():
                task.cancel()

        for cid in list(pc.locks.keys()):
            if cid not in keep and not pc.locks[cid].locked():
                del pc.locks[cid]

    def get_reply_timestamps(self, chat_id: str) -> list[float]:
        return self._per_chat.reply_timestamps.get(chat_id, [])

    def set_reply_timestamps(self, chat_id: str, timestamps: list[float]) -> None:
        self._per_chat.reply_timestamps[chat_id] = timestamps

    def get_last_reply_ts(self, chat_id: str) -> float:
        return self._per_chat.last_reply_ts.get(chat_id, 0.0)

    def set_last_reply_ts(self, chat_id: str, ts: float) -> None:
        self._per_chat.last_reply_ts[chat_id] = ts

    def get_last_observe_ts(self, chat_id: str) -> float:
        return self._per_chat.last_observe_ts.get(chat_id, 0.0)

    def set_last_observe_ts(self, chat_id: str, ts: float) -> None:
        self._per_chat.last_observe_ts[chat_id] = ts

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

    def get_persist_task(self, chat_id: str) -> asyncio.Task[Any] | None:
        return self._per_chat.persist_tasks.get(chat_id)

    def set_persist_task(self, chat_id: str, task: asyncio.Task[Any]) -> None:
        self._per_chat.persist_tasks[chat_id] = task

    def pop_persist_task(self, chat_id: str) -> asyncio.Task[Any] | None:
        return self._per_chat.persist_tasks.pop(chat_id, None)

    def add_bg_task(self, task: asyncio.Task[Any]) -> None:
        self._bg_tasks.add(task)

    def remove_bg_task(self, task: asyncio.Task[Any]) -> None:
        self._bg_tasks.discard(task)

    def get_vdb_save_task(self) -> asyncio.Task[Any] | None:
        return self._vdb_save_task

    def set_vdb_save_task(self, task: asyncio.Task[Any] | None) -> None:
        self._vdb_save_task = task

    def get_next_local_id(self, chat_id: str) -> int:
        return self._per_chat.next_local_id.get(chat_id, 1)

    def set_next_local_id(self, chat_id: str, next_id: int) -> None:
        self._per_chat.next_local_id[chat_id] = next_id

    def fetch_and_increment_local_id(self, chat_id: str) -> int:
        """Atomically get current local_id and increment. Returns the old value."""
        n = self._per_chat.next_local_id.get(chat_id, 1)
        self._per_chat.next_local_id[chat_id] = n + 1
        return n

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

    def set_mood_state(
        self, chat_id: str, mood_text: str, duration_seconds: float = 1800.0
    ) -> None:
        """Persist a mood state for this chat for the given duration."""
        self._per_chat.mood_state[chat_id] = (mood_text, time.time() + duration_seconds)

    def set_pending_bot_name_call(
        self, chat_id: str, user_id: int | None, *, ttl_seconds: float = 60.0
    ) -> None:
        expires_at = time.time() + max(0.0, float(ttl_seconds))
        self._per_chat.pending_bot_name_call[chat_id] = (user_id, expires_at)

    def consume_pending_bot_name_call(
        self, chat_id: str, user_id: int | None, *, now: float | None = None
    ) -> bool:
        entry = self._per_chat.pending_bot_name_call.get(chat_id)
        if not entry:
            return False
        caller_user_id, expires_at = entry
        current_ts = time.time() if now is None else float(now)
        if current_ts > expires_at:
            del self._per_chat.pending_bot_name_call[chat_id]
            return False
        if caller_user_id is not None and user_id != caller_user_id:
            return False
        del self._per_chat.pending_bot_name_call[chat_id]
        return True

    def set_reply_gate_decision(self, chat_id: str, decision: Any) -> None:
        self._per_chat.reply_gate_decision[chat_id] = decision

    def get_reply_gate_decision(self, chat_id: str) -> Any:
        return self._per_chat.reply_gate_decision.get(chat_id)

    @property
    def active_provider(self) -> str | None:
        """Backward-compatible alias for the global in-memory override."""

        return self._global_active_provider

    @active_provider.setter
    def active_provider(self, name: str | None) -> None:
        self._global_active_provider = name

    @property
    def global_active_provider(self) -> str | None:
        return self._global_active_provider

    def set_global_provider(self, name: str | None) -> None:
        self._global_active_provider = name

    def get_chat_provider(self, chat_id: str) -> str | None:
        return self._active_provider_by_chat.get(str(chat_id))

    def set_chat_provider(self, chat_id: str, name: str | None) -> None:
        normalized_chat_id = str(chat_id)
        if name is None:
            self._active_provider_by_chat.pop(normalized_chat_id, None)
            return
        self._active_provider_by_chat[normalized_chat_id] = str(name)

    def provider_overrides(self) -> dict[str, str]:
        return dict(self._active_provider_by_chat)

    def resolve_provider_name(
        self,
        chat_id: str | None,
        provider_names: list[str] | tuple[str, ...],
        default_name: str,
    ) -> str:
        """Resolve chat -> global -> configured default and prune stale overrides."""

        ordered_names = tuple(dict.fromkeys(str(name) for name in provider_names if name))
        valid_names = set(ordered_names)
        if self._global_active_provider not in valid_names:
            self._global_active_provider = None
        for scoped_chat_id, provider_name in tuple(self._active_provider_by_chat.items()):
            if provider_name not in valid_names:
                self._active_provider_by_chat.pop(scoped_chat_id, None)

        if chat_id is not None:
            scoped = self._active_provider_by_chat.get(str(chat_id))
            if scoped in valid_names:
                return scoped
        if self._global_active_provider in valid_names:
            return self._global_active_provider
        if default_name in valid_names:
            return default_name
        return ordered_names[0] if ordered_names else ""


_global_state: ChatRuntimeState | None = None


def get_state() -> ChatRuntimeState:
    global _global_state
    if _global_state is None:
        _global_state = ChatRuntimeState()
    return _global_state


def reset_global_state() -> None:
    global _global_state
    _global_state = None
