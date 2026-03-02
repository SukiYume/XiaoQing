"""Protocol interfaces for decoupling core components."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

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

    def reload(self) -> None:
        ...

    def save_secrets(self) -> None:
        ...

    def on_reload(self, callback) -> None:
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

SendAction = Callable[[dict[str, Any]], Awaitable[None]]

class PluginConfig(Protocol):
    config: dict[str, Any]
    secrets: dict[str, Any]

class PluginRuntime(Protocol):
    send_action: SendAction
    reload_config: Callable[[], Any]
    reload_plugins: Callable[[], None]
    list_commands: Callable[[], list[str]]
    list_plugins: Callable[[], list[str]]

class SessionAccess(Protocol):
    session_manager: Any
    current_user_id: Optional[int]
    current_group_id: Optional[int]

class PluginContextProtocol(PluginConfig, PluginRuntime, SessionAccess, Protocol):
    plugin_name: str
    plugin_dir: Path
    data_dir: Path
    logger: Any
    state: dict[str, Any]

    def default_groups(self) -> list[int]:
        ...

class ContextFactory(Protocol):
    def __call__(
        self,
        plugin_name: str,
        user_id: Optional[int] = None,
        group_id: Optional[int] = None,
        request_id: Optional[str] = None,
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
