"""
命令路由器
负责解析命令并路由到相应的handler
"""

import logging
from typing import Any, Callable, Awaitable, Mapping, cast
from dataclasses import dataclass

from ..models.types import CommandResult, ErrorResult, SuccessResult

logger = logging.getLogger(__name__)

CommandHandler = Callable[[str, str, Any], Awaitable[CommandResult]]

# 顶层命令重定向提示：当用户误将顶层命令放到子模块下时使用
# 例如 /pendo event confirm xxx → 提示应使用 /pendo confirm xxx
TOP_LEVEL_REDIRECTS: dict[str, str] = {
    "confirm": "/pendo confirm <id>",
    "snooze": "/pendo snooze <id> <时间>",
    "undo": "/pendo undo",
}

# 命令元数据表：(别名列表, 描述, 用法)
# 不在此表中的命令仍可通过 handlers 字典注册，只是没有别名和描述
COMMAND_META: dict[str, tuple[list[str], str, str]] = {
    "confirm": (["确认"], "确认提醒", "/pendo confirm <id>"),
    "snooze": (["延后"], "延后提醒", "/pendo snooze <id> <时间>"),
    "undo": (["撤销"], "撤销删除或编辑", "/pendo undo [分钟]"),
    "event": (
        ["e", "日程", "事件"],
        "管理日程",
        "/pendo event <add|today|tomorrow|week|list|delete> [args]",
    ),
    "todo": (
        ["task", "t", "待办", "任务"],
        "管理待办事项",
        "/pendo todo <add|today|list|done|delete> [args]",
    ),
    "diary": (["d", "日记"], "写日记和查看日记", "/pendo diary <write|view|list> [args]"),
    "note": (
        ["n", "笔记", "想法", "灵感"],
        "记笔记",
        "/pendo note <content>",
    ),
    "search": (["s", "搜索", "查找"], "搜索内容", "/pendo search <关键词> [type=] [range=] [status=] [direction=] [category=]"),
    "ledger": (
        ["bill", "finance", "记账", "账单"],
        "记账管理",
        "/pendo ledger <add|quick|list|view|edit|delete|summary> [args]",
    ),
    "export": (
        ["导出"],
        "导出 Markdown 档案并私聊发送文件",
        "/pendo export <文件名> [range] [type]",
    ),
    "settings": (["setting", "设置"], "管理设置", "/pendo settings [key] [value]"),
    "help": (["h", "帮助", "?"], "显示帮助信息", "/pendo help [command]"),
    "web": (["webui", "网页"], "Web UI 管理", "/pendo web <token|start|stop|status>"),
}


@dataclass
class CommandInfo:
    """命令信息"""

    name: str
    handler: CommandHandler
    aliases: list[str]
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

    def __init__(
        self,
        handlers: Mapping[str, object],
        help_provider: Callable[[str], str] | None = None,
    ):
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
        """构建命令注册表

        双层注册：
        1. 已知命令的元数据（别名、描述、用法）从 COMMAND_META 查找
        2. handlers 字典中传入的所有命令都会被注册，即使没有预定义元数据
           （此时使用命令名本身作为描述，无别名）

        新增命令时只需在 main.py 的 handlers 字典中添加即可，
        如需别名/描述，再在 COMMAND_META 中补充。
        """
        commands = {}

        # 注册所有传入的 handler
        for key, handler_obj in self.handlers.items():
            # 解析 handler
            handler = self._resolve_handler_from_obj(handler_obj)
            if handler is None:
                handler = self._make_unimplemented_handler(key)

            # 查找元数据（别名、描述、用法）
            meta = COMMAND_META.get(key)
            if meta:
                aliases, description, usage = meta
            else:
                aliases, description, usage = [], key, f"/pendo {key}"

            commands[key] = CommandInfo(
                name=key, handler=handler, aliases=aliases, description=description, usage=usage
            )

        # 确保 help 命令始终存在
        if "help" not in commands:
            meta = COMMAND_META["help"]
            commands["help"] = CommandInfo(
                name="help",
                handler=self._handle_help,
                aliases=meta[0],
                description=meta[1],
                usage=meta[2],
            )

        return commands

    def _resolve_handler_from_obj(self, handler_obj: object) -> CommandHandler | None:
        """解析命令处理函数，支持传入可调用或对象方法"""
        if handler_obj is None:
            return None
        if callable(handler_obj):
            return cast(CommandHandler, handler_obj)
        # 对象实例：尝试 handle 方法
        if hasattr(handler_obj, "handle") and callable(getattr(handler_obj, "handle")):
            return cast(CommandHandler, getattr(handler_obj, "handle"))
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

    def _make_unimplemented_handler(self, command_name: str) -> CommandHandler:
        async def _handler(user_id: str, args: str, context: Any) -> CommandResult:
            result: ErrorResult = {
                "status": "error",
                "message": f"❌ {command_name} 功能暂不可用，请稍后再试",
                "error_code": None,
            }
            return result

        return _handler

    async def route(self, subcommand: str, user_id: str, args: str, context: Any) -> CommandResult:
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
            result: ErrorResult = {
                "status": "error",
                "message": f"❓ 未知命令: {subcommand}\n\n请使用 /pendo help 查看所有可用命令",
                "error_code": None,
            }
            return result

        # 获取命令信息
        cmd_info = self.commands[cmd_name]

        logger.info("Routing command: %s for user %s", cmd_name, user_id)
        return await cmd_info.handler(user_id, args, context)

    async def _handle_help(self, user_id: str, args: str, context: Any) -> CommandResult:
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

        result: SuccessResult = {
            "status": "success",
            "message": message,
            "item_id": None,
            "data": None,
        }
        return result

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
        lines = ["📖 Pendo 帮助", "", "可用命令:", ""]

        for cmd_name, cmd_info in self.commands.items():
            lines.append(f"• {cmd_name} - {cmd_info.description}")

        lines.extend(["", "使用 /pendo help <命令名> 查看详细用法"])

        return "\n".join(lines)

    def get_command_list(self) -> list[str]:
        """获取所有命令列表"""
        return list(self.commands.keys())

    def get_command_info(self, cmd_name: str) -> CommandInfo | None:
        """获取命令信息"""
        return self.commands.get(cmd_name)
