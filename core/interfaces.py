"""Protocol interfaces for decoupling core components.

这些 Protocol 只描述 Core 与插件之间的最小结构契约，不承担运行时适配。Principal
是 Core 签发的身份快照，Capabilities 是由 Core 按该身份重新计算的窄权限集合；插件
不得从事件原文自行扩权。两者分开保留是为了同时表达“谁在调用”和“本次允许做什么”。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from .ai import AICompletionResult, AIModelInfo
    from .config import ConfigSnapshot
    from .router import CommandCatalogNode
    from .session import Session


class AdminCheck(Protocol):
    def is_admin(self, user_id: int | None) -> bool: ...


class ConfigProvider(Protocol):
    @property
    def config(self) -> Mapping[str, Any]: ...


class PluginRegistry(Protocol):
    def get(self, name: str) -> Any: ...


class MuteControl(Protocol):
    def mute_group(self, group_id: int, duration_minutes: float) -> None: ...

    def unmute_group(self, group_id: int) -> bool: ...

    def is_muted(self, group_id: int | None) -> bool: ...

    def get_mute_remaining(self, group_id: int) -> float:
        """Return the remaining mute duration in minutes."""
        ...


class ConfigManagerLike(Protocol):
    def update_secret(self, path: str, value: Any) -> None: ...

    def get_plugin_secret(self, plugin_name: str, path: str) -> Any: ...

    def set_plugin_secret(self, plugin_name: str, path: str, value: Any) -> None: ...

    def delete_plugin_secret(self, plugin_name: str, path: str) -> bool: ...

    def reload(self, *, notify: bool = False) -> ConfigSnapshot: ...

    def snapshot(self) -> ConfigSnapshot: ...

    def save_secrets(self) -> None: ...

    def on_reload(
        self,
        callback: Callable[[ConfigSnapshot], Any],
    ) -> Callable[[], None]: ...

    def on_security_update(
        self,
        callback: Callable[[ConfigSnapshot], None],
    ) -> Callable[[], None]: ...

    @property
    def config(self) -> Mapping[str, Any]: ...

    @property
    def secrets(self) -> Mapping[str, Any]: ...


SendAction = Callable[[dict[str, Any]], Awaitable[bool | None]]

# Action metadata is an in-process Core/plugin contract, not a OneBot field.
# Keep the names here so producers and consumers cannot drift on magic keys.
ACTION_BYPASS_SINK_KEY = "_bypass_sink"
ACTION_RESULT_MESSAGE_ID_KEY = "_result_message_id"


@dataclass(frozen=True, slots=True)
class PluginSettingsSnapshot:
    """One atomic, plugin-scoped view of public config and private settings."""

    config: Mapping[str, Any]
    secrets: Mapping[str, Any]
    revision: int
    config_status: str = "valid"
    secrets_status: str = "valid"

    @staticmethod
    def _plugin_namespace(
        source: Mapping[str, Any],
        plugin_name: str,
    ) -> Mapping[str, Any]:
        plugins = source.get("plugins")
        if not isinstance(plugins, Mapping):
            return {}
        namespace = plugins.get(plugin_name)
        return namespace if isinstance(namespace, Mapping) else {}

    def plugin_config(self, plugin_name: str) -> Mapping[str, Any]:
        """Return this generation's public namespace for one plugin."""

        return self._plugin_namespace(self.config, plugin_name)

    def plugin_secrets(self, plugin_name: str) -> Mapping[str, Any]:
        """Return this generation's private namespace for one plugin."""

        return self._plugin_namespace(self.secrets, plugin_name)


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """A core-validated immutable private or group delivery destination."""

    kind: Literal["private", "group"]
    target_id: int

    def __post_init__(self) -> None:
        if self.kind not in {"private", "group"}:
            raise ValueError("delivery target kind must be private or group")
        if isinstance(self.target_id, bool) or not isinstance(self.target_id, int):
            raise TypeError("delivery target id must be an integer")
        if self.target_id <= 0:
            raise ValueError("delivery target id must be positive")

    @property
    def user_id(self) -> int | None:
        return self.target_id if self.kind == "private" else None

    @property
    def group_id(self) -> int | None:
        return self.target_id if self.kind == "group" else None


@dataclass(frozen=True, eq=False)
class PluginPrincipal:
    """Core-issued identity snapshot propagated across plugin calls.

    ``eq=False`` 是安全语义：应用用对象身份登记签发记录，复制出字段相同的对象也不
    能冒充原票据。字段组合在构造时收紧，避免私聊/群聊范围互相矛盾。
    """

    kind: Literal["user", "scheduled_system", "lifecycle"]
    user_id: int | None = None
    group_id: int | None = None
    is_bot_admin: bool = False
    is_private: bool = False
    group_role: Literal["owner", "admin", "member", "unknown"] = "unknown"
    delivery_targets: tuple[DeliveryTarget, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"user", "scheduled_system", "lifecycle"}:
            raise ValueError("principal kind is invalid")
        for field_name, value in (("user_id", self.user_id), ("group_id", self.group_id)):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{field_name} must be a positive integer or None")
        if type(self.is_bot_admin) is not bool or type(self.is_private) is not bool:
            raise TypeError("principal flags must be booleans")
        if self.group_role not in {"owner", "admin", "member", "unknown"}:
            raise ValueError("principal group role is invalid")
        if self.kind == "user":
            if self.user_id is None:
                raise ValueError("user principal requires user_id")
            if self.is_private and self.group_id is not None:
                raise ValueError("private user principal must not have group_id")
            if self.group_id is None and self.group_role != "unknown":
                raise ValueError("principal without group_id must have unknown group role")
        elif (
            self.user_id is not None
            or self.group_id is not None
            or self.is_bot_admin
            or self.is_private
            or self.group_role != "unknown"
        ):
            raise ValueError("non-user principal must not carry user scope")
        if not isinstance(self.delivery_targets, tuple) or any(
            not isinstance(target, DeliveryTarget) for target in self.delivery_targets
        ):
            raise TypeError("delivery_targets must be a tuple of DeliveryTarget values")

    @property
    def is_system(self) -> bool:
        return self.kind == "scheduled_system"

    def can_manage_group(self, target_group_id: int | None) -> bool:
        if (
            self.kind != "user"
            or self.is_private
            or type(target_group_id) is not int
            or target_group_id <= 0
        ):
            return False
        return self.group_id == target_group_id and self.group_role in {"owner", "admin"}


class SecretAdminCapability(Protocol):
    def get(self, path: str) -> Any: ...

    def set(self, path: str, value: Any) -> None: ...


class OneBotMediaCapability(Protocol):
    async def get_message(self, message_id: int | str) -> dict[str, Any]: ...

    async def get_image(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
    ) -> dict[str, Any]: ...


class PluginConfigSubscription(Protocol):
    def subscribe(
        self,
        callback: Callable[[Mapping[str, Any]], Any],
    ) -> Callable[[], None]: ...


class CodexArxivSummaryCapability(Protocol):
    """Narrow, core-issued bridge from arxiv_filter to the Codex sidecar."""

    async def enqueue_or_replay(
        self,
        *,
        date: str,
        links: list[str],
    ) -> str: ...


class VoiceSynthesisCapability(Protocol):
    async def synthesize_text(self, text: str) -> list[dict[str, Any]] | None: ...


class ChatReplyCapability(Protocol):
    async def reply(
        self,
        text: str,
        event: dict[str, Any],
    ) -> list[dict[str, Any]]: ...


class AICapability(Protocol):
    async def complete(
        self,
        route: str,
        messages: list[dict[str, Any]],
        *,
        required_modalities: tuple[str, ...] = ("text",),
        pinned_model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        total_timeout_seconds: float | None = None,
        max_retry: int | None = None,
        retry_interval_seconds: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> AICompletionResult: ...

    def list_models(
        self,
        route: str,
        *,
        required_modalities: tuple[str, ...] = ("text",),
    ) -> tuple[AIModelInfo, ...]: ...


@dataclass(frozen=True)
class PluginCapabilities:
    is_bot_admin: bool = False
    is_system: bool = False
    secret_admin: SecretAdminCapability | None = None
    onebot_media: OneBotMediaCapability | None = None
    config_subscription: PluginConfigSubscription | None = None
    codex_arxiv_summary: CodexArxivSummaryCapability | None = None
    voice_synthesis: VoiceSynthesisCapability | None = None
    chat_reply: ChatReplyCapability | None = None
    ai: AICapability | None = None


class PluginConfig(Protocol):
    config: Mapping[str, Any]
    secrets: Mapping[str, Any]


class PluginRuntime(Protocol):
    send_action: SendAction
    reload_config: Callable[[], Any]
    reload_plugins: Callable[[], Any]
    get_command_catalog: Callable[[], tuple[CommandCatalogNode, ...]]
    list_plugins: Callable[[], list[str]]


class SessionAccess(Protocol):
    session_manager: Any
    current_user_id: int | None
    current_group_id: int | None

    async def create_session(
        self,
        initial_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Session: ...

    async def get_session(self) -> Session | None: ...

    async def update_session(
        self,
        callback: Callable[[Session], Any],
    ) -> Any: ...
    async def end_session(self) -> bool: ...

    async def has_session(self) -> bool: ...


class PluginContextProtocol(PluginConfig, PluginRuntime, SessionAccess, Protocol):
    plugin_name: str
    plugin_dir: Path
    data_dir: Path
    logger: Any
    # One dictionary is shared by events for the current plugin generation;
    # unload/reload or process restart discards it.
    state: dict[str, Any]
    principal: PluginPrincipal
    capabilities: PluginCapabilities

    def now(self) -> Any: ...

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...

    def get_config(self, path: str) -> Any: ...

    def get_secret(self, path: str) -> Any: ...

    def default_groups(self) -> list[int]: ...

    def is_global_admin(self, user_id: int | None = None) -> bool: ...

    def mute_group(self, group_id: int, duration_minutes: float) -> None: ...

    def unmute_group(self, group_id: int) -> bool: ...

    def is_group_muted(self, group_id: int) -> bool: ...

    def get_mute_remaining(self, group_id: int) -> float: ...


class ContextFactory(Protocol):
    def __call__(
        self,
        plugin_name: str,
        user_id: int | None = None,
        group_id: int | None = None,
        request_id: str | None = None,
        principal: PluginPrincipal | None = None,
    ) -> Any: ...
