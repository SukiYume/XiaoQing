"""
XiaoQing 主应用

简化后的核心应用模块。
"""

import asyncio
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import aiohttp

from .app_config_apply import AppConfigApplyMixin
from .app_delivery import AppDeliveryMixin
from .app_identity import AppIdentityService
from .app_ingress import AppIngressMixin
from .app_lifecycle import AppLifecycleMixin
from .app_plugin_context import AppPluginContextMixin
from .app_plugin_watch import AppPluginWatchMixin
from .app_scheduling import AppSchedulingMixin
from .app_support import (
    _PLUGIN_WATCH_RESTART_BASE_DELAY_SECONDS,
    _PLUGIN_WATCH_RESTART_MAX_DELAY_SECONDS,
    _PLUGIN_WATCH_STABLE_RESET_SECONDS,
    ApplicationLifecycleFatalError,
    InboundReconcileError,
    _AppLifecycleState,
    _coerce_runtime_number,
    _ConfigApplyOwner,
    _inbound_credentials,
    _onebot_credentials,
    _run_background_operation,
    _trusted_secrets,
    current_action_sink,
)
from .config import ConfigManager, ConfigSnapshot
from .constants import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_SESSION_TIMEOUT_SEC,
)
from .dispatcher import AdjustableSemaphore, Dispatcher
from .interfaces import (
    PluginPrincipal,
    PluginSettingsSnapshot,
)
from .lifecycle import LazyAsyncLock as _LazyAsyncLock
from .logging_config import LogManager, setup_logging
from .metrics import MetricsCollector
from .onebot import (
    OneBotHttpSender,
    OneBotWsClient,
)
from .plugin_manager import PluginManager
from .router import CommandRouter
from .scheduler import SchedulerManager
from .server import InboundManager
from .session import SessionManager

logger = logging.getLogger(__name__)

__all__ = [
    "ApplicationLifecycleFatalError",
    "InboundReconcileError",
    "XiaoQingApp",
    "_onebot_credentials",
    "current_action_sink",
]


class XiaoQingApp(
    AppLifecycleMixin,
    AppConfigApplyMixin,
    AppPluginContextMixin,
    AppPluginWatchMixin,
    AppDeliveryMixin,
    AppIngressMixin,
    AppSchedulingMixin,
):
    """XiaoQing 主应用类"""

    def __init__(
        self,
        root: Path,
        config_manager: ConfigManager | None = None,
        router: CommandRouter | None = None,
        plugin_manager: PluginManager | None = None,
        dispatcher: Dispatcher | None = None,
        scheduler: SchedulerManager | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.root: Path = root

        # 加载配置
        self.config_manager: ConfigManager = config_manager or ConfigManager(
            root / "config" / "config.json",
            root / "config" / "secrets.json",
        )
        self._plugin_settings_cache: dict[tuple[str, int], PluginSettingsSnapshot] = {}
        self._plugin_settings_cache_revision: int | None = None

        # 初始化日志系统（使用新的日志模块）
        self.log_manager: LogManager = setup_logging(
            self.config_manager.config,
            log_dir=root / "logs",
        )

        # HTTP 会话（共享）
        self.http_session: aiohttp.ClientSession | None = None

        # OneBot 通信
        self.http_sender: OneBotHttpSender | None = None
        self.ws_client: OneBotWsClient | None = None
        self.inbound_manager: InboundManager | None = None
        self.identity_service = AppIdentityService()
        # Kept as an internal alias for existing diagnostics; ownership and
        # replacement semantics live in AppIdentityService.
        self._admin_set = self.identity_service.admin_ids

        # 核心组件
        self.router: CommandRouter = router or CommandRouter()
        self.plugins_dir: Path = root / "plugins"
        poll_interval = _coerce_runtime_number(
            self.config_manager.config.get("plugin_poll_interval", 3600),
            key="plugin_poll_interval",
            default=3600.0,
            integer=False,
            minimum=0.01,
            maximum=86400.0,
        )
        context_factory = self._build_plugin_context
        manager_factory = PluginManager
        configured_data_root = self.config_manager.config.get("data_root")
        plugin_data_root = (
            root / "data" if configured_data_root is None else Path(str(configured_data_root))
        )
        self.plugin_manager: PluginManager = plugin_manager or manager_factory(
            self.plugins_dir,
            self.router,
            context_factory,
            poll_interval=poll_interval,
            data_root=plugin_data_root,
        )
        self._configure_plugin_execution(self.config_manager.config)
        self.scheduler: SchedulerManager = scheduler or SchedulerManager(
            self.config_manager.config.get("timezone", "Asia/Shanghai")
        )
        self.metrics: MetricsCollector = MetricsCollector()

        # 会话管理器（用于多轮对话）
        session_timeout = _coerce_runtime_number(
            self.config_manager.config.get("session_timeout", DEFAULT_SESSION_TIMEOUT_SEC),
            key="session_timeout",
            default=DEFAULT_SESSION_TIMEOUT_SEC,
            integer=False,
            minimum=0.001,
            maximum=604800.0,
        )
        self.session_manager: SessionManager = session_manager or SessionManager(
            default_timeout=session_timeout
        )

        # 消息分发器
        concurrency = _coerce_runtime_number(
            self.config_manager.config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            key="max_concurrency",
            default=DEFAULT_MAX_CONCURRENCY,
            integer=True,
            minimum=1,
            maximum=1024,
        )
        self._dispatcher_concurrency = concurrency
        semaphore = AdjustableSemaphore(concurrency)

        self.dispatcher: Dispatcher = dispatcher or Dispatcher(
            self.router,
            self,
            self.plugin_manager,
            self,
            self.plugin_manager.build_context,
            semaphore,
            self.session_manager,
            self.metrics,
        )

        self._session_cleanup_task: asyncio.Task[None] | None = None
        self._reload_lock = _LazyAsyncLock()
        self._reload_task: asyncio.Task[bool] | None = None
        self._ws_client_task: asyncio.Task[None] | None = None
        self._ws_client_stop_task: asyncio.Task[Any] | None = None
        self._config_watch_task: asyncio.Task[None] | None = None
        self._plugin_watch_task: asyncio.Task[None] | None = None
        self._plugin_watch_tasks: set[asyncio.Task[None]] = set()
        self._plugin_watch_restart_task: asyncio.Task[None] | None = None
        self._plugin_watch_desired = False
        self._plugin_watch_restart_pending = False
        self._plugin_watch_restart_failures = 0
        self._plugin_watch_restart_base_delay_seconds = _PLUGIN_WATCH_RESTART_BASE_DELAY_SECONDS
        self._plugin_watch_restart_max_delay_seconds = _PLUGIN_WATCH_RESTART_MAX_DELAY_SECONDS
        self._plugin_watch_stable_reset_seconds = _PLUGIN_WATCH_STABLE_RESET_SECONDS
        self._config_apply_task: asyncio.Task[None] | None = None
        self._config_apply_tasks: set[asyncio.Task[None]] = set()
        self._config_apply_generation = 0
        self._config_apply_revision = -1
        self._config_apply_owner: _ConfigApplyOwner | None = None
        self._security_generation = 0
        self._security_revision = -1
        self._security_snapshot: ConfigSnapshot | None = None
        self._security_conflict_revision: int | None = None
        initial_snapshot = self.config_manager.snapshot()
        (
            self._runtime_onebot_token,
            self._runtime_onebot_credentials_trusted,
        ) = _onebot_credentials(initial_snapshot)
        self._runtime_inbound_token = _inbound_credentials(initial_snapshot)
        self._load_admins(_trusted_secrets(initial_snapshot))
        self._runtime_inbound_key: tuple[Any, ...] | None = None
        self._onebot_auth_generation = 0
        self._ws_client_auth_generation = 0
        self._ws_client_auth_quarantine: OneBotWsClient | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = _LazyAsyncLock()
        self._inbound_reconcile_lock = _LazyAsyncLock()
        self._inbound_cleanup_pending: list[InboundManager] = []
        self._inbound_cleanup_quarantine: dict[int, BaseException] = {}
        self._runtime_auth_lock = threading.RLock()
        self._inbound_candidates_active: set[InboundManager] = set()
        self._inbound_candidates_lock = threading.RLock()
        self._lifecycle_state = _AppLifecycleState.NEW
        self._stopping = False
        self._defer_plugin_schedule_updates = False
        self._background_task_stop_timeout_seconds = 5.0
        self._last_shutdown_errors: tuple[str, ...] = ()
        self._last_connect_notification_ts: float = 0.0
        self._recent_event_ids: dict[tuple[tuple[str, str], ...], float] = {}
        self._event_dedupe_expirations: list[tuple[float, int, tuple[tuple[str, str], ...]]] = []
        self._event_dedupe_sequence = 0
        self._event_dedupe_lock = _LazyAsyncLock()

        # 注册回调
        self.plugin_manager.on_change(self._reschedule)
        security_updates = getattr(self.config_manager, "on_security_update", None)
        if callable(security_updates):
            security_updates(self._apply_security_snapshot)
        self.config_manager.on_reload(self._apply_config)

    def _unregister_inbound_candidate(self, manager: InboundManager) -> None:
        with self._inbound_candidates_lock:
            self._inbound_candidates_active.discard(manager)

    def _active_inbound_candidates(self) -> tuple[InboundManager, ...]:
        with self._inbound_candidates_lock:
            return tuple(self._inbound_candidates_active)

    # ============================================================
    # 属性代理（供 Dispatcher 使用）
    # ============================================================

    @property
    def config(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self.config_manager.config)

    @property
    def secrets(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self.config_manager.secrets)

    def is_admin(self, user_id: int | None) -> bool:
        return self.identity_service.is_admin(user_id)

    def issue_user_principal(
        self,
        event: dict[str, Any],
        *,
        user_id: int | None,
        group_id: int | None,
        is_private: bool,
    ) -> PluginPrincipal:
        """Mint a user principal from one authenticated OneBot event."""

        return self.identity_service.issue_user_principal(
            event,
            user_id=user_id,
            group_id=group_id,
            is_private=is_private,
        )

    def _load_admins(self, secrets: Mapping[str, Any] | None = None) -> None:
        source = secrets if secrets is not None else self.secrets
        self.identity_service.load_admins(source)

    # ============================================================
    # 生命周期
    # ============================================================

    def _reload_plugins(self) -> asyncio.Task[bool] | None:
        """
        重载所有插件（非阻塞，创建后台任务）

        注意：此方法立即返回，实际重载在后台进行。
        如需等待重载完成，请检查 _reload_task 或监听日志。
        """
        if self._stopping:
            logger.debug("Ignoring plugin reload while stopping")
            return None
        if not bool(getattr(self.plugin_manager, "hot_reload_supported", True)):
            reason = getattr(self.plugin_manager, "hot_reload_unavailable_reason", None)
            logger.error(
                "Plugin reload is unavailable; restart the process to apply plugin changes: %s",
                reason or "module import barrier capability probe failed",
            )
            return None
        if self._reload_task and not self._reload_task.done():
            logger.info("Plugin reload already in progress")
            return self._reload_task
        self._reload_task = asyncio.create_task(
            _run_background_operation(self._reload_plugins_async_with_logging)
        )
        return self._reload_task

    async def _reload_plugins_async_with_logging(self) -> bool:
        """执行插件重载并返回可供管理命令通知用户的最终结果。"""
        try:
            async with self._reload_lock.get():
                if self._stopping:
                    return False
                logger.info("Starting plugin reload...")
                completed = await self.plugin_manager.reload_all_plugins(
                    before_reload=self.session_manager.clear_plugin_sessions,
                )
                if not completed:
                    logger.error("Plugin reload stopped because a generation is quarantined")
                    return False
                logger.info("Plugin reload completed successfully")
                return True
        except Exception as exc:
            logger.exception("Plugin reload failed: %s", exc)
            return False

    # ============================================================
    # 配置热更新
    # ============================================================

    # 配置快照发布与运行时热更新由 AppConfigApplyMixin 提供。
