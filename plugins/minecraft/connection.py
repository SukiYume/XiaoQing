"""Minecraft 连接对象与并发安全的连接注册表。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from core.interfaces import DeliveryTarget

from .audit import audit_error_type
from .log_monitor import LogMonitor
from .rcon import RconClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class McConnection:
    """一个 QQ 投递目标当前使用的 Minecraft 连接。"""

    host: str
    port: int
    target: DeliveryTarget
    rcon_client: RconClient | None = None
    log_monitor: LogMonitor | None = None

    async def cleanup(self) -> None:
        """幂等关闭 RCON；先摘除引用，避免并发清理重复使用旧客户端。"""

        client, self.rcon_client = self.rcon_client, None
        if client is not None:
            await client.disconnect()


class ConnectionManager:
    """以 core 已验证的投递目标为键，原子发布和移除连接。"""

    def __init__(self) -> None:
        self._connections: dict[DeliveryTarget, McConnection] = {}
        self._lock                                            = asyncio.Lock()

    def get_connection(self, target: DeliveryTarget) -> McConnection | None:
        """返回当前快照；所有写操作均在同一事件循环内原子发布。"""

        return self._connections.get(target)

    async def replace_connection(self, conn: McConnection) -> McConnection | None:
        """先发布新连接，再尽力关闭同一目标的旧连接。"""

        async with self._lock:
            old                            = self._connections.get(conn.target)
            self._connections[conn.target] = conn

        if old is not None and old is not conn:
            await self._cleanup_safely(old, operation="replace")
        return old

    async def disconnect_connection(self, target: DeliveryTarget) -> McConnection | None:
        """原子移除指定目标的连接，并在锁外关闭网络资源。"""

        async with self._lock:
            conn = self._connections.pop(target, None)
        if conn is not None:
            await self._cleanup_safely(conn, operation="disconnect")
        return conn

    def all_connections(self) -> list[McConnection]:
        """返回活跃连接的稳定快照，避免调度期间遍历可变字典。"""

        return list(self._connections.values())

    async def cleanup_all(self) -> None:
        """一次摘除全部连接，并保证单个关闭失败不影响其余连接。"""

        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        await asyncio.gather(
            *(self._cleanup_safely(conn, operation="shutdown") for conn in connections)
        )

    @staticmethod
    async def _cleanup_safely(conn: McConnection, *, operation: str) -> None:
        try:
            await conn.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Minecraft connection cleanup status=failed operation=%s error_type=%s",
                operation,
                audit_error_type(exc),
            )


__all__ = ["ConnectionManager", "McConnection"]
