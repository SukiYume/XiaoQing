"""Pendo 顶层子命令、别名与帮助路由。"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from core.router import CommandCatalogNode

from .types import CommandMessage

logger = logging.getLogger(__name__)

CommandHandler = Callable[[str, str, Any], Awaitable[CommandMessage]]

# 用户误把顶层操作写进业务子模块时，返回唯一正确入口。
TOP_LEVEL_REDIRECTS: Final[dict[str, str]] = {
    "confirm": "/pendo confirm <id>",
    "snooze": "/pendo snooze <id> <时间>",
    "undo": "/pendo undo",
}


@dataclass(frozen=True, slots=True)
class CommandInfo:
    """一个已经验证并可直接调用的顶层命令。"""

    name: str
    handler: CommandHandler
    aliases: tuple[str, ...]
    description: str
    usage: str


class CommandRouter:
    """把规范命令或别名路由到对应的异步处理函数。"""

    def __init__(
        self,
        root: CommandCatalogNode,
        handlers: Mapping[str, CommandHandler],
        help_provider: Callable[[str], str] | None = None,
    ) -> None:
        if root.code != "pendo.pendo":
            raise ValueError(f"unexpected Pendo command root: {root.code}")
        self.root          = root
        self.help_provider = help_provider
        self.commands      = self._build_command_registry(root, handlers)
        self.alias_map     = self._build_alias_map()
        logger.info("CommandRouter initialized with %s commands", len(self.commands))

    def _build_command_registry(
        self,
        root: CommandCatalogNode,
        handlers: Mapping[str, CommandHandler],
    ) -> dict[str, CommandInfo]:
        """只从 Core 目录读取命令元数据，处理器映射只负责业务实现。"""
        catalog_by_name = {child.name: child for child in root.children}
        missing         = sorted((set(handlers) | {"help"}) - set(catalog_by_name))
        if missing:
            raise ValueError(f"Pendo handlers and command catalog disagree: missing={missing}")

        commands: dict[str, CommandInfo] = {}
        selected_names                   = (*handlers, "help")
        for name in selected_names:
            node           = catalog_by_name[name]
            handler        = self._handle_help if name == "help" else handlers[name]
            commands[name] = CommandInfo(
                name,
                handler,
                node.aliases,
                node.help_text,
                node.usage,
            )

        return commands

    def _build_alias_map(self) -> dict[str, str]:
        """把规范名和所有别名压平成一次查询表。"""
        aliases: dict[str, str] = {}
        for command_name, command in self.commands.items():
            aliases[command_name] = command_name
            aliases.update(dict.fromkeys(command.aliases, command_name))
        return aliases

    async def route(
        self,
        subcommand: str,
        user_id: str,
        args: str,
        context: Any,
    ) -> CommandMessage:
        """执行命令；未知命令只返回固定帮助提示。"""
        command_name = self.alias_map.get(subcommand.lower())
        if command_name is None:
            logger.info("Unknown command: %s", subcommand)
            return {
                "status": "error",
                "message": f"❓ 未知命令: {subcommand}\n\n请使用 /pendo help 查看所有可用命令",
                "error_code": None,
            }
        logger.info("Routing command: %s for user %s", command_name, user_id)
        return await self.commands[command_name].handler(user_id, args, context)

    async def _handle_help(
        self,
        _user_id: str,
        args: str,
        _context: Any,
    ) -> CommandMessage:
        """把 help 也作为普通命令返回统一消息结构。"""
        return {
            "status": "success",
            "message": self.get_help_message(args.strip()),
            "item_id": None,
            "data": None,
        }

    def get_help_message(self, args: str = "") -> str:
        """优先复用完整帮助源，否则由命令元数据生成简版帮助。"""
        target = (args or "").strip()
        if target:
            if self.help_provider:
                provided = self.help_provider(target)
                if not provided.startswith("❌ 未知命令"):
                    return provided

            command_name = self.alias_map.get(target.lower())
            if command_name is None:
                return f"❌ 未知命令: {target}\n\n使用 /pendo help 查看所有命令"
            if self.help_provider:
                return self.help_provider(command_name)

            command = self.commands[command_name]
            aliases = ", ".join(command.aliases) or "无"
            return (
                f"📖 {command.name} - {command.description}\n\n"
                f"用法:\n{command.usage}\n\n别名: {aliases}"
            )

        if self.help_provider:
            return self.help_provider("")

        lines = ["📖 Pendo 帮助", "", "可用命令:", ""]
        lines.extend(
            f"• {command.name} - {command.description}" for command in self.commands.values()
        )
        lines.extend(["", "使用 /pendo help <命令名> 查看详细用法"])
        return "\n".join(lines)
