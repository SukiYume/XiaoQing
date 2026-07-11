import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from .context import PluginContext

logger = logging.getLogger(__name__)
Handler = Callable[[str, str, dict[str, Any], "PluginContext"], Awaitable[list[dict[str, Any]]]]

@dataclass
class CommandSpec:
    plugin: str
    name: str
    triggers: list[str]
    help_text: str
    admin_only: bool
    handler: Handler
    priority: int = 0  # 优先级，数字越大越先匹配
    usage: str | None = None

class CommandRouter:
    def __init__(self) -> None:
        self._commands: list[CommandSpec] = []
        self._ordered_commands: list[CommandSpec] = []
        self._trigger_index: dict[str, list[tuple[CommandSpec, str]]] = {}
        self._index_dirty = True

    @staticmethod
    def _command_sort_key(item: CommandSpec) -> tuple[int, int]:
        return item.priority, max((len(t) for t in item.triggers), default=0)

    def _mark_index_dirty(self) -> None:
        self._index_dirty = True

    def _ensure_indexes(self) -> None:
        if not self._index_dirty:
            return

        self._ordered_commands = sorted(
            self._commands,
            key=self._command_sort_key,
            reverse=True,
        )

        trigger_index: dict[str, list[tuple[CommandSpec, str]]] = {}
        for spec in self._ordered_commands:
            for trigger in spec.triggers or []:
                if not trigger:
                    continue
                trigger_index.setdefault(trigger[0], []).append((spec, trigger))

        for candidates in trigger_index.values():
            candidates.sort(
                key=lambda item: (item[0].priority, len(item[1])),
                reverse=True,
            )

        self._trigger_index = trigger_index
        self._index_dirty = False

    def register(self, spec: CommandSpec) -> None:
        self._commands.append(spec)
        self._mark_index_dirty()

    def clear_plugin(self, plugin_name: str) -> None:
        self._commands = [cmd for cmd in self._commands if cmd.plugin != plugin_name]
        self._mark_index_dirty()

    def resolve(self, text: str) -> Optional[tuple[CommandSpec, str]]:
        """
        解析命令
        
        排序优先级：
        1. priority 数字越大越优先
        2. 同优先级时，trigger 越长越优先（避免短命令抢匹配）
        
        匹配规则：
        - trigger 必须是完整的词，不能是其他词的前缀
        - trigger 后面要么是空格（有参数），要么是字符串结束（无参数）
        - 例如：trigger "sh" 只匹配 "sh" 或 "sh arg"，不匹配 "showimg"
        """
        if not text:
            return None

        self._ensure_indexes()
        for spec, trigger in self._trigger_index.get(text[0], []):
            # 检查是否以 trigger 开头
            if text.startswith(trigger):
                # 获取 trigger 后的内容
                remainder = text[len(trigger):]
                # 确保 trigger 是完整的词：
                # 1. remainder 为空（完全匹配）
                # 2. remainder 以空格开头（后面有参数）
                if not remainder or remainder[0].isspace():
                    args = remainder.strip()
                    return spec, args
        return None

    def list_commands(self) -> list[str]:
        self._ensure_indexes()
        lines = []
        # 按插件分组并排序
        plugins_dict: dict[str, list[CommandSpec]] = {}
        for spec in self._ordered_commands:
            if spec.plugin not in plugins_dict:
                plugins_dict[spec.plugin] = []
            plugins_dict[spec.plugin].append(spec)
        
        # 遍历每个插件
        for plugin_name in sorted(plugins_dict.keys()):
            specs = plugins_dict[plugin_name]
            # 添加插件标题
            lines.append(f"\n[{plugin_name}]")
            
            for spec in specs:
                trigger = "/" + spec.name if spec.name else ""
                triggers = ", ".join(spec.triggers) if spec.triggers else trigger
                admin_mark = " 🔐" if spec.admin_only else ""
                lines.append(f"  {triggers}{admin_mark}")
                lines.append(f"    ↳ {spec.help_text}")
                if spec.usage:
                    lines.append(f"    ↳ 用法: {spec.usage}")
        
        return lines

