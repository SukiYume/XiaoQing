"""Typed models for core data structures."""

import json
import keyword
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .message import normalize_inbound_message, validate_message_segments
from .plugin_execution import PluginConcurrency

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def canonical_relative_path(raw: str, *, description: str = "path") -> str:
    """Validate the portable relative paths accepted in plugin manifests."""

    if type(raw) is not str or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError(f"{description} must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    windows_path = PureWindowsPath(raw)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{description} must be relative")
    if path.as_posix() != raw or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{description} must use one canonical POSIX spelling")
    if any(
        ":" in part
        or part.endswith((" ", "."))
        or any(ord(character) < 32 or character in '<>"|?*' for character in part)
        or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        for part in path.parts
    ):
        raise ValueError(f"{description} contains an invalid path component")
    return raw


def canonical_python_module_part(part: str) -> bool:
    return (
        type(part) is str
        and part.isidentifier()
        and not keyword.iskeyword(part)
        and unicodedata.normalize("NFKC", part) == part
    )


def canonical_plugin_name(raw: str) -> str:
    name = canonical_relative_path(raw, description="plugin name")
    if (
        "/" in name
        or not name.isascii()
        or name.casefold() != name
        or not canonical_python_module_part(name)
    ):
        raise ValueError("plugin name must be one lowercase ASCII Python identifier")
    return name


def canonical_python_entry(raw: str) -> str:
    entry = canonical_relative_path(raw, description="plugin entry")
    path = PurePosixPath(entry)
    if path.suffix != ".py":
        raise ValueError("plugin entry must name a lowercase .py source file")
    parents = tuple(part.casefold() for part in path.parts[:-1])
    module_parts = (*path.parts[:-1], path.stem)
    if (
        (parents and parents[0] == "data")
        or "__pycache__" in parents
        or path.stem.casefold() == "__init__"
        or any(not canonical_python_module_part(part) for part in module_parts)
    ):
        raise ValueError("plugin entry must map to one canonical Python module name")
    return entry


def canonical_plugin_watch_file(raw: str) -> str:
    relative = canonical_relative_path(raw, description="plugin watch file")
    path = PurePosixPath(relative)
    parents = tuple(part.casefold() for part in path.parts[:-1])
    if path.suffix != ".json":
        raise ValueError("plugin watch file must be a lowercase .json file")
    if (parents and parents[0] == "data") or "__pycache__" in parents:
        raise ValueError("plugin watch file uses a reserved runtime directory")
    return relative


class OneBotEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    time: int | None = Field(default=None, description="事件时间戳")
    self_id: int | None = Field(default=None, description="机器人 QQ 号")
    post_type: str | None = Field(default=None, description="事件类型")
    message_type: str | None = None
    sub_type: str | None = None
    message_id: int | None = None
    user_id: int | None = None
    group_id: int | None = None
    message: list[dict[str, Any]] | str | None = None
    raw_message: str | None = None

    @field_validator("message", mode="before")
    @classmethod
    def _coerce_message(cls, v: Any) -> list[dict[str, Any]] | str | None:
        """处理 message 字段的各种格式，某些 OneBot 实现可能发送空字符串而非列表"""
        if v is None:
            return None
        if v == "":
            return ""
        if isinstance(v, str):
            # JSON segment arrays use the strict structured contract. Other
            # strings, including ordinary JSON-looking text, remain text.
            try:
                parsed = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, list) and (
                not parsed or any(isinstance(item, dict) and "type" in item for item in parsed)
            ):
                return validate_message_segments(parsed)

            return [{"type": "text", "data": {"text": v}}]
        if isinstance(v, list):
            return validate_message_segments(v)
        raise ValueError("message must be text or a valid OneBot segment list")

    @model_validator(mode="after")
    def _fill_message_from_raw_message(self) -> "OneBotEvent":
        normalized = normalize_inbound_message(
            {"message": self.message, "raw_message": self.raw_message}
        )
        self.message = normalized["message"]
        return self


CommandContext = Literal["private", "group"]
CommandPermission = Literal["public", "bot_admin", "group_admin"]


def _default_command_contexts() -> list[CommandContext]:
    return ["private", "group"]


def _validate_command_token(value: str, *, description: str) -> str:
    """校验用户可见的命令词；允许中文和连字符，但禁止层级分隔符与空白。"""

    if type(value) is not str:
        raise ValueError(f"{description} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 64
        or normalized != value
        or "." in normalized
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        raise ValueError(
            f"{description} must be a non-empty token without dots, whitespace, or controls"
        )
    return normalized


def _validate_command_examples(values: list[str]) -> list[str]:
    """Validate the shared bounded example-list contract for command manifests."""

    if any(type(value) is not str or not value.strip() or len(value) > 5_000 for value in values):
        raise ValueError("command examples must be non-empty bounded strings")
    if len(values) != len(set(values)):
        raise ValueError("command examples must not contain duplicates")
    return values


def _validate_command_children(
    children: list["PluginCommandNodeManifest"],
) -> list["PluginCommandNodeManifest"]:
    """保证同级规范名和别名唯一，避免帮助目录与真实解析产生歧义。"""

    claimed: dict[str, str] = {}
    for child in children:
        for token in (child.name, *child.aliases):
            normalized = token.casefold()
            previous = claimed.get(normalized)
            if previous is not None:
                raise ValueError(
                    f"command token {token!r} conflicts with sibling command {previous!r}"
                )
            claimed[normalized] = child.name
    return children


class PluginCommandNodeManifest(BaseModel):
    """一个可递归查询的用户命令节点，不包含处理器实现。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    aliases: list[str] = Field(default_factory=list, max_length=32)
    help: str
    usage: str
    match: Literal["prefix", "exact"] = "prefix"
    # 省略时继承父命令；Core 发布目录时会展开为最终有效值。
    permission: CommandPermission | None = None
    contexts: list[CommandContext] | None = None
    examples: list[str] = Field(default_factory=list, max_length=16)
    invalid_examples: list[str] = Field(default_factory=list, max_length=16)
    subcommands: list["PluginCommandNodeManifest"] = Field(default_factory=list, max_length=128)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _validate_command_token(value, description="command name")

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, values: list[str]) -> list[str]:
        normalized = [
            _validate_command_token(value, description="command alias") for value in values
        ]
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("command aliases must not contain duplicates")
        return normalized

    @field_validator("help", "usage")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if type(value) is not str or not value.strip() or len(value) > 2_000:
            raise ValueError("command help and usage must be non-empty bounded strings")
        return value.strip()

    @field_validator("contexts")
    @classmethod
    def _validate_contexts(
        cls,
        values: list[CommandContext] | None,
    ) -> list[CommandContext] | None:
        if values is None:
            return None
        if not values or len(values) != len(set(values)):
            raise ValueError("command contexts must be non-empty and unique")
        return values

    @field_validator("examples", "invalid_examples")
    @classmethod
    def _validate_examples(cls, values: list[str]) -> list[str]:
        return _validate_command_examples(values)

    @field_validator("subcommands")
    @classmethod
    def _validate_subcommands(
        cls,
        values: list["PluginCommandNodeManifest"],
    ) -> list["PluginCommandNodeManifest"]:
        return _validate_command_children(values)

    @model_validator(mode="after")
    def _validate_own_aliases(self) -> "PluginCommandNodeManifest":
        if self.name.casefold() in {alias.casefold() for alias in self.aliases}:
            raise ValueError("command aliases must not repeat the canonical name")
        return self


class PluginCommandManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    triggers: list[str]
    help: str
    admin_only: bool = False
    priority: int = 0
    usage: str | None = None
    permission: CommandPermission = "public"
    contexts: list[CommandContext] = Field(default_factory=_default_command_contexts)
    examples: list[str] = Field(default_factory=list, max_length=16)
    invalid_examples: list[str] = Field(default_factory=list, max_length=16)
    subcommands: list[PluginCommandNodeManifest] = Field(default_factory=list, max_length=128)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _validate_command_token(value, description="command name")

    @field_validator("triggers")
    @classmethod
    def _validate_triggers(cls, values: list[str]) -> list[str]:
        normalized = [
            _validate_command_token(value, description="command trigger") for value in values
        ]
        # 顶层路由当前区分大小写；例如 qingssh 明确同时接受 ``ssh`` 与 ``SSH``。
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("command triggers must be non-empty and unique")
        return normalized

    @field_validator("contexts")
    @classmethod
    def _validate_contexts(cls, values: list[CommandContext]) -> list[CommandContext]:
        if not values or len(values) != len(set(values)):
            raise ValueError("command contexts must be non-empty and unique")
        return values

    @field_validator("help")
    @classmethod
    def _validate_help(cls, value: str) -> str:
        if type(value) is not str or not value.strip() or len(value) > 2_000:
            raise ValueError("command help must be a non-empty bounded string")
        return value.strip()

    @field_validator("usage")
    @classmethod
    def _validate_usage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.strip() or len(value) > 2_000:
            raise ValueError("command usage must be a non-empty bounded string")
        return value.strip()

    @field_validator("examples", "invalid_examples")
    @classmethod
    def _validate_examples(cls, values: list[str]) -> list[str]:
        return _validate_command_examples(values)

    @field_validator("subcommands")
    @classmethod
    def _validate_subcommands(
        cls,
        values: list[PluginCommandNodeManifest],
    ) -> list[PluginCommandNodeManifest]:
        return _validate_command_children(values)

    @model_validator(mode="after")
    def _normalize_permission(self) -> "PluginCommandManifest":
        if self.admin_only:
            self.permission = "bot_admin"
        return self


class PluginScheduleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handler: str
    cron: dict[str, Any]
    id: str | None = None
    group_ids: list[int] | None = None
    description: str | None = None
    enabled: bool = True

    @field_validator("group_ids", mode="before")
    @classmethod
    def _reject_boolean_group_ids(cls, value: Any) -> Any:
        if isinstance(value, list) and any(isinstance(item, bool) for item in value):
            raise ValueError("schedule group_ids must not contain booleans")
        return value

    @field_validator("group_ids")
    @classmethod
    def _validate_group_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("schedule group_ids must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("schedule group_ids must not contain duplicates")
        return value


class PluginDependencyManifest(BaseModel):
    """A Python module dependency checked before the plugin is imported."""

    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool = True
    description: str | None = None


PluginServiceName = Literal[
    "voice.synthesize_text",
    "chat.reply",
    "codex.enqueue_arxiv_summary",
    "core.observe_outgoing_action",
]

PluginCapabilityName = Literal[
    "admin_sessions",
    "config_subscription",
    "execution_timeout_exempt",
    "onebot_media",
    "secret_admin",
]


class PluginServiceManifest(BaseModel):
    """One narrowly declared inter-plugin service export.

    Service names and their consumers are intentionally closed over the small
    set of bridges maintained by the core.  Adding a bridge therefore requires
    an explicit core/schema change instead of making an arbitrary module
    attribute remotely callable.
    """

    model_config = ConfigDict(extra="forbid")

    name: PluginServiceName
    callback: str
    callers: list[str]
    required_capability: Literal["codex_arxiv_summary"] | None = None

    @field_validator("callback")
    @classmethod
    def _validate_callback(cls, value: str) -> str:
        if not value.isidentifier() or value.startswith("_"):
            raise ValueError("service callback must be a public Python identifier")
        return value

    @field_validator("callers")
    @classmethod
    def _validate_callers(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("service callers must not be empty")
        if any(not item.isidentifier() or item.startswith("_") for item in value):
            raise ValueError("service callers must be public plugin identifiers")
        if len(value) != len(set(value)):
            raise ValueError("service callers must not contain duplicates")
        return value


_SERVICE_CONTRACTS: dict[
    PluginServiceName,
    tuple[str, frozenset[str], str | None],
] = {
    "voice.synthesize_text": ("voice", frozenset({"smalltalk"}), None),
    "chat.reply": ("chat", frozenset({"smalltalk"}), None),
    "codex.enqueue_arxiv_summary": (
        "codex",
        frozenset({"arxiv_filter"}),
        "codex_arxiv_summary",
    ),
    "core.observe_outgoing_action": (
        "xiaoqing_chat",
        frozenset({"core"}),
        None,
    ),
}

_CAPABILITY_CONTRACTS: dict[PluginCapabilityName, frozenset[str]] = {
    "admin_sessions": frozenset({"codex", "jupyter", "minecraft", "qingssh", "shell"}),
    "config_subscription": frozenset({"pendo"}),
    "execution_timeout_exempt": frozenset({"codex", "jupyter", "qingssh", "shell"}),
    "onebot_media": frozenset({"xiaoqing_chat"}),
    "secret_admin": frozenset({"bot_core"}),
}


class PluginManifest(BaseModel):
    """Versioned, strict manifest for a plugin's runtime contract.

    `description` and `author` are operational metadata emitted at load time;
    `dependencies` names importable Python modules, not plugin load-order edges.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    schema_version: Literal[1] = 1
    version: str = "0.0.0"
    description: str | None = None
    author: str | None = None
    entry: str = "main.py"
    commands: list[PluginCommandManifest] = Field(default_factory=list, max_length=128)
    schedule: list[PluginScheduleManifest] = Field(default_factory=list)
    dependencies: list[PluginDependencyManifest] = Field(default_factory=list)
    services: list[PluginServiceManifest] = Field(default_factory=list)
    uses_services: list[PluginServiceName] = Field(default_factory=list)
    capabilities: list[PluginCapabilityName] = Field(default_factory=list)
    watch_files: list[str] = Field(default_factory=list, max_length=64)
    concurrency: PluginConcurrency = "parallel"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return canonical_plugin_name(value)

    @field_validator("watch_files")
    @classmethod
    def _validate_watch_files(cls, value: list[str]) -> list[str]:
        normalized = [canonical_plugin_watch_file(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("plugin watch_files must not contain duplicates")
        return normalized

    @field_validator("entry")
    @classmethod
    def _validate_entry(cls, value: str) -> str:
        return canonical_python_entry(value)

    @model_validator(mode="after")
    def _validate_service_contracts(self) -> "PluginManifest":
        command_names = [command.name.casefold() for command in self.commands]
        if len(command_names) != len(set(command_names)):
            raise ValueError("plugin commands must not contain duplicate stable names")

        command_count = 0
        stack: list[
            tuple[PluginCommandManifest | PluginCommandNodeManifest, int]
        ] = [(command, 1) for command in self.commands]
        while stack:
            command, depth = stack.pop()
            command_count += 1
            if command_count > 512:
                raise ValueError("plugin command catalog must not exceed 512 nodes")
            if depth > 8:
                raise ValueError("plugin command catalog must not exceed 8 levels")
            stack.extend((child, depth + 1) for child in command.subcommands)

        names = [service.name for service in self.services]
        if len(names) != len(set(names)):
            raise ValueError("plugin services must not contain duplicate names")
        for service in self.services:
            owner, callers, required_capability = _SERVICE_CONTRACTS[service.name]
            if self.name != owner:
                raise ValueError(f"service {service.name} may only be exported by {owner}")
            if frozenset(service.callers) != callers:
                expected = ", ".join(sorted(callers))
                raise ValueError(f"service {service.name} callers must be exactly: {expected}")
            if service.required_capability != required_capability:
                raise ValueError(
                    f"service {service.name} required_capability must be {required_capability!r}"
                )
        if len(self.uses_services) != len(set(self.uses_services)):
            raise ValueError("plugin uses_services must not contain duplicates")
        for service_name in self.uses_services:
            _owner, callers, _required_capability = _SERVICE_CONTRACTS[service_name]
            if self.name not in callers:
                raise ValueError(f"plugin {self.name} may not consume service {service_name}")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("plugin capabilities must not contain duplicates")
        for capability in self.capabilities:
            allowed_plugins = _CAPABILITY_CONTRACTS[capability]
            if self.name not in allowed_plugins:
                raise ValueError(f"plugin {self.name} may not request capability {capability}")
        return self
