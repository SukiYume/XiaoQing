"""Protocol interfaces for decoupling core components."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, Protocol


class AdminCheck(Protocol):
    def is_admin(self, user_id: Optional[int]) -> bool:
        ...

class ConfigProvider(Protocol):
    @property
    def config(self) -> dict[str, Any]:
        ...

class PluginRegistry(Protocol):
    def get(self, name: str) -> Any:
        ...

class MuteControl(Protocol):
    def mute_group(self, group_id: int, duration_minutes: float) -> None:
        ...

    def unmute_group(self, group_id: int) -> bool:
        ...

    def is_muted(self, group_id: Optional[int]) -> bool:
        ...

    def get_mute_remaining(self, group_id: int) -> float:
        ...

class ConfigManagerLike(Protocol):
    def update_secret(self, path: str, value: Any) -> None:
        ...

    def get_plugin_secret(self, plugin_name: str, path: str) -> Any:
        ...

    def set_plugin_secret(self, plugin_name: str, path: str, value: Any) -> None:
        ...

    def delete_plugin_secret(self, plugin_name: str, path: str) -> bool:
        ...

    def reload(self) -> None:
        ...

    def save_secrets(self) -> None:
        ...

    def on_reload(self, callback) -> Callable[[], None]:
        ...

    @property
    def config(self) -> dict[str, Any]:
        ...

    @property
    def secrets(self) -> dict[str, Any]:
        ...

class CommandLister(Protocol):
    def __call__(self) -> list[str]:
        ...

SendAction = Callable[[dict[str, Any]], Awaitable[bool]]


@dataclass(frozen=True, eq=False)
class PluginPrincipal:
    """Core-issued identity snapshot propagated across plugin calls."""

    kind: Literal["user", "scheduled_system", "lifecycle"]
    user_id: int | None = None
    group_id: int | None = None
    is_bot_admin: bool = False
    is_private: bool = False
    group_role: Literal["owner", "admin", "member", "unknown"] = "unknown"

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
    def get(self, path: str) -> Any:
        ...

    def set(self, path: str, value: Any) -> None:
        ...


class OneBotMediaCapability(Protocol):
    async def get_message(self, message_id: int | str) -> dict[str, Any]:
        ...

    async def get_image(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
    ) -> dict[str, Any]:
        ...


class PluginConfigSubscription(Protocol):
    def subscribe(
        self,
        callback: Callable[[dict[str, Any]], Any],
    ) -> Callable[[], None]:
        ...


@dataclass(frozen=True)
class PluginCapabilities:
    is_bot_admin: bool = False
    is_system: bool = False
    secret_admin: SecretAdminCapability | None = None
    onebot_media: OneBotMediaCapability | None = None
    config_subscription: PluginConfigSubscription | None = None

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

    async def update_session(self, callback: Callable[[Any], Any]) -> Any:
        ...

class PluginContextProtocol(PluginConfig, PluginRuntime, SessionAccess, Protocol):
    plugin_name: str
    plugin_dir: Path
    data_dir: Path
    logger: Any
    state: dict[str, Any]
    principal: PluginPrincipal
    capabilities: PluginCapabilities

    def default_groups(self) -> list[int]:
        ...

    async def call_plugin(
        self,
        plugin_name: str,
        callback_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        ...

class ContextFactory(Protocol):
    def __call__(
        self,
        plugin_name: str,
        user_id: Optional[int] = None,
        group_id: Optional[int] = None,
        request_id: Optional[str] = None,
        principal: PluginPrincipal | None = None,
    ) -> Any:
        ...

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
    ) -> Any:
        ...
