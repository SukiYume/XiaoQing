"""Signin 与 core 交互所需的最小上下文协议。"""

from typing import Any, Protocol

from core.interfaces import PluginSettingsSnapshot


class Context(Protocol):
    """命令与定时签到实际使用的上下文属性。"""

    http_session: Any | None

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...


__all__ = ["Context"]
