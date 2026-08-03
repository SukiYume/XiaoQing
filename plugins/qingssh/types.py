"""QingSSH 与 core 交互所需的最小协议及消息类型。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import segments as _core_segments

OneBotEvent = dict[str, Any]
MessageSegments = list[dict[str, Any]]

# follow-imports=skip 时 core 的返回类型会退化为 Any，在插件边界集中恢复稳定契约。
segments = cast(Callable[[Any], MessageSegments], _core_segments)


class Context(Protocol):
    """命令、会话和清理入口实际使用的 core 上下文接口。"""

    current_user_id: int | None
    current_group_id: int | None
    plugin_dir: Path
    data_dir: Path
    state: dict[str, Any]
    session_manager: Any | None

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...

    @property
    def logger(self) -> Any:
        """日志记录器"""
        ...

    async def create_session(
        self,
        initial_data: dict[str, Any],
        timeout: float,
    ) -> None:
        """创建会话"""
        ...

    async def get_session(self) -> "Session | None":
        """获取当前会话"""
        ...

    async def update_session(self, callback: Callable[["Session"], Any]) -> Any:
        """在当前会话事务中执行原子读改写。"""
        ...

    async def end_session(self) -> None:
        """结束当前会话"""
        ...

    async def send_action(self, action: Any) -> None:
        """发送动作"""
        ...

    def get_secret(self, key: str) -> str | None:
        """从 secrets.json 获取密钥"""
        ...

    def set_secret(self, key: str, value: str) -> None: ...

    def delete_secret(self, key: str) -> bool: ...


class Session(Protocol):
    """会话处理器依赖的可变键值接口。"""

    plugin_name: str

    def get(self, key: str, default: Any = None) -> Any:
        """获取会话数据"""
        ...

    def set(self, key: str, value: Any) -> None:
        """设置会话数据"""
        ...


__all__ = ["Context", "MessageSegments", "OneBotEvent", "Session", "segments"]
