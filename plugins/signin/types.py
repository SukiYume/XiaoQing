"""Signin 与 core 交互所需的最小类型边界。"""

from collections.abc import Callable
from typing import Any, Protocol, cast

from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import segments as _core_segments

OneBotEvent = dict[str, Any]
MessageSegments = list[dict[str, Any]]
segments = cast(Callable[[Any], MessageSegments], _core_segments)


class Context(Protocol):
    """命令与定时签到实际使用的上下文属性。"""

    http_session: Any | None

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...


__all__ = ["Context", "MessageSegments", "OneBotEvent", "segments"]
