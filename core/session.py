"""
会话管理器

用于支持多轮对话场景（如猜数字游戏）。
每个用户可以有一个活跃会话，会话中存储对话状态。
设计为线程安全，支持并发访问。
"""

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .async_keyed_lock import AsyncKeyedLockPool

logger = logging.getLogger(__name__)
T = TypeVar("T")

@dataclass
class Session:
    """用户会话"""
    user_id: int
    group_id: int | None  # None 表示私聊
    plugin_name: str  # 会话所属插件
    state: str = "active"  # 会话状态
    data: dict[str, Any] = field(default_factory=dict)  # 会话数据
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    timeout: float = 300.0  # 会话超时时间（秒），默认 5 分钟
    version: int = 0  # 由 SessionManager 事务和写入操作递增

    def update(self) -> None:
        """更新会话时间戳"""
        self.updated_at = time.time()
        self.version += 1

    def is_expired(self) -> bool:
        """检查会话是否过期"""
        return time.time() - self.updated_at > self.timeout

    def get(self, key: str, default: Any = None) -> Any:
        """获取会话数据"""
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置会话数据"""
        self.data[key] = value
        self.update()

    def __getitem__(self, key: str) -> Any:
        """支持 session['key'] 读取"""
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """支持 session['key'] = value 写入"""
        self.data[key] = value
        self.update()

    def __contains__(self, key: str) -> bool:
        """支持 'key' in session"""
        return key in self.data

    def clear(self) -> None:
        """清空会话数据"""
        self.data.clear()
        self.update()

class SessionManager:
    """
    会话管理器
    
    支持：
    - 创建/获取/删除用户会话
    - 会话超时自动清理
    - 每个会话键的事务性读改写（不同键可并行）
    
    会话键格式：(user_id, group_id)
    - group_id 为 None 时表示私聊会话
    - group_id 有值时表示群聊会话（同一用户在不同群有不同会话）
    """

    def __init__(self, default_timeout: float = 300.0) -> None:
        self._sessions: dict[tuple[int, int | None], Session] = {}
        self._lock = asyncio.Lock()
        self._key_lock_pool = AsyncKeyedLockPool(max_keys=4096, max_key_length=128)
        self._default_timeout = default_timeout

    @property
    def default_timeout(self) -> float:
        return self._default_timeout

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def active_key_lock_count(self) -> int:
        return self._key_lock_pool.active_key_count

    def set_default_timeout(self, timeout: float) -> None:
        self._default_timeout = float(timeout)

    def _make_key(self, user_id: int, group_id: int | None) -> tuple[int, int | None]:
        """生成会话键"""
        return (user_id, group_id)

    @asynccontextmanager
    async def _lock_key(self, key: tuple[int, int | None]):
        async with self._key_lock_pool.hold(key):
            yield

    async def _get_active_locked(self, key: tuple[int, int | None]) -> Session | None:
        """Read an active session while its per-key lock is held."""

        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if session.is_expired():
                del self._sessions[key]
                logger.debug("Session expired and removed: user=%s, group=%s", key[0], key[1])
                return None
            return session

    async def create(
        self,
        user_id: int,
        group_id: int | None,
        plugin_name: str,
        initial_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Session:
        """
        创建新会话

        如果已存在会话，会覆盖旧会话。
        """
        # 防御：如果误传了 Session 对象作为 initial_data，提取其 data 字段
        if isinstance(initial_data, Session):
            logger.warning("Session object passed as initial_data, extracting .data dict")
            initial_data = initial_data.data if isinstance(initial_data.data, dict) else {}

        key = self._make_key(user_id, group_id)
        async with self._lock_key(key):
            session = Session(
                user_id=user_id,
                group_id=group_id,
                plugin_name=plugin_name,
                data=initial_data if isinstance(initial_data, dict) else {},
                timeout=timeout or self._default_timeout,
            )
            async with self._lock:
                self._sessions[key] = session
            logger.debug(
                "Session created: user=%s, group=%s, plugin=%s",
                user_id,
                group_id,
                plugin_name,
            )
            return session

    async def get(self, user_id: int, group_id: int | None) -> Session | None:
        """
        获取用户会话

        如果会话已过期，会自动删除并返回 None。
        每次成功获取会话时刷新超时计时器。
        """
        key = self._make_key(user_id, group_id)
        async with self._lock_key(key):
            session = await self._get_active_locked(key)
            if session is None:
                return None
            # Compatibility read API.  Mutable writes through this returned
            # object are not transactional; use update() for read-modify-write.
            session.update()
            return session

    async def peek(self, user_id: int, group_id: int | None) -> Session | None:
        """
        只读查看用户会话。

        如果会话已过期，会自动删除并返回 None。
        成功返回会话时不会刷新超时计时器；需要续命时使用 get()。
        """
        key = self._make_key(user_id, group_id)
        async with self._lock_key(key):
            return await self._get_active_locked(key)

    async def update(
        self,
        user_id: int,
        group_id: int | None,
        callback: Callable[[Session], T | Awaitable[T]],
    ) -> T | None:
        """Atomically run a read-modify-write callback for one session.

        The callback may await.  The manager retains the per-key lock until it
        finishes, so same-session events cannot overwrite each other while
        unrelated users/groups continue concurrently.  The session is touched
        on entry and committed again on successful completion.  Returning
        ``None`` means that no active session was available or the callback
        itself produced ``None``.
        """

        key = self._make_key(user_id, group_id)
        async with self._lock_key(key):
            session = await self._get_active_locked(key)
            if session is None:
                return None
            session.update()
            result = callback(session)
            if inspect.isawaitable(result):
                result = await result
            # The per-key lock prevents create/delete/cleanup from changing
            # this entry mid-transaction.  Re-check supports a callback that
            # intentionally ended or replaced its own session.
            async with self._lock:
                if self._sessions.get(key) is session:
                    session.update()
            return result

    async def delete(self, user_id: int, group_id: int | None) -> bool:
        """
        删除用户会话
        
        返回是否成功删除。
        """
        key = self._make_key(user_id, group_id)
        async with self._lock_key(key):
            async with self._lock:
                if key in self._sessions:
                    del self._sessions[key]
                    logger.debug("Session deleted: user=%s, group=%s", user_id, group_id)
                    return True
                return False

    async def exists(self, user_id: int, group_id: int | None) -> bool:
        """检查会话是否存在（且未过期），不会刷新超时计时器。"""
        key = self._make_key(user_id, group_id)
        async with self._lock_key(key):
            return await self._get_active_locked(key) is not None

    async def cleanup_expired(self) -> int:
        """
        清理所有过期会话
        
        返回清理的会话数量。
        """
        async with self._lock:
            keys = list(self._sessions)

        expired_keys: list[tuple[int, int | None]] = []
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
        """返回活跃会话数量"""
        async with self._lock:
            return len(self._sessions)

    async def list_user_sessions(self, user_id: int) -> list:
        """列出用户的所有会话"""
        async with self._lock:
            return [
                session for key, session in self._sessions.items()
                if key[0] == user_id and not session.is_expired()
            ]

    async def clear_plugin_sessions(self, plugin_name: str) -> int:
        """清理指定插件的所有会话（用于插件 reload）

        Returns:
            清理的会话数量
        """
        async with self._lock:
            candidate_keys = list(self._sessions)

        keys_to_remove: list[tuple[int, int | None]] = []
        for key in candidate_keys:
            async with self._lock_key(key):
                async with self._lock:
                    session = self._sessions.get(key)
                    if session is not None and session.plugin_name == plugin_name:
                        del self._sessions[key]
                        keys_to_remove.append(key)

        if keys_to_remove:
            logger.info("Cleared %d sessions for plugin '%s'", len(keys_to_remove), plugin_name)
        return len(keys_to_remove)

    async def get_all_sessions(self, plugin_name: str | None = None) -> list[Session]:
        """获取所有活跃会话（可选按插件筛选）"""
        async with self._lock:
            if plugin_name:
                return [
                    s for s in self._sessions.values() 
                    if s.plugin_name == plugin_name and not s.is_expired()
                ]
            return [
                s for s in self._sessions.values() 
                if not s.is_expired()
            ]
