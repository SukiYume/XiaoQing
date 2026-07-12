"""Protocol interfaces for decoupling core components."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, Protocol


class AdminCheck(Protocol):
    def is_admin(self, user_id: Optional[int]) -> bool: ...


class ConfigProvider(Protocol):
    @property
    def config(self) -> dict[str, Any]: ...


class PluginRegistry(Protocol):
    def get(self, name: str) -> Any: ...


class MuteControl(Protocol):
    def mute_group(self, group_id: int, duration_minutes: float) -> None: ...

    def unmute_group(self, group_id: int) -> bool: ...

    def is_muted(self, group_id: Optional[int]) -> bool: ...

    def get_mute_remaining(self, group_id: int) -> float: ...


class ConfigManagerLike(Protocol):
    def update_secret(self, path: str, value: Any) -> None: ...

    def get_plugin_secret(self, plugin_name: str, path: str) -> Any: ...

    def set_plugin_secret(self, plugin_name: str, path: str, value: Any) -> None: ...

    def delete_plugin_secret(self, plugin_name: str, path: str) -> bool: ...

    def reload(self) -> None: ...

    def save_secrets(self) -> None: ...

    def on_reload(self, callback) -> Callable[[], None]: ...

    @property
    def config(self) -> dict[str, Any]: ...

    @property
    def secrets(self) -> dict[str, Any]: ...


class CommandLister(Protocol):
    def __call__(self) -> list[str]: ...


SendAction = Callable[[dict[str, Any]], Awaitable[bool]]


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
    """Core-issued identity snapshot propagated across plugin calls."""

    kind: Literal["user", "scheduled_system", "lifecycle"]
    user_id: int | None = None
    group_id: int | None = None
    is_bot_admin: bool = False
    is_private: bool = False
    group_role: Literal["owner", "admin", "member", "unknown"] = "unknown"
    delivery_targets: tuple[DeliveryTarget, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_targets, tuple) or any(
            not isinstance(target, DeliveryTarget) for target in self.delivery_targets
        ):
            raise TypeError("delivery_targets must be a tuple of DeliveryTarget values")

    @property
    def is_system(self) -> bool:
        return self.kind == "scheduled_system"

    def can_manage_group(self, target_group_id: int | None) -> bool:
        if self.kind != "user" or self.is_private or target_group_id is None:
            return False
        try:
            same_group = int(self.group_id or 0) == int(target_group_id)
        except (TypeError, ValueError):
            return False
        return same_group and self.group_role in {"owner", "admin"}


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
        callback: Callable[[dict[str, Any]], Any],
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


class PluginConfig(Protocol):
    config: dict[str, Any]
    secrets: dict[str, Any]


class PluginRuntime(Protocol):
    send_action: SendAction
    reload_config: Callable[[], Any]
    reload_plugins: Callable[[], Any]
    list_commands: Callable[[], list[str]]
    list_plugins: Callable[[], list[str]]


class SessionAccess(Protocol):
    session_manager: Any
    current_user_id: Optional[int]
    current_group_id: Optional[int]

    async def update_session(self, callback: Callable[[Any], Any]) -> Any: ...


class PluginContextProtocol(PluginConfig, PluginRuntime, SessionAccess, Protocol):
    plugin_name: str
    plugin_dir: Path
    data_dir: Path
    logger: Any
    state: dict[str, Any]
    principal: PluginPrincipal
    capabilities: PluginCapabilities

    def default_groups(self) -> list[int]: ...


class ContextFactory(Protocol):
    def __call__(
        self,
        plugin_name: str,
        user_id: Optional[int] = None,
        group_id: Optional[int] = None,
        request_id: Optional[str] = None,
        principal: PluginPrincipal | None = None,
    ) -> Any: ...


class PluginContextFactory(Protocol):
    def __call__(
        self,
        plugin_name: str,
        plugin_dir: Path,
        data_dir: Path,
        state: dict[str, Any],
        user_id: Optional[int] = None,
        group_id: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> Any: ...
