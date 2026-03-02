"""
命令路由器
负责解析命令并路由到相应的handler，参考 Pendo 插件实现
"""
import logging
import inspect
from typing import Any, Optional, Callable, Dict, List, Union, Tuple

logger = logging.getLogger(__name__)

class CommandRouter:
    """命令路由器"""
    
    def __init__(self):
        self.routes: Dict[str, Callable] = {}
        self.aliases: Dict[str, str] = {}
        self.descriptions: Dict[str, str] = {}
        
    def register(self, command: str, handler: Callable, aliases: List[str] = None, description: str = ""):
        """注册命令"""
        self.routes[command] = handler
        self.descriptions[command] = description
        
        if aliases:
            for alias in aliases:
                self.aliases[alias] = command
                
    async def route(self, command: str, *args, **kwargs) -> Any:
        """路由命令"""
        cmd_lower = command.lower()
        
        # 查找命令或者是别名
        target_cmd = self.aliases.get(cmd_lower, cmd_lower)
        
        handler = self.routes.get(target_cmd)
        if not handler:
            return None
            
        # 调用 handler
        # 检查 handler 是同步还是异步
        if inspect.iscoroutinefunction(handler):
            return await handler(*args, **kwargs)
        else:
            return handler(*args, **kwargs)
            
    def get_handler(self, command: str) -> Optional[Callable]:
        """获取命令处理器"""
        cmd_lower = command.lower()
        target_cmd = self.aliases.get(cmd_lower, cmd_lower)
        return self.routes.get(target_cmd)

    def is_valid_command(self, command: str) -> bool:
        """检查是否是有效命令"""
        cmd_lower = command.lower()
        return cmd_lower in self.routes or cmd_lower in self.aliases
