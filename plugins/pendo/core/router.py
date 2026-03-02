"""
命令路由器
负责解析命令并路由到相应的handler
"""
import logging
from typing import Any, Optional, Callable
from dataclasses import dataclass

from ..models.types import CommandResult

logger = logging.getLogger(__name__)

@dataclass
class CommandInfo:
    """命令信息"""
    name: str
    handler: Callable
    aliases: list
    description: str
    usage: str

class CommandRouter:
    """命令路由器
    
    负责:
    1. 解析用户输入的命令
    2. 路由到对应的handler
    3. 处理命令别名
    4. 提供命令帮助信息
    """
    
    def __init__(self, handlers: dict[str, Any], help_provider: Optional[Callable[[str], str]] = None):
        """初始化路由器
        
        Args:
            handlers: handler实例字典，如:
                {
                    'event': EventHandler(...),
                    'task': TaskHandler(...),
                    ...
                }
        """
        self.handlers = handlers
        self.help_provider = help_provider
        self.commands = self._build_command_registry()
        self.alias_map = self._build_alias_map()
        logger.info("CommandRouter initialized with %s commands", len(self.commands))
    
    def _build_command_registry(self) -> dict[str, CommandInfo]:
        """构建命令注册表（配置驱动）
        
        使用配置数组统一定义所有命令，避免重复的handler解析代码。
        新增命令时只需在配置数组中添加一项即可。
        """
        # 统一的handler配置数组
        # 格式: (命令键, 优先方法列表, 回退方法, 默认handler函数, 别名列表, 描述, 用法)
        handler_configs = [
            ('confirm', ['handle_confirm'], None, None,
             ['确认'], '确认提醒', '/pendo confirm <id>'),
            
            ('snooze', ['handle_snooze'], None, None,
             ['延后'], '延后提醒', '/pendo snooze <id> <时间>'),
            
            ('undo', ['handle_undo'], None, None,
             ['撤销'], '撤销删除或编辑', '/pendo undo [分钟]'),
            
            ('event', ['handle_event_command', 'handle_event'], 'handle', None,
             ['e', '日程', '事件'], '管理日程', '/pendo event <add|today|tomorrow|week|list|delete> [args]'),
            
            ('todo', ['handle_task_command', 'handle_task'], 'handle', None,
             ['task', 't', '待办', '任务'], '管理待办事项', '/pendo todo <add|today|list|done|delete> [args]'),
            
            ('diary', ['handle_diary_command', 'handle_diary'], 'handle', None,
             ['d', '日记'], '写日记和查看日记', '/pendo diary <write|view|list> [args]'),
            
            ('note', ['handle_note_command', 'handle_note'], 'handle', None,
             ['n', '笔记', '想法', '灵感'], '记笔记', '/pendo note <content>'),
            
            ('search', ['search'], 'handle', None,
             ['s', '搜索', '查找'], '搜索内容', '/pendo search <关键词> [type=<类型>] [range=<时间范围>]'),
            
            ('export', ['handle_export'], None, self._make_unimplemented_handler('export'),
             ['导出'], '导出数据', '/pendo export md [range=<时间范围>]'),
            
            ('import', ['handle_import'], None, self._make_unimplemented_handler('import'),
             ['导入'], '导入数据', '/pendo import md <发送文件>'),
            
            ('settings', ['handle_settings'], None, self._make_unimplemented_handler('settings'),
             ['setting', '设置'], '管理设置', '/pendo settings [key] [value]'),
            
            ('help', ['handle_help'], None, self._handle_help,
             ['h', '帮助', '?'], '显示帮助信息', '/pendo help [command]'),
        ]
        
        # 构建命令字典
        commands = {}
        for config in handler_configs:
            key, preferred_attrs, fallback_attr, default_handler, aliases, description, usage = config
            
            # 解析handler
            handler = self._resolve_handler(key, preferred_attrs, fallback_attr) or default_handler
            
            commands[key] = CommandInfo(
                name=key,
                handler=handler,
                aliases=aliases,
                description=description,
                usage=usage
            )
        
        return commands

    def _resolve_handler(self, key: str, preferred_attrs: list, fallback_attr: Optional[str]) -> Optional[Callable]:
        """解析命令处理函数，支持传入可调用或对象方法"""
        handler = self.handlers.get(key)
        if handler is None:
            return None
        if callable(handler):
            return handler
        for attr in preferred_attrs:
            if hasattr(handler, attr):
                return getattr(handler, attr)
        if fallback_attr and hasattr(handler, fallback_attr):
            return getattr(handler, fallback_attr)
        return None
    
    def _build_alias_map(self) -> dict[str, str]:
        """构建别名映射表"""
        alias_map = {}
        for cmd_name, cmd_info in self.commands.items():
            # 命令名本身
            alias_map[cmd_name] = cmd_name
            # 所有别名
            for alias in cmd_info.aliases:
                alias_map[alias] = cmd_name
        return alias_map

    def _make_unimplemented_handler(self, command_name: str) -> Callable:
        async def _handler(user_id: str, args: str, context: Any) -> CommandResult:
            return {
                'status': 'error',
                'message': f"❌ {command_name} 功能暂不可用，请稍后再试"
            }

        return _handler
    
    async def route(self, subcommand: str, user_id: str, args: str, 
                   context: Any) -> CommandResult:
        """路由命令到对应的handler
        
        Args:
            subcommand: 子命令（如 'event', 'todo'等）
            user_id: 用户ID
            args: 命令参数
            context: 上下文对象
            
        Returns:
            命令执行结果
        """
        # 解析别名
        cmd_name = self.alias_map.get(subcommand.lower())
        
        if not cmd_name:
            # 未知命令，返回帮助提示
            logger.info("Unknown command: %s", subcommand)
            return {
                'status': 'error',
                'message': (
                    f"❓ 未知命令: {subcommand}\n\n"
                    "请使用 /pendo help 查看所有可用命令"
                )
            }
        
        # 获取命令信息
        cmd_info = self.commands[cmd_name]

        logger.info("Routing command: %s for user %s", cmd_name, user_id)
        return await cmd_info.handler(user_id, args, context)
    
    async def _handle_help(self, user_id: str, args: str, 
                          context: Any) -> CommandResult:
        """处理help命令
        
        Args:
            user_id: 用户ID
            args: 参数（可选的具体命令名）
            context: 上下文
            
        Returns:
            帮助信息
        """
        args = args.strip()
        
        message = self.get_help_message(args)
        
        return {
            'status': 'success',
            'message': message,
            'item_id': None
        }

    def get_help_message(self, args: str = "") -> str:
        """获取帮助信息文本"""
        args = (args or "").strip()

        # 如果指定了命令，显示该命令的详细帮助
        if args:
            cmd_name = self.alias_map.get(args.lower())
            if cmd_name:
                cmd_info = self.commands[cmd_name]
                return (
                    f"📖 {cmd_info.name} - {cmd_info.description}\n\n"
                    f"用法:\n{cmd_info.usage}\n\n"
                    f"别名: {', '.join(cmd_info.aliases)}"
                )
            return f"❌ 未知命令: {args}\n\n使用 /pendo help 查看所有命令"

        # 使用外部提供的帮助文本（避免重复定义）
        if self.help_provider:
            return self.help_provider("")

        # 默认帮助
        lines = [
            "📖 Pendo 帮助",
            "",
            "可用命令:",
            ""
        ]

        for cmd_name, cmd_info in self.commands.items():
            lines.append(f"• {cmd_name} - {cmd_info.description}")

        lines.extend([
            "",
            "使用 /pendo help <命令名> 查看详细用法"
        ])

        return "\n".join(lines)
    
    def get_command_list(self) -> list:
        """获取所有命令列表"""
        return list(self.commands.keys())
    
    def get_command_info(self, cmd_name: str) -> Optional[CommandInfo]:
        """获取命令信息"""
        return self.commands.get(cmd_name)
