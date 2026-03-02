"""
类型定义

为改进类型提示和代码可读性而定义的类型协议
"""

from typing import Any, Protocol, Optional

class Context(Protocol):
    """插件上下文协议"""
    current_user_id: Any
    current_group_id: Optional[Any]
    plugin_dir: str
    
    @property
    def logger(self) -> Any:
        """日志记录器"""
        ...
    
    async def create_session(
        self, 
        initial_data: dict[str, Any], 
        timeout: float
    ) -> None:
        """创建会话"""
        ...
    
    async def get_session(self) -> Optional['Session']:
        """获取当前会话"""
        ...
    
    async def end_session(self) -> None:
        """结束当前会话"""
        ...
    
    async def send_action(self, action: Any) -> None:
        """发送动作"""
        ...
    
    def get_secret(self, key: str) -> Optional[str]:
        """从 secrets.json 获取密钥"""
        ...

class Session(Protocol):
    """会话对象协议"""
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取会话数据"""
        ...
    
    def set(self, key: str, value: Any) -> None:
        """设置会话数据"""
        ...

# OneBot 事件类型
OneBotEvent = dict[str, Any]

# 消息段列表类型
MessageSegments = list[dict[str, Any]]
