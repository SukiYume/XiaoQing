"""Transactional, per-user plugin session storage.

Stored sessions are never exposed directly. Read APIs return detached snapshots
and :meth:`SessionManager.update` is the only read-modify-write boundary. Each
update works on a bounded clone of a safe built-in value tree and publishes a
second clone only after the callback has completed successfully.

Depth/node limits and explicit cycle checks keep plugin-controlled state from
turning snapshot creation into unbounded recursion or memory growth.  Custom
``__deepcopy__`` hooks are never executed.  Cancellation is funneled through a
single scheduled transaction Future: the caller may be cancelled, but commit
or rollback is decided only after that Future has been cancelled and drained,
so no background callback can publish state after its request has returned.

会话值始终是管理器私有对象，读接口只返回有界深拷贝；同一用户/群组键的更新串行化，
回调只能修改事务工作副本。提交必须在回调（包括取消清理）完全结束后一次性替换原值，
因此异常、取消和非法数据都不能留下半提交状态。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, overload

from .async_keyed_lock import AsyncKeyedLockPool

logger = logging.getLogger(__name__)
T = TypeVar("T")
SessionKey = tuple[int, int | None]
_MAX_SESSION_DATA_DEPTH = 64
_MAX_SESSION_DATA_NODES = 100_000


def _clone_session_value(
    value: Any,
    *,
    memo: dict[int, Any],
    active: set[int],
    depth: int,
    nodes: list[int],
) -> Any:
    """Clone the supported session value tree without invoking user hooks.

    ``copy.deepcopy`` delegates to arbitrary ``__deepcopy__`` implementations;
    such a hook may legally return the original mutable object and silently
    defeat both snapshot isolation and transaction rollback.  Session state is
    intentionally a bounded, JSON-like value tree, so clone its concrete built-
    in containers directly and reject custom objects instead of executing their
    copy protocol.
    """

    nodes[0] += 1
    if nodes[0] > _MAX_SESSION_DATA_NODES:
        raise ValueError("session data exceeds the node limit")
    if depth > _MAX_SESSION_DATA_DEPTH:
        raise ValueError("session data exceeds the nesting limit")

    value_type = type(value)
    if value is None or value_type in {bool, int, float, str, bytes}:
        return value
    if value_type not in {dict, list, tuple}:
        raise TypeError(
            "session data values must use built-in dict, list, tuple, "
            "str, bytes, int, float, bool, or None"
        )

    identity = id(value)
    if identity in active:
        raise ValueError("session data must not contain reference cycles")
    if identity in memo:
        return memo[identity]

    active.add(identity)
    try:
        if value_type is dict:
            cloned_dict: dict[str, Any] = {}
            memo[identity] = cloned_dict
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("session data mapping keys must be strings")
                cloned_dict[key] = _clone_session_value(
                    item,
                    memo=memo,
                    active=active,
                    depth=depth + 1,
                    nodes=nodes,
                )
            return cloned_dict
        if value_type is list:
            cloned_list: list[Any] = []
            memo[identity] = cloned_list
            cloned_list.extend(
                _clone_session_value(
                    item,
                    memo=memo,
                    active=active,
                    depth=depth + 1,
                    nodes=nodes,
                )
                for item in value
            )
            return cloned_list

        cloned_tuple = tuple(
            _clone_session_value(
                item,
                memo=memo,
                active=active,
                depth=depth + 1,
                nodes=nodes,
            )
            for item in value
        )
        memo[identity] = cloned_tuple
        return cloned_tuple
    finally:
        active.remove(identity)


def _clone_session_data(data: dict[str, Any]) -> dict[str, Any]:
    cloned = _clone_session_value(data, memo={}, active=set(), depth=0, nodes=[0])
    if not isinstance(cloned, dict):  # pragma: no cover - guarded by the caller/type tree
        raise TypeError("session data must be a dict")
    return cloned


@overload
def _normalize_id(value: Any, *, field_name: str, allow_none: Literal[False] = False) -> int: ...


@overload
def _normalize_id(value: Any, *, field_name: str, allow_none: Literal[True]) -> int | None: ...


def _normalize_id(value: Any, *, field_name: str, allow_none: bool = False) -> int | None:
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{field_name} must be a positive integer")
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a positive integer")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or not stripped.isdecimal():
            raise TypeError(f"{field_name} must be a positive integer")
        normalized = int(stripped)
    elif isinstance(value, int):
        normalized = value
    else:
        raise TypeError(f"{field_name} must be a positive integer")
    if normalized <= 0:
        raise ValueError(f"{field_name} must be positive")
    return normalized


def _normalize_plugin_name(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("plugin_name must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("plugin_name must not be empty")
    return normalized


def _normalize_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("session timeout must be a positive finite number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("session timeout must be a positive finite number") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("session timeout must be a positive finite number")
    return normalized


def _normalize_timestamp(value: Any, *, field_name: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(f"{field_name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be a finite number")
    return normalized


def _normalize_session_id(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("session_id must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("session_id must not be empty")
    return normalized


def _new_session_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Session:
    """A mutable session value or detached session snapshot."""

    user_id: int
    group_id: int | None
    plugin_name: str
    session_id: str = field(default_factory=_new_session_id)
    state: str = "active"
    data: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    timeout: float = 300.0
    version: int = 0

    def __post_init__(self) -> None:
        self.user_id = int(_normalize_id(self.user_id, field_name="user_id"))
        self.group_id = _normalize_id(
            self.group_id,
            field_name="group_id",
            allow_none=True,
        )
        self.plugin_name = _normalize_plugin_name(self.plugin_name)
        self.session_id = _normalize_session_id(self.session_id)
        if type(self.state) is not str:
            raise TypeError("session state must be a string")
        self.created_at = _normalize_timestamp(self.created_at, field_name="created_at")
        self.updated_at = _normalize_timestamp(self.updated_at, field_name="updated_at")
        self.timeout = _normalize_timeout(self.timeout)
        if not isinstance(self.data, dict):
            raise TypeError("session data must be a dict")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("session version must be an integer")
        if self.version < 0:
            raise ValueError("session version must not be negative")

    def update(self) -> None:
        """Touch this mutable value and increment its local version."""

        self.updated_at = time.time()
        self.version += 1

    def is_expired(self) -> bool:
        """Return whether this session has exceeded its idle timeout."""

        return time.time() - self.updated_at > self.timeout

    def get(self, key: str, default: Any = None) -> Any:
        """Read one value from session data."""

        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set one value on this mutable session value."""

        self.data[key] = value
        self.update()

    def delete(self, key: str) -> bool:
        """Delete one value and return whether it existed."""

        if key not in self.data:
            return False
        del self.data[key]
        self.update()
        return True

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def clear(self) -> None:
        self.data.clear()
        self.update()


@dataclass
class _SessionTransaction:
    """One task-owned staged view for a single session key."""

    original: Session
    working: Session | None
    replaced: bool = False


class SessionManager:
    """Store sessions with per-key serializable update transactions.

    A transaction registry is keyed by the exact ``(asyncio.Task, key)`` pair.
    This lets the callback re-enter read/create/delete APIs without deadlocking,
    while child tasks do not inherit the staged view and wait for the key lock.
    A callback may only re-enter operations for its own key: acquiring another
    session key while the first remains locked can create an ABBA deadlock, so
    cross-key access fails fast and must be coordinated outside ``update``.
    """

    def __init__(self, default_timeout: float = 300.0) -> None:
        self._sessions: dict[SessionKey, Session] = {}
        self._lock = asyncio.Lock()
        self._key_lock_pool = AsyncKeyedLockPool(max_keys=4096, max_key_length=128)
        self._default_timeout = _normalize_timeout(default_timeout)
        # 事务视图按“实际执行回调的 Task + 会话键”隔离；子任务不会继承父任务的
        # 工作副本，从而不能绕过键锁观察或修改尚未提交的数据。
        self._transactions: dict[
            tuple[asyncio.Task[Any], SessionKey],
            _SessionTransaction,
        ] = {}

    @property
    def default_timeout(self) -> float:
        return self._default_timeout

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def active_key_lock_count(self) -> int:
        return int(self._key_lock_pool.active_key_count)

    def set_default_timeout(self, timeout: float) -> None:
        self._default_timeout = _normalize_timeout(timeout)

    def _make_key(self, user_id: Any, group_id: Any) -> SessionKey:
        normalized_user = _normalize_id(user_id, field_name="user_id")
        normalized_group = _normalize_id(
            group_id,
            field_name="group_id",
            allow_none=True,
        )
        return (int(normalized_user), normalized_group)

    def _current_transaction(self, key: SessionKey) -> _SessionTransaction | None:
        task = asyncio.current_task()
        if task is None:
            return None
        return self._transactions.get((task, key))

    def _transaction_key_for_current_task(self) -> SessionKey | None:
        """Return the key owned by the current callback task, if any."""

        task = asyncio.current_task()
        if task is None:
            return None
        for owner, key in self._transactions:
            if owner is task:
                return key
        return None

    def _reject_cross_key_transaction(self, key: SessionKey) -> None:
        """Prevent a callback from waiting on a second per-key session lock."""

        owned_key = self._transaction_key_for_current_task()
        if owned_key is not None and owned_key != key:
            raise RuntimeError(
                "session transaction callback cannot access a different session key; "
                "coordinate cross-key work outside SessionManager.update"
            )

    def _reject_bulk_transaction_operation(self, operation: str) -> None:
        """Reject bulk lock acquisition from inside a per-key transaction."""

        if self._transaction_key_for_current_task() is not None:
            raise RuntimeError(
                f"session transaction callback cannot call {operation}; "
                "coordinate bulk session work outside SessionManager.update"
            )

    def _register_transaction(
        self,
        task: asyncio.Task[Any],
        key: SessionKey,
        transaction: _SessionTransaction,
    ) -> None:
        registry_key = (task, key)
        if registry_key in self._transactions:
            raise RuntimeError("nested session update for the same key is not allowed")
        self._transactions[registry_key] = transaction

    def _unregister_transaction(
        self,
        task: asyncio.Task[Any],
        key: SessionKey,
        transaction: _SessionTransaction,
    ) -> None:
        registry_key = (task, key)
        if self._transactions.get(registry_key) is transaction:
            self._transactions.pop(registry_key, None)

    @staticmethod
    def _clone(session: Session) -> Session:
        return Session(
            user_id=session.user_id,
            group_id=session.group_id,
            plugin_name=session.plugin_name,
            session_id=session.session_id,
            state=session.state,
            data=_clone_session_data(session.data),
            created_at=session.created_at,
            updated_at=session.updated_at,
            timeout=session.timeout,
            version=session.version,
        )

    @staticmethod
    def _clone_optional(session: Session | None) -> Session | None:
        return None if session is None else SessionManager._clone(session)

    @asynccontextmanager
    async def _lock_key(self, key: SessionKey):
        async with self._key_lock_pool.hold(key):
            yield

    async def _get_active_locked(self, key: SessionKey) -> Session | None:
        """Return the private stored value while the per-key lock is held."""

        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if session.is_expired():
                del self._sessions[key]
                logger.debug("Session expired and removed: user=%s, group=%s", key[0], key[1])
                return None
            return session

    @staticmethod
    def _initial_data_copy(initial_data: dict[str, Any] | None) -> dict[str, Any]:
        if initial_data is None:
            return {}
        if not isinstance(initial_data, dict):
            raise TypeError("initial_data must be a dict or None")
        return _clone_session_data(initial_data)

    def _new_session(
        self,
        key: SessionKey,
        plugin_name: str,
        initial_data: dict[str, Any] | None,
        timeout: float | None,
    ) -> Session:
        now = time.time()
        return Session(
            user_id=key[0],
            group_id=key[1],
            plugin_name=_normalize_plugin_name(plugin_name),
            data=self._initial_data_copy(initial_data),
            created_at=now,
            updated_at=now,
            timeout=self._default_timeout if timeout is None else _normalize_timeout(timeout),
        )

    async def create(
        self,
        user_id: int,
        group_id: int | None,
        plugin_name: str,
        initial_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Session:
        """Create or replace a session and return a detached snapshot."""

        key = self._make_key(user_id, group_id)
        self._reject_cross_key_transaction(key)
        session = self._new_session(key, plugin_name, initial_data, timeout)
        returned = self._clone(session)

        transaction = self._current_transaction(key)
        if transaction is not None:
            transaction.working = session
            transaction.replaced = True
            return returned

        async with self._lock_key(key):
            async with self._lock:
                self._sessions[key] = session
            logger.debug(
                "Session created: user=%s, group=%s, plugin=%s, session_id=%s",
                key[0],
                key[1],
                session.plugin_name,
                session.session_id,
            )
            return returned

    async def get(self, user_id: int, group_id: int | None) -> Session | None:
        """Return a detached snapshot and refresh the stored idle timestamp."""

        key = self._make_key(user_id, group_id)
        self._reject_cross_key_transaction(key)
        transaction = self._current_transaction(key)
        if transaction is not None:
            return self._clone_optional(transaction.working)

        async with self._lock_key(key):
            session = await self._get_active_locked(key)
            if session is None:
                return None
            # Reads extend the idle lease but do not create a new data version.
            session.updated_at = time.time()
            return self._clone(session)

    async def peek(self, user_id: int, group_id: int | None) -> Session | None:
        """Return a detached snapshot without refreshing its idle lease."""

        key = self._make_key(user_id, group_id)
        self._reject_cross_key_transaction(key)
        transaction = self._current_transaction(key)
        if transaction is not None:
            return self._clone_optional(transaction.working)

        async with self._lock_key(key):
            return self._clone_optional(await self._get_active_locked(key))

    async def _await_transaction_future(self, future: asyncio.Future[T]) -> T:
        """Await a callback without allowing it to swallow caller cancellation.

        ``Task.cancelling()`` and ``Task.uncancel()`` do not exist on Python
        3.10.  Shielding gives this manager an explicit cancellation boundary:
        a cancellation of the update task is recorded here, forwarded to the
        callback, and re-raised after callback cleanup even if the callback
        catches ``CancelledError`` and returns normally.
        """

        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                # shield 只阻止取消直接越过管理器；一旦收到取消，仍会显式转发给
                # 回调 Future，并等它进入终态后再把原始 CancelledError 还给调用者。
                result = await asyncio.shield(future)
                if cancellation is None:
                    return result
                break
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                if not future.done():
                    future.cancel()
                    continue
                break
            except BaseException:
                if cancellation is None:
                    raise
                break

        if not future.done():
            raise RuntimeError("session callback cancellation did not reach a terminal state")
        try:
            future.result()
        except BaseException:
            pass
        if cancellation is None:
            raise RuntimeError("session callback ended without a result")
        raise cancellation

    @staticmethod
    async def _run_callback(
        callback: Callable[[Session], T | Awaitable[T]],
        working: Session,
    ) -> T:
        """Invoke every callback inside one manager-owned transaction task.

        Synchronous callbacks need the same cancellation boundary as async
        callbacks.  A callback can cancel its own current task and return
        without another await; the explicit checkpoint delivers that pending
        cancellation before the manager may publish the working copy.  This
        remains compatible with Python 3.10 and does not depend on
        ``Task.cancelling`` or ``Task.uncancel``.
        """

        result = callback(working)
        if inspect.isawaitable(result):
            if isinstance(result, asyncio.Future):
                cancellation = await SessionManager._cancel_and_drain_scheduled(result)
                if cancellation is not None:
                    raise cancellation
                raise TypeError("session callback must not return a scheduled Task or Future")
            result = await result
        if isinstance(result, asyncio.Future):
            cancellation = await SessionManager._cancel_and_drain_scheduled(result)
            if cancellation is not None:
                raise cancellation
            raise TypeError("session callback must not return a scheduled Task or Future")
        await asyncio.sleep(0)
        return result

    @staticmethod
    async def _cancel_and_drain_scheduled(
        future: asyncio.Future[Any],
    ) -> asyncio.CancelledError | None:
        """Reclaim an invalid scheduled callback result without orphaning it."""

        cancellation: asyncio.CancelledError | None = None
        if not future.done():
            future.cancel()
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError as exc:
                # A CancelledError from the now-terminal child is expected.
                # If it is still live, this task itself was cancelled; remember
                # that request, forward it, and continue draining.
                if future.done():
                    break
                if cancellation is None:
                    cancellation = exc
                future.cancel()
            except BaseException:
                break
        try:
            future.result()
        except BaseException:
            pass
        return cancellation

    def _prepare_commit(self, key: SessionKey, transaction: _SessionTransaction) -> Session | None:
        if transaction.working is None:
            return None

        candidate = self._clone(transaction.working)
        candidate.user_id = key[0]
        candidate.group_id = key[1]
        candidate.timeout = _normalize_timeout(candidate.timeout)
        if not isinstance(candidate.data, dict):
            raise TypeError("session data must be a dict")

        candidate.updated_at = time.time()
        candidate.version = transaction.original.version + 1
        if transaction.replaced:
            candidate.plugin_name = _normalize_plugin_name(candidate.plugin_name)
            candidate.session_id = _normalize_session_id(candidate.session_id)
        else:
            candidate.plugin_name = transaction.original.plugin_name
            candidate.created_at = transaction.original.created_at
            candidate.session_id = transaction.original.session_id
        return candidate

    async def update(
        self,
        user_id: int,
        group_id: int | None,
        callback: Callable[[Session], T | Awaitable[T]],
    ) -> T | None:
        """Run one rollback-safe, serializable read-modify-write transaction.

        The callback receives a private working copy. Exceptions, any
        ``BaseException``, value-tree validation failures, and cancellation publish neither
        data nor metadata.  A successful transaction commits one new stored
        object and increments ``version`` exactly once.
        """

        key = self._make_key(user_id, group_id)
        self._reject_cross_key_transaction(key)
        if self._current_transaction(key) is not None:
            raise RuntimeError("nested session update for the same key is not allowed")

        async with self._lock_key(key):
            original = await self._get_active_locked(key)
            if original is None:
                return None
            working = self._clone(original)
            transaction = _SessionTransaction(original=original, working=working)

            callback_owner = asyncio.create_task(self._run_callback(callback, working))
            self._register_transaction(callback_owner, key, transaction)
            try:
                result = await self._await_transaction_future(callback_owner)

                committed = self._prepare_commit(key, transaction)
                async with self._lock:
                    if self._sessions.get(key) is not original:
                        raise RuntimeError("stored session changed during a locked transaction")
                    if committed is None:
                        self._sessions.pop(key, None)
                    else:
                        self._sessions[key] = committed
                return result
            finally:
                self._unregister_transaction(callback_owner, key, transaction)

    async def delete(self, user_id: int, group_id: int | None) -> bool:
        """Delete a session, staging the delete when called by its transaction."""

        key = self._make_key(user_id, group_id)
        self._reject_cross_key_transaction(key)
        transaction = self._current_transaction(key)
        if transaction is not None:
            if transaction.working is None:
                return False
            transaction.working = None
            return True

        async with self._lock_key(key):
            async with self._lock:
                if key not in self._sessions:
                    return False
                del self._sessions[key]
            logger.debug("Session deleted: user=%s, group=%s", key[0], key[1])
            return True

    async def exists(self, user_id: int, group_id: int | None) -> bool:
        """Check for an active session without refreshing its idle lease."""

        key = self._make_key(user_id, group_id)
        self._reject_cross_key_transaction(key)
        transaction = self._current_transaction(key)
        if transaction is not None:
            return transaction.working is not None
        async with self._lock_key(key):
            return await self._get_active_locked(key) is not None

    async def cleanup_expired(self) -> int:
        """Delete expired sessions after waiting for active key transactions."""

        self._reject_bulk_transaction_operation("cleanup_expired")
        async with self._lock:
            keys = list(self._sessions)

        expired_keys: list[SessionKey] = []
        for key in keys:
            async with self._lock_key(key):
                async with self._lock:
                    session = self._sessions.get(key)
                    if session is not None and session.is_expired():
                        del self._sessions[key]
                        expired_keys.append(key)

        if expired_keys:
            logger.debug("Cleaned up %d expired sessions", len(expired_keys))
        return len(expired_keys)

    async def count(self) -> int:
        """Return the number of stored sessions."""

        async with self._lock:
            return len(self._sessions)

    async def list_user_sessions(self, user_id: int) -> list[Session]:
        """Return detached active snapshots for one normalized user id."""

        normalized_user = int(_normalize_id(user_id, field_name="user_id"))
        async with self._lock:
            return [
                self._clone(session)
                for key, session in self._sessions.items()
                if key[0] == normalized_user and not session.is_expired()
            ]

    async def clear_plugin_sessions(self, plugin_name: str) -> int:
        """Delete every session belonging to one plugin."""

        self._reject_bulk_transaction_operation("clear_plugin_sessions")
        normalized_plugin = _normalize_plugin_name(plugin_name)
        async with self._lock:
            candidate_keys = list(self._sessions)

        keys_to_remove: list[SessionKey] = []
        for key in candidate_keys:
            async with self._lock_key(key):
                async with self._lock:
                    session = self._sessions.get(key)
                    if session is not None and session.plugin_name == normalized_plugin:
                        del self._sessions[key]
                        keys_to_remove.append(key)

        if keys_to_remove:
            logger.info(
                "Cleared %d sessions for plugin '%s'",
                len(keys_to_remove),
                normalized_plugin,
            )
        return len(keys_to_remove)

    async def get_all_sessions(self, plugin_name: str | None = None) -> list[Session]:
        """Return detached active snapshots, optionally filtered by plugin."""

        normalized_plugin = None if plugin_name is None else _normalize_plugin_name(plugin_name)
        async with self._lock:
            return [
                self._clone(session)
                for session in self._sessions.values()
                if not session.is_expired()
                and (normalized_plugin is None or session.plugin_name == normalized_plugin)
            ]
