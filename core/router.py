"""线程安全的命令注册、索引构建与最长触发词路由。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import PluginContext
    from .plugin_execution import PluginExecutionGate

logger = logging.getLogger(__name__)
Handler = Callable[[str, str, dict[str, Any], "PluginContext"], Awaitable[list[dict[str, Any]]]]


class CommandConflictError(ValueError):
    """Two commands claimed one trigger at the same explicit priority."""

    def __init__(
        self,
        *,
        trigger: str,
        priority: int,
        first: CommandSpec,
        second: CommandSpec,
    ) -> None:
        self.trigger = trigger
        self.priority = priority
        self.first = first
        self.second = second
        super().__init__(
            "ambiguous command trigger "
            f"{trigger!r} at priority {priority}: "
            f"{first.plugin}.{first.name} conflicts with {second.plugin}.{second.name}"
        )


@dataclass(frozen=True, slots=True)
class CommandCatalogNode:
    """Core 持有的不可变命令目录节点；帮助、导出和测试共享该快照。"""

    code: str
    plugin: str
    path: tuple[str, ...]
    name: str
    aliases: tuple[str, ...]
    help_text: str
    usage: str
    match_mode: str = "prefix"
    permission: str = "public"
    contexts: tuple[str, ...] = ("private", "group")
    examples: tuple[str, ...] = ()
    invalid_examples: tuple[str, ...] = ()
    children: tuple[CommandCatalogNode, ...] = ()

    def walk(self) -> tuple[CommandCatalogNode, ...]:
        """按目录顺序展平自身及全部后代。"""

        descendants = tuple(node for child in self.children for node in child.walk())
        return (self, *descendants)

    def resolve_child(self, token: str) -> CommandCatalogNode | None:
        """按规范名或别名解析一个直接子命令。"""

        normalized = token.casefold()
        for child in self.children:
            if normalized == child.name.casefold() or any(
                normalized == alias.casefold() for alias in child.aliases
            ):
                return child
        return None

    def to_dict(self) -> dict[str, Any]:
        """返回无处理器、可直接 JSON 序列化的公开目录。"""

        return {
            "code": self.code,
            "plugin": self.plugin,
            "path": list(self.path),
            "name": self.name,
            "aliases": list(self.aliases),
            "help": self.help_text,
            "usage": self.usage,
            "match": self.match_mode,
            "permission": self.permission,
            "contexts": list(self.contexts),
            "examples": list(self.examples),
            "invalid_examples": list(self.invalid_examples),
            "subcommands": [child.to_dict() for child in self.children],
        }


def format_command_catalog(
    root: CommandCatalogNode,
    *,
    title: str | None = None,
    include_examples: bool = False,
) -> str:
    """把一个目录子树渲染成可读帮助；命令码和用法始终完整保留。"""

    lines = [title or f"📚 {root.plugin} 命令目录", ""]
    for node in root.walk():
        depth = max(0, len(node.path) - len(root.path))
        marker = "  " * depth + "•"
        lines.append(f"{marker} [{node.code}] {node.usage}")
        lines.append(f"{'  ' * (depth + 1)}{node.help_text}")
        if node.aliases:
            lines.append(f"{'  ' * (depth + 1)}别名: {', '.join(node.aliases)}")
        if node.permission != "public" or node.contexts != ("private", "group"):
            context_label = "/".join(node.contexts)
            lines.append(f"{'  ' * (depth + 1)}权限: {node.permission}; 场景: {context_label}")
        if include_examples:
            if node.examples:
                lines.append(f"{'  ' * (depth + 1)}示例: {node.examples[0]}")
            if node.invalid_examples:
                lines.append(f"{'  ' * (depth + 1)}错误示例: {node.invalid_examples[0]}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """Core 对一条输入做出的目录路径解析结果。"""

    root: CommandCatalogNode
    chain: tuple[CommandCatalogNode, ...]
    remainders: tuple[str, ...]

    @property
    def node(self) -> CommandCatalogNode:
        return self.chain[-1]

    @property
    def arguments(self) -> str:
        """返回消费完最深命令路径后的业务参数。"""

        return self.remainders[-1]

    def remainder_after(self, depth: int) -> str:
        """返回指定路径深度后的原始余串；根节点深度为零。"""

        if depth < 0 or depth >= len(self.remainders):
            raise IndexError("command invocation depth is out of range")
        return self.remainders[depth]


def _effective_command_permission(parent: str, declared: object) -> str:
    """合并父子权限，子命令不能比父命令更宽松。"""

    permission = declared if declared in {"public", "bot_admin", "group_admin"} else parent
    if "bot_admin" in {parent, permission}:
        return "bot_admin"
    if "group_admin" in {parent, permission}:
        return "group_admin"
    return "public"


def build_command_catalog_node(
    plugin_name: str,
    command: Mapping[str, Any],
    *,
    parent_code_path: tuple[str, ...] = (),
    parent_path: tuple[str, ...] = (),
    parent_permission: str = "public",
    parent_contexts: tuple[str, ...] = ("private", "group"),
    root: bool = False,
) -> CommandCatalogNode:
    """把已校验的 manifest 命令递归展开成 Core 的不可变公开快照。"""

    code_name = str(command["name"])
    if root:
        triggers = tuple(str(value) for value in command["triggers"])
        name = triggers[0]
        aliases = triggers[1:]
    else:
        name = code_name
        aliases = tuple(str(value) for value in command.get("aliases", ()))

    code_path = (*parent_code_path, code_name)
    path = (*parent_path, name)
    permission = _effective_command_permission(parent_permission, command.get("permission"))
    declared_contexts = command.get("contexts")
    if declared_contexts is None:
        contexts = parent_contexts
    else:
        allowed = frozenset(str(value) for value in declared_contexts)
        contexts = tuple(value for value in parent_contexts if value in allowed)
    if not contexts:
        raise ValueError(f"command {plugin_name}.{'.'.join(code_path)} has no usable context")

    usage = command.get("usage") or f"/{' '.join(path)}"
    children = tuple(
        build_command_catalog_node(
            plugin_name,
            child,
            parent_code_path=code_path,
            parent_path=path,
            parent_permission=permission,
            parent_contexts=contexts,
        )
        for child in command.get("subcommands", ())
    )
    return CommandCatalogNode(
        code=f"{plugin_name}.{'.'.join(code_path)}",
        plugin=plugin_name,
        path=path,
        name=name,
        aliases=aliases,
        help_text=str(command["help"]),
        usage=str(usage),
        match_mode=str(command.get("match", "prefix")),
        permission=permission,
        contexts=contexts,
        examples=tuple(str(value) for value in command.get("examples", ())),
        invalid_examples=tuple(str(value) for value in command.get("invalid_examples", ())),
        children=children,
    )


def resolve_catalog_invocation(root: CommandCatalogNode, args: str) -> CommandInvocation:
    """从根节点开始最长解析子命令，同时保留每一级之后的参数。"""

    chain = [root]
    remaining = str(args or "").strip()
    remainders = [remaining]
    current = root
    while remaining and current.children:
        parts = remaining.split(maxsplit=1)
        child = current.resolve_child(parts[0])
        if child is None:
            break
        next_remaining = parts[1] if len(parts) > 1 else ""
        if child.match_mode == "exact" and next_remaining:
            break
        remaining = next_remaining
        chain.append(child)
        remainders.append(remaining)
        current = child
    return CommandInvocation(root, tuple(chain), tuple(remainders))


def get_context_command_root(context: Any, code: str) -> CommandCatalogNode | None:
    """从一次请求或 Core 快照取得指定根命令，供复杂插件共享解析事实。"""

    invocation = getattr(context, "command_invocation", None)
    if isinstance(invocation, CommandInvocation) and invocation.root.code == code:
        return invocation.root
    getter = getattr(context, "get_command_catalog", None)
    if not callable(getter):
        return None
    catalog = getter()
    if not isinstance(catalog, (tuple, list)):
        return None
    return next(
        (root for root in catalog if isinstance(root, CommandCatalogNode) and root.code == code),
        None,
    )


def resolve_context_command_invocation(
    context: Any,
    code: str,
    args: str,
) -> CommandInvocation | None:
    """复用 Dispatcher 解析结果；直接调用处理器时仍从同一目录解析。"""

    invocation = getattr(context, "command_invocation", None)
    if isinstance(invocation, CommandInvocation) and invocation.root.code == code:
        return invocation
    root = get_context_command_root(context, code)
    return resolve_catalog_invocation(root, args) if root is not None else None


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
    catalog: CommandCatalogNode | None = field(default=None, repr=False, compare=False)
    # 执行门既控制并发，也标识插件加载代数；已解析命令不能迁移到新代数的执行门。
    execution_gate: PluginExecutionGate | None = field(
        default=None,
        repr=False,
        compare=False,
    )


class CommandRouter:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._commands: list[CommandSpec] = []
        self._ordered_commands: list[CommandSpec] = []
        self._trigger_index: dict[str, list[tuple[CommandSpec, str]]] = {}
        self._index_dirty = True

    @staticmethod
    def _command_sort_key(item: CommandSpec) -> tuple[int, int]:
        return item.priority, max((len(t) for t in item.triggers), default=0)

    def _mark_index_dirty(self) -> None:
        self._index_dirty = True

    @staticmethod
    def _validate_unambiguous(commands: list[CommandSpec]) -> None:
        owners: dict[tuple[str, int], CommandSpec] = {}
        for spec in commands:
            for trigger in spec.triggers:
                key = (trigger, spec.priority)
                previous = owners.get(key)
                if previous is not None:
                    raise CommandConflictError(
                        trigger=trigger,
                        priority=spec.priority,
                        first=previous,
                        second=spec,
                    )
                owners[key] = spec

    def _ensure_indexes(self) -> None:
        with self._lock:
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
        with self._lock:
            replacement = [*self._commands, spec]
            self._validate_unambiguous(replacement)
            self._commands = replacement
            self._mark_index_dirty()

    def replace_plugin(
        self,
        plugin_name: str,
        specs: list[CommandSpec],
    ) -> tuple[CommandSpec, ...]:
        """Atomically replace one plugin's complete command generation."""

        with self._lock:
            previous = tuple(command for command in self._commands if command.plugin == plugin_name)
            replacement = [command for command in self._commands if command.plugin != plugin_name]
            replacement.extend(specs)
            self._validate_unambiguous(replacement)
            self._commands = replacement
            self._mark_index_dirty()
            return previous

    def clear_plugin(self, plugin_name: str) -> None:
        self.replace_plugin(plugin_name, [])

    def resolve(self, text: str) -> tuple[CommandSpec, str] | None:
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

        with self._lock:
            self._ensure_indexes()
            candidates = tuple(self._trigger_index.get(text[0], ()))
        for spec, trigger in candidates:
            # 检查是否以 trigger 开头
            if text.startswith(trigger):
                # 获取 trigger 后的内容
                remainder = text[len(trigger) :]
                # 确保 trigger 是完整的词：
                # 1. remainder 为空（完全匹配）
                # 2. remainder 以空格开头（后面有参数）
                if not remainder or remainder[0].isspace():
                    args = remainder.strip()
                    return spec, args
        return None

    def get_command_catalog(self) -> tuple[CommandCatalogNode, ...]:
        """返回当前已发布插件代的完整不可变命令目录。"""

        with self._lock:
            self._ensure_indexes()
            ordered_commands = tuple(self._ordered_commands)
        catalog = []
        for spec in ordered_commands:
            if spec.catalog is not None:
                catalog.append(spec.catalog)
                continue
            trigger = spec.triggers[0] if spec.triggers else spec.name
            usage = spec.usage or (f"/{trigger}" if trigger else "")
            catalog.append(
                CommandCatalogNode(
                    code=f"{spec.plugin}.{spec.name}",
                    plugin=spec.plugin,
                    path=(trigger,),
                    name=trigger,
                    aliases=tuple(spec.triggers[1:]),
                    help_text=spec.help_text,
                    usage=usage,
                    permission="bot_admin" if spec.admin_only else "public",
                )
            )
        return tuple(sorted(catalog, key=lambda node: (node.plugin, node.code)))
