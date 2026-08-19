"""插件调用上下文与最小权限配置视图。

本模块把核心能力、请求身份、配置快照和会话状态组合成插件可见的稳定边界，
避免插件直接接触应用内部对象或其他插件的配置与密钥。
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiohttp

from .clock import now_in_configured_timezone
from .config import _freeze_config_mapping, materialize_snapshot_value
from .interfaces import (
    ConfigManagerLike,
    MuteControl,
    PluginCapabilities,
    PluginPrincipal,
    PluginSettingsSnapshot,
    SendAction,
)

_PLUGIN_PUBLIC_CONFIG_KEYS = (
    "bot_name",
    "command_prefixes",
    "default_group_ids",
    "timezone",
    "require_bot_name_in_group",
)


@dataclass(frozen=True, slots=True, eq=False)
class _ScopedPluginView(Mapping[str, Any]):
    """Immutable mapping carrying the scope that produced its frozen view."""

    value: Mapping[str, Any]
    plugin_name: str
    kind: Literal["config", "secrets"]

    def __getitem__(self, key: str) -> Any:
        return self.value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _ScopedPluginView):
            other = other.value
        return self.value == other


def _scoped_plugin_config(plugin_name: str, source: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build an immutable view of public settings plus one plugin namespace."""

    if (
        isinstance(source, _ScopedPluginView)
        and source.kind == "config"
        and source.plugin_name == plugin_name
    ):
        return source

    plugins = source.get("plugins", {})
    if not isinstance(plugins, Mapping):
        plugins = {}
    plugin_options = plugins.get(plugin_name, {})
    if not isinstance(plugin_options, Mapping):
        plugin_options = {}
    view = {key: source[key] for key in _PLUGIN_PUBLIC_CONFIG_KEYS if key in source}
    view["plugins"] = {plugin_name: plugin_options}
    return _ScopedPluginView(
        value=_freeze_config_mapping(view),
        plugin_name=plugin_name,
        kind="config",
    )


def _scoped_plugin_secrets(plugin_name: str, source: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build an immutable view containing only one plugin's secret namespace."""

    if (
        isinstance(source, _ScopedPluginView)
        and source.kind == "secrets"
        and source.plugin_name == plugin_name
    ):
        return source

    plugins = source.get("plugins", {})
    if not isinstance(plugins, Mapping):
        plugins = {}
    plugin_secrets = plugins.get(plugin_name, {})
    if not isinstance(plugin_secrets, Mapping):
        plugin_secrets = {}
    return _ScopedPluginView(
        value=_freeze_config_mapping({"plugins": {plugin_name: plugin_secrets}}),
        plugin_name=plugin_name,
        kind="secrets",
    )


if TYPE_CHECKING:
    from .metrics import MetricsCollector
    from .router import CommandCatalogNode, CommandInvocation
    from .session import Session, SessionManager


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
    config: Mapping[str, Any]
    secrets: Mapping[str, Any]
    plugin_name: str
    plugin_dir: Path
    data_dir: Path
    http_session: aiohttp.ClientSession | None
    send_action: SendAction
    reload_config: Callable[[], Any]
    reload_plugins: Callable[[], Any]
    get_command_catalog: Callable[[], tuple["CommandCatalogNode", ...]]
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
    settings_reader: Callable[[], PluginSettingsSnapshot] | None = field(
        default=None,
        repr=False,
    )
    secret_reader: Callable[[str], Any] | None = field(default=None, repr=False)
    secret_writer: Callable[[str, Any], None] | None = field(default=None, repr=False)
    secret_deleter: Callable[[str], bool] | None = field(default=None, repr=False)
    principal: PluginPrincipal = field(
        default_factory=lambda: PluginPrincipal(kind="lifecycle"),
    )
    capabilities: PluginCapabilities = field(default_factory=PluginCapabilities)
    request_id: str | None = None
    command_invocation: "CommandInvocation | None" = None
    state: dict[str, Any] = field(default_factory=dict)
    logger: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Enforce the least-privilege view even for extension/test factories that
        # construct PluginContext directly instead of going through XiaoQingApp.
        self.config = _scoped_plugin_config(self.plugin_name, self.config)
        self.secrets = _scoped_plugin_secrets(self.plugin_name, self.secrets)
        self.logger = _RequestLogger(
            logging.getLogger(f"plugin.{self.plugin_name}"),
            self.request_id,
        )

    def default_groups(self) -> list[int]:
        """返回当前调用应使用的群发目标。

        定时任务的目标已经由 Core 根据 ``schedule.group_ids`` 解析完成：字段存在时
        使用清单值，字段缺失时使用全局 ``default_group_ids``。这里必须保留解析后的
        结果（包括显式空列表），让需要自行确认投递结果的插件与 Core 的统一发送路径
        遵循同一份 Schedule Manifest。
        """

        if self.principal.kind == "scheduled_system":
            return [
                target.target_id
                for target in self.principal.delivery_targets
                if target.kind == "group"
            ]
        groups = self.config.get("default_group_ids", ())
        if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes, bytearray)):
            return []
        return list(groups)

    def is_global_admin(self, user_id: int | None = None) -> bool:
        """核验当前操作者，不向插件暴露全局管理员名单。"""

        actor = self.current_user_id if user_id is None else user_id
        if type(actor) is not int or type(self.principal.user_id) is not int:
            return False
        return actor == self.principal.user_id and self.capabilities.is_bot_admin

    def get_secret(self, path: str) -> Any:
        if self.secret_reader is not None:
            return self.secret_reader(path)
        # 与 get_config() 一样只消费一份原子 settings 快照。直接构造的测试/
        # 扩展上下文通常没有 secret_reader，但仍可能有可热更新的 settings_reader；
        # 从 self.secrets 读取会把这条兼容路径固定在旧代。
        settings = self.get_settings_snapshot()
        current: Any = settings.secrets.get("plugins", {}).get(self.plugin_name, {})
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return materialize_snapshot_value(current)

    def get_settings_snapshot(self) -> PluginSettingsSnapshot:
        """Return one atomic generation scoped to this plugin."""
        if self.settings_reader is not None:
            return self.settings_reader()
        return PluginSettingsSnapshot(
            config=_freeze_config_mapping(self.config),
            secrets=_freeze_config_mapping(self.secrets),
            revision=0,
        )

    def now(self):
        """Return the current time in the application's configured timezone."""

        return now_in_configured_timezone(self)

    def get_config(self, path: str) -> Any:
        """Read one detached value from the current plugin config namespace."""
        settings = self.get_settings_snapshot()
        current: Any = settings.config.get("plugins", {}).get(self.plugin_name, {})
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return materialize_snapshot_value(current)

    def set_secret(self, path: str, value: Any) -> None:
        if self.secret_writer is None:
            raise RuntimeError("plugin secret writer unavailable")
        self.secret_writer(path, value)

    def delete_secret(self, path: str) -> bool:
        if self.secret_deleter is None:
            raise RuntimeError("plugin secret deleter unavailable")
        return self.secret_deleter(path)

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
            return bool(self.mute_control.unmute_group(group_id))
        return False

    def is_group_muted(self, group_id: int) -> bool:
        """检查群是否被静音"""
        if self.mute_control:
            return bool(self.mute_control.is_muted(group_id))
        return False

    def get_mute_remaining(self, group_id: int) -> float:
        """获取剩余静音时间（分钟）"""
        if self.mute_control:
            return float(self.mute_control.get_mute_remaining(group_id))
        return 0

    # ============================================================
    # 会话便捷方法
    # ============================================================

    async def create_session(
        self,
        initial_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> "Session":
        """
        为当前用户创建会话

        Args:
            initial_data: 初始会话数据
            timeout: 会话超时时间（秒），默认使用 SessionManager 默认值

        Returns:
            新创建会话的隔离快照；修改该对象不会回写存储。
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
        获取当前用户会话的隔离快照，并刷新空闲超时。

        Returns:
            Session 快照，如果不存在或已过期则返回 None。持久修改必须使用
            :meth:`update_session`。
        """
        if not self.session_manager:
            return None
        if self.current_user_id is None:
            return None

        return await self.session_manager.get(
            user_id=self.current_user_id,
            group_id=self.current_group_id,
        )

    async def update_session(self, callback: Callable[["Session"], Any]) -> Any:
        """Atomically update the current user's session through a callback.

        The callback receives a private working copy and may be async.  Only a
        successful return is committed; exceptions and cancellation roll back
        both data and metadata.  Snapshots returned by :meth:`get_session` are
        read-only in the persistence sense and cannot be used to write back.
        """
        if not self.session_manager:
            return None
        if self.current_user_id is None:
            return None
        return await self.session_manager.update(
            user_id=self.current_user_id,
            group_id=self.current_group_id,
            callback=callback,
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

        return bool(
            await self.session_manager.delete(
                user_id=self.current_user_id,
                group_id=self.current_group_id,
            )
        )

    async def has_session(self) -> bool:
        """检查当前用户是否有活跃会话，不刷新空闲超时。"""
        if not self.session_manager or self.current_user_id is None:
            return False
        return bool(
            await self.session_manager.exists(
                user_id=self.current_user_id,
                group_id=self.current_group_id,
            )
        )
