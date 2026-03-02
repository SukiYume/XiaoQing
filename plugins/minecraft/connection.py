"""
Minecraft 连接管理

管理多个 MC 服务器连接。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

@dataclass
class McConnection:
    """单个 MC 服务器连接"""
    host: str
    port: int
    password: str = field(repr=False)
    log_file: str
    target_type: str  # "group" 或 "private"
    target_id: int    # group_id 或 user_id
    rcon_client: Any = None
    log_monitor: Any = None

    async def cleanup(self) -> None:
        if self.rcon_client and hasattr(self.rcon_client, "disconnect"):
            await self.rcon_client.disconnect()
    
    def connection_key(self) -> str:
        """生成连接的唯一标识"""
        return f"{self.target_type}_{self.target_id}"

class ConnectionManager:
    """管理所有 MC 服务器连接"""
    
    def __init__(self):
        # key: "group_{group_id}" 或 "private_{user_id}"
        self._connections: dict[str, McConnection] = {}
    
    def get_key(self, group_id: Optional[int], user_id: Optional[int]) -> str:
        """根据 group_id 或 user_id 生成 key"""
        if group_id:
            return f"group_{group_id}"
        else:
            return f"private_{user_id}"
    
    def get_connection(self, group_id: Optional[int], user_id: Optional[int]) -> Optional[McConnection]:
        """获取当前 id 的连接"""
        key = self.get_key(group_id, user_id)
        return self._connections.get(key)
    
    def has_connection(self, group_id: Optional[int], user_id: Optional[int]) -> bool:
        """检查是否有连接"""
        key = self.get_key(group_id, user_id)
        return key in self._connections
    
    def add_connection(self, conn: McConnection) -> None:
        """添加连接"""
        key = conn.connection_key()
        self._connections[key] = conn
    
    def remove_connection(self, group_id: Optional[int], user_id: Optional[int]) -> Optional[McConnection]:
        """移除并返回连接"""
        key = self.get_key(group_id, user_id)
        return self._connections.pop(key, None)
    
    def all_connections(self) -> list[McConnection]:
        """获取所有活跃连接"""
        return list(self._connections.values())
    
    def connection_count(self) -> int:
        """获取连接数量"""
        return len(self._connections)

    async def cleanup_all(self) -> None:
        for conn in list(self._connections.values()):
            await conn.cleanup()
        self._connections.clear()
