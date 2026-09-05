"""把 Core 发布的青宠命令目录绑定到同步领域处理器。"""

from collections.abc import Callable, Mapping
from typing import Any

from core.router import CommandCatalogNode

CommandHandler = Callable[..., Any]


class CommandRouter:
    """只保存处理器；规范名和别名全部来自 Core 目录快照。"""

    def __init__(
        self,
        root: CommandCatalogNode,
        handlers: Mapping[str, CommandHandler],
    ) -> None:
        catalog_names = {child.name for child in root.children}
        handler_names = set(handlers)
        if catalog_names != handler_names:
            missing = sorted(catalog_names - handler_names)
            extra   = sorted(handler_names - catalog_names)
            raise ValueError(
                f"qingpet command catalog mismatch missing_handlers={missing} "
                f"extra_handlers={extra}"
            )

        self.root                    = root
        self.routes                  = dict(handlers)
        self.aliases: dict[str, str] = {}
        for child in root.children:
            self.aliases[child.name.casefold()] = child.name
            for alias in child.aliases:
                self.aliases[alias.casefold()] = child.name

    def get_handler(self, command: str) -> CommandHandler | None:
        """解析规范名或别名对应的处理器。"""

        return self.routes.get(self.resolve_command(command))

    def resolve_command(self, command: str) -> str:
        """把用户命令词归一化为目录中的规范名。"""

        normalized = command.casefold()
        return self.aliases.get(normalized, normalized)
