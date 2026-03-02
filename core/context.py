import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

from .interfaces import ConfigManagerLike, MuteControl, SendAction

if TYPE_CHECKING:
    from .session import Session, SessionManager
    from .metrics import MetricsCollector

class _RequestLogger:
    def __init__(self, base_logger: logging.Logger, request_id: str | None) -> None:
        self._base_logger = base_logger
        self._request_id = request_id

    def _with_request_id(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        extra = dict(kwargs.get("extra", {}))
        extra["request_id"] = self._request_id
        return {**kwargs, "extra": extra}

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._base_logger.debug(msg, *args, **self._with_request_id(kwargs))

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._base_logger.info(msg, *args, **self._with_request_id(kwargs))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._base_logger.warning(msg, *args, **self._with_request_id(kwargs))

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._base_logger.error(msg, *args, **self._with_request_id(kwargs))

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._base_logger.exception(msg, *args, **self._with_request_id(kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base_logger, name)

@dataclass
class PluginContext:
    config: dict[str, Any]
    secrets: dict[str, Any]
    plugin_name: str
    plugin_dir: Path
    data_dir: Path
    http_session: aiohttp.ClientSession | None
    send_action: SendAction
    reload_config: Callable[[], Any]
    reload_plugins: Callable[[], None]
    list_commands: Callable[[], list[str]]
    list_plugins: Callable[[], list[str]]
    metrics: "MetricsCollector | None" = None
    # 会话管理器（用于多轮对话）
    session_manager: "SessionManager | None" = None
    # 当前事件的 user_id 和 group_id（由 dispatcher 注入）
    current_user_id: int | None = None
    current_group_id: int | None = None
    # 静音控制接口
    mute_control: MuteControl | None = None
    # ConfigManager 引用（用于更新配置）
    config_manager: ConfigManagerLike | None = None
    request_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    logger: Any = field(init=False, repr=False)
    def __post_init__(self) -> None:
        self.logger = _RequestLogger(
            logging.getLogger(f"plugin.{self.plugin_name}"),
            self.request_id,
        )

    def default_groups(self) -> list[int]:
        return self.config.get("default_group_ids", [])

    # ============================================================
    # 静音控制方法
    # ============================================================

    def mute_group(self, group_id: int, duration_minutes: float) -> None:
        """
        让机器人在指定群静音一段时间
        
        Args:
            group_id: 群号
            duration_minutes: 静音时长（分钟）
        """
        if self.mute_control:
            self.mute_control.mute_group(group_id, duration_minutes)

    def unmute_group(self, group_id: int) -> bool:
        """取消群静音"""
        if self.mute_control:
            return self.mute_control.unmute_group(group_id)
        return False

    def is_group_muted(self, group_id: int) -> bool:
        """检查群是否被静音"""
        if self.mute_control:
            return self.mute_control.is_muted(group_id)
        return False

    def get_mute_remaining(self, group_id: int) -> float:
        """获取剩余静音时间（分钟）"""
        if self.mute_control:
            return self.mute_control.get_mute_remaining(group_id)
        return 0

    # ============================================================
    # 会话便捷方法
    # ============================================================

    async def create_session(
        self,
        initial_data: dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> "Session":
        """
        为当前用户创建会话
        
        Args:
            initial_data: 初始会话数据
            timeout: 会话超时时间（秒），默认 5 分钟
        
        Returns:
            新创建的 Session 对象
        """
        if not self.session_manager:
            raise RuntimeError("SessionManager not available")
        if self.current_user_id is None:
            raise RuntimeError("No current user context")
        
        return await self.session_manager.create(
            user_id=self.current_user_id,
            group_id=self.current_group_id,
            plugin_name=self.plugin_name,
            initial_data=initial_data,
            timeout=timeout,
        )

    async def get_session(self) -> "Session | None":
        """
        获取当前用户的会话
        
        Returns:
            Session 对象，如果不存在或已过期则返回 None
        """
        if not self.session_manager:
            return None
        if self.current_user_id is None:
            return None
        
        return await self.session_manager.get(
            user_id=self.current_user_id,
            group_id=self.current_group_id,
        )

    async def end_session(self) -> bool:
        """
        结束当前用户的会话
        
        Returns:
            是否成功删除会话
        """
        if not self.session_manager:
            return False
        if self.current_user_id is None:
            return False
        
        return await self.session_manager.delete(
            user_id=self.current_user_id,
            group_id=self.current_group_id,
        )

    async def has_session(self) -> bool:
        """检查当前用户是否有活跃会话"""
        session = await self.get_session()
        return session is not None
