"""
会话管理器

用于支持多轮对话场景（如猜数字游戏）。
每个用户可以有一个活跃会话，会话中存储对话状态。
设计为线程安全，支持并发访问。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable
import logging

logger = logging.getLogger(__name__)

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

    def update(self) -> None:
        """更新会话时间戳"""
        self.updated_at = time.time()

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
    - 线程安全（使用 asyncio.Lock）
    
    会话键格式：(user_id, group_id)
    - group_id 为 None 时表示私聊会话
    - group_id 有值时表示群聊会话（同一用户在不同群有不同会话）
    """

    def __init__(self, default_timeout: float = 300.0) -> None:
        self._sessions: dict[tuple, Session] = {}
        self._lock = asyncio.Lock()
        self._default_timeout = default_timeout

    def _make_key(self, user_id: int, group_id: int | None) -> tuple:
        """生成会话键"""
        return (user_id, group_id)

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

        async with self._lock:
            key = self._make_key(user_id, group_id)
            session = Session(
                user_id=user_id,
                group_id=group_id,
                plugin_name=plugin_name,
                data=initial_data if isinstance(initial_data, dict) else {},
                timeout=timeout or self._default_timeout,
            )
            self._sessions[key] = session
            logger.debug("Session created: user=%s, group=%s, plugin=%s", user_id, group_id, plugin_name)
            return session

    async def get(self, user_id: int, group_id: int | None) -> Session | None:
        """
        获取用户会话

        如果会话已过期，会自动删除并返回 None。
        每次成功获取会话时刷新超时计时器。
        """
        async with self._lock:
            key = self._make_key(user_id, group_id)
            session = self._sessions.get(key)

            if session is None:
                return None

            if session.is_expired():
                del self._sessions[key]
                logger.debug("Session expired and removed: user=%s, group=%s", user_id, group_id)
                return None

            # 刷新超时计时器，避免活跃对话意外过期
            session.update()
            return session

    async def delete(self, user_id: int, group_id: int | None) -> bool:
        """
        删除用户会话
        
        返回是否成功删除。
        """
        async with self._lock:
            key = self._make_key(user_id, group_id)
            if key in self._sessions:
                del self._sessions[key]
                logger.debug("Session deleted: user=%s, group=%s", user_id, group_id)
                return True
            return False

    async def exists(self, user_id: int, group_id: int | None) -> bool:
        """检查会话是否存在（且未过期）"""
        async with self._lock:
            key = self._make_key(user_id, group_id)
            session = self._sessions.get(key)
            if session is None:
                return False
            if session.is_expired():
                del self._sessions[key]
                logger.debug("Session expired and removed during exists(): user=%s, group=%s", user_id, group_id)
                return False
            return True

    async def cleanup_expired(self) -> int:
        """
        清理所有过期会话
        
        返回清理的会话数量。
        """
        async with self._lock:
            expired_keys = [
                key for key, session in self._sessions.items()
                if session.is_expired()
            ]
            for key in expired_keys:
                del self._sessions[key]
            
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
            keys_to_remove = [
                key for key, session in self._sessions.items()
                if session.plugin_name == plugin_name
            ]
            for key in keys_to_remove:
                del self._sessions[key]

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
