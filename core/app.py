"""
XiaoQing 主应用

简化后的核心应用模块。
"""

import asyncio
import functools
import inspect
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast

import aiohttp

from .ai import (
    AICompletionResult,
    AIModelInfo,
    complete_configured_route,
    list_configured_models,
)
from .app_delivery import AppDeliveryMixin
from .app_identity import AppIdentityService
from .app_ingress import AppIngressMixin
from .app_plugin_watch import AppPluginWatchMixin
from .app_scheduling import AppSchedulingMixin
from .app_support import (
    _PLUGIN_WATCH_RESTART_BASE_DELAY_SECONDS,
    _PLUGIN_WATCH_RESTART_MAX_DELAY_SECONDS,
    _PLUGIN_WATCH_STABLE_RESET_SECONDS,
    _STARTUP_OWNERSHIP_MAX_ATTEMPTS,
    _STARTUP_OWNERSHIP_RETRY_BASE_DELAY_SECONDS,
    _STARTUP_OWNERSHIP_RETRY_MAX_DELAY_SECONDS,
    _STARTUP_OWNERSHIP_TIMEOUT_SECONDS,
    ApplicationLifecycleFatalError,
    InboundReconcileError,
    _AppLifecycleState,
    _coerce_runtime_number,
    _ConfigApplyOwner,
    _inbound_credentials,
    _onebot_credentials,
    _require_onebot_holder_credentials,
    _run_background_operation,
    _trusted_secrets,
    current_action_sink,
)
from .capabilities import (
    AIService,
    ChatReplyService,
    CodexArxivSummaryService,
    ConfigSubscriptionService,
    OneBotMediaService,
    SecretAdminService,
    VoiceSynthesisService,
)
from .config import ConfigManager, ConfigSnapshot, ConfigSourceStatus
from .constants import (
    DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_SESSION_TIMEOUT_SEC,
)
from .context import PluginContext, _scoped_plugin_config, _scoped_plugin_secrets
from .dispatcher import AdjustableSemaphore, Dispatcher
from .interfaces import (
    PluginCapabilities,
    PluginPrincipal,
    PluginSettingsSnapshot,
)
from .lifecycle import (
    DeferredCancellation as _DeferredCancellation,
)
from .lifecycle import LazyAsyncLock as _LazyAsyncLock
from .lifecycle import (
    OwnedTaskFatalError as _OwnedTaskFatalError,
)
from .lifecycle import (
    await_owned_task as _await_owned_task,
)
from .lifecycle import (
    run_owned_operation as _run_owned_operation,
)
from .logging_config import LogManager, setup_logging
from .metrics import MetricsCollector
from .onebot import (
    OneBotHttpSender,
    OneBotWsClient,
)
from .plugin_execution import (
    call_plugin_callback,
    invoke_loaded_plugin,
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
        self._reload_task: asyncio.Task[None] | None = None
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

    def _latest_startup_snapshot(self) -> ConfigSnapshot:
        """Return the newest security-published snapshot as one atomic pair."""

        manager_snapshot = self.config_manager.snapshot()
        with self._runtime_auth_lock:
            security_snapshot = self._security_snapshot
            if security_snapshot is None or manager_snapshot.revision > security_snapshot.revision:
                self._apply_security_snapshot_locked(manager_snapshot)
                return manager_snapshot
            return security_snapshot

    def _claim_or_reuse_startup_owner(
        self,
        snapshot: ConfigSnapshot,
    ) -> _ConfigApplyOwner | None:
        owner = self._claim_config_apply_owner(snapshot)
        if owner is not None:
            return owner
        with self._runtime_auth_lock:
            owner = self._config_apply_owner
            if (
                owner is not None
                and not self._stopping
                and owner.revision == snapshot.revision
                and owner.security_generation == self._security_generation
                and owner.generation == self._config_apply_generation
            ):
                return owner
        return None

    async def _wait_for_startup_ownership_retry(
        self,
        *,
        attempt: int,
        started_at: float,
        reason: str,
    ) -> None:
        """Apply bounded backoff or fail startup when ownership never stabilizes."""

        elapsed = max(0.0, time.monotonic() - started_at)
        if (
            attempt >= _STARTUP_OWNERSHIP_MAX_ATTEMPTS
            or elapsed >= _STARTUP_OWNERSHIP_TIMEOUT_SECONDS
        ):
            logger.error(
                "Application startup ownership did not stabilize after %d attempt(s) "
                "and %.3fs; last_reason=%s",
                attempt,
                elapsed,
                reason,
            )
            raise RuntimeError("startup authentication ownership did not stabilize")

        exponent = min(max(0, attempt - 1), 16)
        delay = min(
            _STARTUP_OWNERSHIP_RETRY_MAX_DELAY_SECONDS,
            _STARTUP_OWNERSHIP_RETRY_BASE_DELAY_SECONDS * (2**exponent),
        )
        await asyncio.sleep(max(0.0, delay))

    async def start(self) -> None:
        """Start one runtime generation, rolling it back completely on failure."""
        async with self._lifecycle_lock.get():
            if self._stopping:
                raise RuntimeError(
                    f"Cannot start application in lifecycle state {self._lifecycle_state.value}"
                )
            if self._lifecycle_state is _AppLifecycleState.RUNNING:
                return
            if self._lifecycle_state in {
                _AppLifecycleState.STOPPING,
                _AppLifecycleState.STOPPED,
                _AppLifecycleState.FAILED,
            }:
                raise RuntimeError(
                    f"Cannot start application in lifecycle state {self._lifecycle_state.value}"
                )
            if self._lifecycle_state is not _AppLifecycleState.NEW:
                raise RuntimeError("Application start is already in progress")

            self._lifecycle_state = _AppLifecycleState.STARTING
            self._last_shutdown_errors = ()
            try:
                await self._start_runtime()
            except BaseException as start_error:
                self._stopping = True
                rollback_errors: list[str] = []
                deferred_cancellation = _DeferredCancellation()
                cleanup_task = asyncio.create_task(
                    _run_owned_operation(lambda: self._cleanup_runtime(rollback_errors))
                )
                try:
                    await _await_owned_task(cleanup_task, deferred_cancellation)
                except BaseException as cleanup_exc:
                    rollback_errors.append(
                        f"rollback task: {type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
                    logger.exception("Application startup rollback failed", exc_info=cleanup_exc)
                self._last_shutdown_errors = tuple(rollback_errors)
                if rollback_errors:
                    self._lifecycle_state = _AppLifecycleState.FAILED
                    logger.error(
                        "Application startup rollback left %d cleanup error(s): %s",
                        len(rollback_errors),
                        "; ".join(rollback_errors),
                    )
                else:
                    self._lifecycle_state = _AppLifecycleState.NEW
                    self._stopping = self._shutdown_task is not None
                    # A clean rollback owns no runtime side effects.  Release
                    # its startup-only revision claim so a same-revision
                    # pre-start apply/retry can run, while keeping generation
                    # counters and the fail-closed security revision monotonic.
                    with self._runtime_auth_lock:
                        self._config_apply_owner = None
                        self._config_apply_revision = -1
                if isinstance(start_error, asyncio.CancelledError):
                    raise
                deferred_cancellation.raise_if_requested(cause=start_error)
                if isinstance(start_error, Exception):
                    raise
                raise ApplicationLifecycleFatalError(start_error) from None

            self._lifecycle_state = _AppLifecycleState.RUNNING

    async def _start_runtime(self) -> None:
        startup_snapshot = self._latest_startup_snapshot()
        startup_config = startup_snapshot.config

        # 注入的测试 dispatcher 可以没有 limiter；生产 limiter 始终原地调容。
        concurrency = _coerce_runtime_number(
            startup_config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            key="max_concurrency",
            default=DEFAULT_MAX_CONCURRENCY,
            integer=True,
            minimum=1,
            maximum=1024,
        )
        session_timeout = _coerce_runtime_number(
            startup_config.get("session_timeout", DEFAULT_SESSION_TIMEOUT_SEC),
            key="session_timeout",
            default=DEFAULT_SESSION_TIMEOUT_SEC,
            integer=False,
            minimum=0.001,
            maximum=604800.0,
        )
        if self.dispatcher.semaphore is None:
            self.dispatcher.semaphore = AdjustableSemaphore(concurrency)
        elif isinstance(self.dispatcher.semaphore, AdjustableSemaphore):
            self.dispatcher.semaphore.resize(concurrency)
        self.session_manager.set_default_timeout(session_timeout)

        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=DEFAULT_HTTP_TIMEOUT_SECONDS,
                connect=DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS,
            )
        )

        self._defer_plugin_schedule_updates = True
        try:
            self.plugin_manager.load_all()
            await self.plugin_manager.wait_inits()
        finally:
            self._defer_plugin_schedule_updates = False
        self.scheduler.ensure_started()
        self._reschedule("startup")

        self._session_cleanup_task = asyncio.create_task(
            _run_background_operation(self._cleanup_sessions_loop)
        )

        # Build every authentication holder from one immutable snapshot.  A
        # security publication can run from another thread (or re-enter a
        # patched factory in tests), so retry until one owner survives every
        # await and publication boundary.
        ownership_started_at = time.monotonic()
        ownership_attempt = 0
        while not self._stopping:
            ownership_attempt += 1
            startup_snapshot = self._latest_startup_snapshot()
            owner = self._claim_or_reuse_startup_owner(startup_snapshot)
            if owner is None:
                await self._wait_for_startup_ownership_retry(
                    attempt=ownership_attempt,
                    started_at=ownership_started_at,
                    reason="claim_rejected",
                )
                continue
            config = startup_snapshot.config
            secrets = _trusted_secrets(startup_snapshot)
            onebot_token, onebot_credentials_trusted = _onebot_credentials(startup_snapshot)
            http_base = str(config.get("onebot_http_base", "") or "").strip()

            ownership_lost_reason = ""
            with self._runtime_auth_lock:
                if not self._owns_config_apply_locked(owner):
                    ownership_lost_reason = "before_http_publish"
                elif http_base:
                    sender = OneBotHttpSender(
                        http_base,
                        onebot_token,
                        self.http_session,
                        credentials_trusted=onebot_credentials_trusted,
                    )
                    if not self._owns_config_apply_locked(owner):
                        sender.update(http_base, "", credentials_trusted=False)
                        ownership_lost_reason = "during_http_publish"
                    else:
                        self.http_sender = sender
                else:
                    self.http_sender = None
                    ownership_lost_reason = ""

            if ownership_lost_reason:
                await self._wait_for_startup_ownership_retry(
                    attempt=ownership_attempt,
                    started_at=ownership_started_at,
                    reason=ownership_lost_reason,
                )
                continue

            await self._reconcile_ws_client(
                enable_ws=bool(config.get("enable_ws_client", True)),
                ws_uri=str(config.get("onebot_ws_uri", "") or "").strip(),
                token=onebot_token,
                credentials_trusted=onebot_credentials_trusted,
                queue_size=self._parse_ws_queue_size(config),
                owner=owner,
            )
            if not self._owns_config_apply(owner):
                await self._wait_for_startup_ownership_retry(
                    attempt=ownership_attempt,
                    started_at=ownership_started_at,
                    reason="after_ws_reconcile",
                )
                continue
            await self._reconcile_inbound_manager(config, secrets, owner=owner)
            if self._owns_config_apply(owner):
                break
            await self._wait_for_startup_ownership_retry(
                attempt=ownership_attempt,
                started_at=ownership_started_at,
                reason="after_inbound_reconcile",
            )

        if self._stopping:
            return

        # Start the watcher only after all startup auth holders are safely
        # published; it can no longer overtake provisional startup objects.
        self._config_watch_task = asyncio.create_task(
            _run_background_operation(self.config_manager.watch)
        )
        poll_interval = _coerce_runtime_number(
            startup_snapshot.config.get("plugin_poll_interval", 3600),
            key="plugin_poll_interval",
            default=3600.0,
            integer=False,
            minimum=0.01,
            maximum=86400.0,
        )
        self._configure_plugin_watch(
            startup_snapshot.config,
            poll_interval=float(poll_interval),
        )

    async def stop(self) -> None:
        """优雅停止应用，并让并发调用共享同一次关停。"""
        task = self._shutdown_task
        if task is None or (
            task.done() and self._lifecycle_state is not _AppLifecycleState.STOPPED
        ):
            # 先冻结所有会重建运行时组件的入口，再异步执行逐阶段清理。
            self._stopping = True
            task = asyncio.create_task(_run_owned_operation(self._stop_async))
            self._shutdown_task = task

        if task is asyncio.current_task():
            return
        try:
            await asyncio.shield(task)
        finally:
            if (
                task.done()
                and self._shutdown_task is task
                and self._lifecycle_state is not _AppLifecycleState.STOPPED
            ):
                self._shutdown_task = None

    async def _stop_async(self) -> None:
        """Enter the terminal stopped state after converging every owned resource."""
        async with self._lifecycle_lock.get():
            if self._lifecycle_state is _AppLifecycleState.STOPPED:
                return
            self._lifecycle_state = _AppLifecycleState.STOPPING
            logger.info("Shutting down XiaoQing...")
            errors: list[str] = []
            await self._cleanup_runtime(errors)

            self._last_shutdown_errors = tuple(errors)
            if errors:
                self._lifecycle_state = _AppLifecycleState.FAILED
                logger.warning(
                    "XiaoQing shutdown remains incomplete with %d cleanup error(s): %s",
                    len(errors),
                    "; ".join(errors),
                )
            else:
                self._lifecycle_state = _AppLifecycleState.STOPPED
                logger.info("XiaoQing shutdown complete")

    async def _cleanup_runtime(self, errors: list[str]) -> None:
        """Release resources in dependency order and retain blocked downstream owners."""
        # 1. Freeze and converge every control-plane task before touching the
        # runtime objects it may still reconcile, reload or reschedule.
        await self._run_shutdown_step(
            "background config apply tasks",
            self._cancel_config_apply_tasks,
            errors,
        )
        await self._run_shutdown_step(
            "background plugin watch tasks",
            self._cancel_plugin_watch_tasks,
            errors,
        )
        for attr_name in (
            "_config_watch_task",
            "_reload_task",
            "_session_cleanup_task",
        ):
            await self._run_shutdown_step(
                f"background task {attr_name}",
                functools.partial(self._cancel_task, attr_name),
                errors,
            )

        live_control_tasks = self._live_control_plane_tasks()
        if live_control_tasks:
            message = (
                "runtime cleanup deferred while control-plane tasks are still running: "
                + ", ".join(live_control_tasks)
            )
            errors.append(message)
            logger.warning(message)
            return

        # 2. Stop every ingress path and its in-flight work before removing
        # scheduler/plugin dependencies used by event callbacks.
        inbound_stopped = await self._run_shutdown_step(
            "inbound server",
            self._stop_inbound_manager,
            errors,
        )
        ws_client_stopped = await self._run_shutdown_step(
            "WebSocket client",
            self._stop_ws_client,
            errors,
        )
        ws_listener_stopped = await self._run_shutdown_step(
            "WebSocket listener task",
            lambda: self._cancel_task("_ws_client_task"),
            errors,
        )
        if not all((inbound_stopped, ws_client_stopped, ws_listener_stopped)):
            message = "scheduler, plugin and HTTP cleanup deferred until ingress shutdown converges"
            errors.append(message)
            logger.warning(message)
            return

        # 3. 先停止调度，之后再卸载可能被任务引用的插件。若旧任务仍在
        # 收敛，必须保留插件和共享连接供它完成清理，下一次 stop 再继续。
        scheduler_stopped = await self._run_shutdown_step(
            "scheduler",
            self._stop_scheduler,
            errors,
        )
        if not scheduler_stopped:
            message = "plugin and HTTP cleanup deferred until scheduler shutdown converges"
            errors.append(message)
            logger.warning(message)
            return

        plugins_unloaded = await self._unload_plugins_for_shutdown(errors)
        if not plugins_unloaded:
            message = "HTTP cleanup deferred until plugin shutdown converges"
            errors.append(message)
            logger.warning(message)
            return

        # 4. 最后释放共享连接。引用先清空，防止并发回调继续复用半关闭对象。
        await self._run_shutdown_step("HTTP session", self._close_http_session, errors)

    def _live_control_plane_tasks(self) -> tuple[str, ...]:
        """Return control tasks that still own mutable runtime dependencies."""
        live: list[str] = []
        config_tasks = set(self._config_apply_tasks)
        if self._config_apply_task is not None:
            config_tasks.add(self._config_apply_task)
        if any(not task.done() for task in config_tasks):
            live.append("config apply")

        plugin_watch_tasks = set(self._plugin_watch_tasks)
        if self._plugin_watch_task is not None:
            plugin_watch_tasks.add(self._plugin_watch_task)
        if any(not task.done() for task in plugin_watch_tasks):
            live.append("plugin watcher")
        restart_task = self._plugin_watch_restart_task
        if restart_task is not None and not restart_task.done():
            live.append("plugin watcher restart")

        for attr_name, label in (
            ("_config_watch_task", "config watcher"),
            ("_reload_task", "plugin reload"),
            ("_session_cleanup_task", "session maintenance"),
        ):
            task = getattr(self, attr_name, None)
            if task is not None and not task.done():
                live.append(label)
        return tuple(live)

    async def _run_shutdown_step(
        self,
        name: str,
        operation: Callable[[], Awaitable[None]],
        errors: list[str],
    ) -> bool:
        """运行一个关停阶段，并把错误记入汇总而不是中断关停。"""
        try:
            await operation()
        except BaseException as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            logger.exception("Shutdown step %s failed", name)
            return False
        return True

    async def _stop_inbound_manager(self) -> None:
        lock = self._inbound_reconcile_lock.get()
        try:
            await asyncio.wait_for(
                lock.acquire(),
                timeout=self._background_task_stop_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("timed out waiting for inbound reconciliation to finish") from exc
        try:
            current = self.inbound_manager
            managers: list[InboundManager] = []
            for manager in (current, *self._inbound_cleanup_pending):
                if manager is not None and all(manager is not item for item in managers):
                    managers.append(manager)
            failed: list[tuple[InboundManager, BaseException]] = []
            for manager in managers:
                quarantined_error = self._inbound_cleanup_quarantine.pop(
                    id(manager),
                    None,
                )
                if quarantined_error is not None:
                    # The candidate already failed one cleanup attempt in the
                    # operation that triggered this rollback.  Preserve that
                    # failure and ownership; a later explicit stop retries it.
                    failed.append((manager, quarantined_error))
                    continue
                try:
                    await manager.stop()
                except BaseException as exc:
                    failed.append((manager, exc))
                else:
                    self._inbound_cleanup_quarantine.pop(id(manager), None)
            failed_managers = [manager for manager, _ in failed]
            self._inbound_cleanup_pending = [
                manager for manager in failed_managers if current is None or manager is not current
            ]
            if current is not None and any(current is manager for manager in failed_managers):
                self.inbound_manager = current
            else:
                self.inbound_manager = None
            if failed:
                summary = "; ".join(f"{type(exc).__name__}: {exc}" for _, exc in failed)
                raise RuntimeError(f"inbound cleanup failed: {summary}") from failed[0][1]
            if managers:
                logger.info("Inbound server stopped")
        finally:
            lock.release()

    async def _stop_ws_client(self, *, owner: _ConfigApplyOwner | None = None) -> None:
        if not self._owns_config_apply(owner):
            return
        client = self.ws_client
        if client is None:
            if self._owns_config_apply(owner):
                self._ws_client_stop_task = None
            return

        stop_task = self._ws_client_stop_task
        if stop_task is not None and stop_task.done():
            try:
                stop_task.result()
            except asyncio.CancelledError:
                pass
            except _OwnedTaskFatalError as exc:
                self._ws_client_stop_task = None
                self.ws_client = client
                raise ApplicationLifecycleFatalError(exc.original) from None
            except BaseException as exc:
                logger.warning("Earlier WebSocket client stop attempt failed: %s", exc)
            else:
                if not self._owns_config_apply(owner):
                    return
                self._ws_client_stop_task = None
                if self.ws_client is client:
                    self.ws_client = None
                if self._ws_client_auth_quarantine is client:
                    self._ws_client_auth_quarantine = None
                logger.info("WebSocket client stopped")
                return
            self._ws_client_stop_task = None
            stop_task = None

        if stop_task is None:
            if not self._owns_config_apply(owner):
                return
            stop_task = asyncio.create_task(_run_owned_operation(client.stop))
            self._ws_client_stop_task = stop_task
        stop_timeout = self._background_task_stop_timeout_seconds
        client_timeout = getattr(client, "_shutdown_timeout_seconds", None)
        if isinstance(client_timeout, (int, float)) and not isinstance(client_timeout, bool):
            # The client owns one absolute internal deadline.  Give its
            # bounded cleanup a small scheduling margin rather than cancelling
            # it at the exact instant it is about to report convergence.
            stop_timeout = max(stop_timeout, max(0.0, float(client_timeout)) + 0.25)
        _done, pending = await asyncio.wait(
            {stop_task},
            timeout=stop_timeout,
        )
        with self._runtime_auth_lock:
            if not self._owns_config_apply_locked(owner):
                return
            if pending:
                stop_task.cancel()
                self.ws_client = client
                raise RuntimeError(f"WebSocket client stop exceeded {stop_timeout:.3f}s")
            try:
                stop_task.result()
            except _OwnedTaskFatalError as exc:
                self._ws_client_stop_task = None
                self.ws_client = client
                raise ApplicationLifecycleFatalError(exc.original) from None
            except BaseException:
                self._ws_client_stop_task = None
                self.ws_client = client
                raise
            else:
                self._ws_client_stop_task = None
                if self.ws_client is client:
                    self.ws_client = None
                if self._ws_client_auth_quarantine is client:
                    self._ws_client_auth_quarantine = None
                logger.info("WebSocket client stopped")

    async def _stop_scheduler(self) -> None:
        await self.scheduler.shutdown_async(wait=True)
        logger.info("Scheduler stopped")

    async def _unload_plugins_for_shutdown(self, errors: list[str]) -> bool:
        budget = self._plugin_shutdown_budget_seconds()
        deadline = time.monotonic() + budget
        try:
            plugin_names = self.plugin_manager.list_runtime_plugins()
        except BaseException as exc:
            errors.append(f"plugin list: {type(exc).__name__}: {exc}")
            logger.exception("Could not enumerate plugins during shutdown")
            return False

        for index, name in enumerate(plugin_names):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                message = (
                    f"plugin shutdown budget exhausted after {budget:.3f}s; "
                    f"remaining plugins: {', '.join(plugin_names[index:])}"
                )
                errors.append(message)
                logger.warning(message)
                break
            await self._run_shutdown_step(
                f"plugin {name}",
                functools.partial(
                    self._unload_plugin_with_budget,
                    name,
                    remaining,
                ),
                errors,
            )
        try:
            remaining_plugins = self.plugin_manager.list_runtime_plugins()
        except BaseException as exc:
            errors.append(f"plugin post-drain list: {type(exc).__name__}: {exc}")
            logger.exception("Could not enumerate plugins after shutdown")
            return False
        if remaining_plugins:
            message = "plugin drain incomplete; quarantined callbacks still running: " + ", ".join(
                remaining_plugins
            )
            errors.append(message)
            logger.warning(message)
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = f"plugin sync broker close exceeded shared {budget:.3f}s budget"
            errors.append(message)
            logger.warning(message)
            return False
        broker_closed = await self._run_shutdown_step(
            "plugin sync broker",
            lambda: self._close_plugin_execution_broker(remaining),
            errors,
        )
        if not broker_closed:
            return False
        logger.info("All plugins unloaded and sync broker drained (%d total)", len(plugin_names))
        return True

    def _plugin_shutdown_budget_seconds(self) -> float:
        raw_timeout = getattr(
            self.plugin_manager,
            "execution_drain_timeout_seconds",
            self._background_task_stop_timeout_seconds,
        )
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = self._background_task_stop_timeout_seconds
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = self._background_task_stop_timeout_seconds
        return timeout

    async def _unload_plugin_with_budget(self, name: str, remaining: float) -> None:
        unload = self.plugin_manager.unload_plugin
        try:
            parameters = inspect.signature(unload).parameters.values()
        except (TypeError, ValueError):
            supports_budget = False
        else:
            supports_budget = any(
                parameter.name == "drain_timeout_seconds" for parameter in parameters
            )
        operation = (
            unload(name, drain_timeout_seconds=remaining) if supports_budget else unload(name)
        )
        await asyncio.wait_for(operation, timeout=remaining)

    async def _close_plugin_execution_broker(self, remaining: float) -> None:
        close_manager = getattr(self.plugin_manager, "close", None)
        close_broker = getattr(self.plugin_manager, "close_execution_broker", None)
        close_operation = close_manager if callable(close_manager) else close_broker
        if not callable(close_operation):
            return
        result = await asyncio.wait_for(
            close_operation(timeout_seconds=remaining),
            timeout=remaining,
        )
        if not result.drained:
            pending = getattr(
                result,
                "pending_callbacks",
                getattr(result, "pending_sync_callbacks", "unknown"),
            )
            raise RuntimeError(f"sync broker drain incomplete; pending={pending}")

    async def _close_http_session(self) -> None:
        session = self.http_session
        if session:
            try:
                await session.close()
            except BaseException:
                self.http_session = session
                raise
            self.http_session = None
            self.http_sender = None
            logger.info("HTTP session closed")
        else:
            self.http_sender = None

    async def _cancel_config_apply_tasks(self) -> None:
        tasks = set(self._config_apply_tasks)
        current = self._config_apply_task
        if current is not None:
            tasks.add(current)
        if not tasks:
            self._config_apply_task = None
            return

        for task in tasks:
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait(
            tasks,
            timeout=self._background_task_stop_timeout_seconds,
        )
        failures: list[BaseException] = []
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                failures.append(exc)

        self._config_apply_tasks = {task for task in self._config_apply_tasks if not task.done()}
        self._config_apply_tasks.update(pending)
        if pending:
            if self._config_apply_task not in pending:
                self._config_apply_task = next(iter(pending))
            details = ""
            if failures:
                details = "; completed failures: " + "; ".join(
                    f"{type(exc).__name__}: {exc}" for exc in failures
                )
            raise RuntimeError(
                f"{len(pending)} runtime configuration task(s) ignored cancellation within "
                f"{self._background_task_stop_timeout_seconds:.3f}s{details}"
            )

        self._config_apply_tasks.clear()
        if self._config_apply_task in tasks:
            self._config_apply_task = None
        if failures:
            summary = "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
            raise RuntimeError(f"runtime configuration task(s) failed: {summary}") from failures[0]

    async def _cancel_plugin_watch_tasks(self) -> None:
        self._plugin_watch_desired = False
        self._plugin_watch_restart_pending = False
        self._plugin_watch_restart_failures = 0
        tasks = set(self._plugin_watch_tasks)
        current = self._plugin_watch_task
        if current is not None:
            tasks.add(current)
        restart_task = self._plugin_watch_restart_task
        if restart_task is not None:
            tasks.add(restart_task)
        if not tasks:
            self._plugin_watch_task = None
            self._plugin_watch_restart_task = None
            return

        for task in tasks:
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait(
            tasks,
            timeout=self._background_task_stop_timeout_seconds,
        )
        failures: list[BaseException] = []
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                failures.append(exc)

        pending_watchers = pending - ({restart_task} if restart_task is not None else set())
        self._plugin_watch_tasks = {task for task in self._plugin_watch_tasks if not task.done()}
        self._plugin_watch_tasks.update(pending_watchers)
        if pending:
            if pending_watchers and self._plugin_watch_task not in pending_watchers:
                self._plugin_watch_task = next(iter(pending_watchers))
            if restart_task in pending:
                self._plugin_watch_restart_task = restart_task
            raise RuntimeError(
                f"{len(pending)} plugin watcher task(s) ignored cancellation within "
                f"{self._background_task_stop_timeout_seconds:.3f}s"
            )

        self._plugin_watch_tasks.clear()
        if self._plugin_watch_task in tasks:
            self._plugin_watch_task = None
        if self._plugin_watch_restart_task in tasks:
            self._plugin_watch_restart_task = None
        if failures:
            summary = "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
            raise RuntimeError(f"plugin watcher task(s) failed: {summary}") from failures[0]

    async def _cancel_task(self, attr_name: str) -> None:
        task = getattr(self, attr_name, None)
        if not task:
            return
        if task is asyncio.current_task():
            logger.warning("Skipped cancelling current task %s during shutdown", attr_name)
            raise RuntimeError(f"cannot synchronously stop current task {attr_name}")
        task.cancel()
        _done, pending = await asyncio.wait(
            {task},
            timeout=self._background_task_stop_timeout_seconds,
        )
        if pending:
            raise RuntimeError(
                f"task {attr_name} did not finish after cancellation within "
                f"{self._background_task_stop_timeout_seconds:.3f}s"
            )
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        finally:
            if getattr(self, attr_name, None) is task:
                setattr(self, attr_name, None)

    async def _cleanup_sessions_loop(self) -> None:
        while True:
            try:
                await self.session_manager.cleanup_expired()
            except Exception as exc:
                logger.warning("Session cleanup failed: %s", exc)
            await asyncio.sleep(60)

    # ============================================================
    # 事件处理
    # ============================================================

    # ============================================================
    # 插件上下文构建
    # ============================================================

    def _build_plugin_context(
        self,
        plugin_name: str,
        plugin_dir: Path,
        data_dir: Path,
        state: dict[str, Any],
        user_id: int | None = None,
        group_id: int | None = None,
        request_id: str | None = None,
        principal: PluginPrincipal | None = None,
        declared_capabilities: frozenset[str] = frozenset(),
        uses_services: frozenset[str] = frozenset(),
    ) -> Any:
        """构建插件上下文"""
        if principal is None:
            if user_id is not None:
                principal = self.issue_user_principal(
                    {},
                    user_id=user_id,
                    group_id=group_id,
                    is_private=group_id is None,
                )
            else:
                principal = self.identity_service.issue(
                    kind="lifecycle",
                    group_id=group_id,
                )
        elif not self.identity_service.owns(principal):
            raise PermissionError("plugin context principal was not issued by this application")
        if principal.kind == "user":
            try:
                context_user_id = int(user_id) if user_id is not None else None
                principal_user_id = (
                    int(principal.user_id) if principal.user_id is not None else None
                )
                context_group_id = int(group_id) if group_id is not None else None
                principal_group_id = (
                    int(principal.group_id) if principal.group_id is not None else None
                )
            except (TypeError, ValueError) as exc:
                raise PermissionError("invalid user principal identifiers") from exc
            if (context_user_id, context_group_id) != (
                principal_user_id,
                principal_group_id,
            ):
                raise PermissionError("plugin context identifiers do not match its principal")

        async def send_action(action: dict[str, Any]) -> bool | None:
            return await self._send_action(
                self._tag_action_source(action, plugin_name),
                wait_ws_seconds=2.0,
            )

        plugin_settings = self._plugin_settings_snapshot(plugin_name)
        capabilities = self._build_plugin_capabilities(
            plugin_name,
            principal,
            request_id,
            declared_capabilities=declared_capabilities,
            uses_services=uses_services,
        )
        return PluginContext(
            config=plugin_settings.config,
            secrets=plugin_settings.secrets,
            plugin_name=plugin_name,
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            http_session=self.http_session,
            send_action=send_action,
            reload_config=self.reload_config,
            reload_plugins=self._reload_plugins,
            get_command_catalog=self.router.get_command_catalog,
            list_plugins=self.plugin_manager.list_plugins,
            metrics=self.metrics,
            session_manager=self.session_manager,
            current_user_id=user_id,
            current_group_id=group_id,
            mute_control=self.dispatcher,
            config_manager=None,
            settings_reader=lambda: self._plugin_settings_snapshot(plugin_name),
            secret_reader=lambda path: self.config_manager.get_plugin_secret(plugin_name, path),
            secret_writer=lambda path, value: self.config_manager.set_plugin_secret(
                plugin_name, path, value
            ),
            secret_deleter=lambda path: self.config_manager.delete_plugin_secret(plugin_name, path),
            principal=principal,
            capabilities=capabilities,
            request_id=request_id,
            state=state,
        )

    async def _invoke_declared_service(
        self,
        *,
        caller_plugin: str,
        service_name: str,
        principal: PluginPrincipal,
        request_id: str | None,
        args: tuple[Any, ...],
        granted_capabilities: frozenset[str] = frozenset(),
    ) -> Any:
        """Invoke one current manifest binding selected by a core capability."""

        if not self.identity_service.owns(principal):
            raise PermissionError("plugin service principal was not issued by this application")
        loaded, service = self.plugin_manager.resolve_service(
            caller_plugin=caller_plugin,
            service_name=service_name,
            granted_capabilities=granted_capabilities,
        )
        user_id = principal.user_id if principal.kind == "user" else None
        group_id = principal.group_id if principal.kind == "user" else None
        target_context = self.plugin_manager.build_context(
            service.owner,
            user_id,
            group_id,
            request_id,
            principal,
        )

        async def operation() -> Any:
            return await call_plugin_callback(service.callback, *args, target_context)

        return await invoke_loaded_plugin(loaded, operation)

    def _codex_arxiv_authorized(self, principal: PluginPrincipal) -> bool:
        if not self.identity_service.owns(principal):
            return False
        if principal.is_system:
            return True
        return (
            principal.kind == "user"
            and principal.user_id is not None
            and principal.is_bot_admin
            and self.is_admin(principal.user_id)
        )

    async def _enqueue_codex_arxiv_summary(
        self,
        *,
        caller_plugin: str,
        principal: PluginPrincipal,
        request_id: str | None,
        date: str,
        links: list[str],
    ) -> str:
        if not self._codex_arxiv_authorized(principal):
            raise PermissionError("Codex arXiv capability is no longer authorized")
        user_id = principal.user_id if principal.kind == "user" else None
        group_id = principal.group_id if principal.kind == "user" else None
        return cast(
            str,
            await self._invoke_declared_service(
                caller_plugin=caller_plugin,
                service_name="codex.enqueue_arxiv_summary",
                principal=principal,
                request_id=request_id,
                args=(date, list(links), user_id, group_id),
                granted_capabilities=frozenset({"codex_arxiv_summary"}),
            ),
        )

    def _build_plugin_capabilities(
        self,
        plugin_name: str,
        principal: PluginPrincipal,
        request_id: str | None = None,
        *,
        declared_capabilities: frozenset[str] = frozenset(),
        uses_services: frozenset[str] = frozenset(),
    ) -> PluginCapabilities:
        is_system = principal.is_system and self.identity_service.owns(principal)
        is_bot_admin = (
            principal.kind == "user"
            and self.identity_service.owns(principal)
            and self.is_admin(principal.user_id)
        )
        secret_admin = None
        if "secret_admin" in declared_capabilities and is_bot_admin and principal.is_private:
            secret_admin = SecretAdminService(
                _authorized=lambda: (
                    principal.user_id is not None
                    and principal.is_private
                    and self.is_admin(principal.user_id)
                ),
                _snapshot=lambda: self.config_manager.snapshot().secrets,
                _writer=self.config_manager.update_secret,
            )

        onebot_media = None
        if "onebot_media" in declared_capabilities:
            onebot_media = OneBotMediaService(self._request_onebot_action)

        config_subscription = None
        if "config_subscription" in declared_capabilities:

            def subscribe(
                callback: Callable[[Mapping[str, Any]], Any],
            ) -> Callable[[], None]:
                def relay(snapshot: ConfigSnapshot) -> Any:
                    return callback(self._plugin_config_view(plugin_name, snapshot.config))

                return cast(Callable[[], None], self.config_manager.on_reload(relay))

            config_subscription = ConfigSubscriptionService(subscribe)

        codex_arxiv_summary = None
        if "codex.enqueue_arxiv_summary" in uses_services and (is_system or is_bot_admin):
            codex_arxiv_summary = CodexArxivSummaryService(
                _authorized=lambda: self._codex_arxiv_authorized(principal),
                _enqueue=functools.partial(
                    self._enqueue_codex_arxiv_summary,
                    caller_plugin=plugin_name,
                    principal=principal,
                    request_id=request_id,
                ),
            )

        voice_synthesis = None
        chat_reply = None
        if "voice.synthesize_text" in uses_services:

            async def synthesize_text(text: str) -> list[dict[str, Any]] | None:
                return cast(
                    list[dict[str, Any]] | None,
                    await self._invoke_declared_service(
                        caller_plugin=plugin_name,
                        service_name="voice.synthesize_text",
                        principal=principal,
                        request_id=request_id,
                        args=(text,),
                    ),
                )

            voice_synthesis = VoiceSynthesisService(synthesize_text)

        if "chat.reply" in uses_services:

            async def reply_via_chat(
                text: str,
                event: dict[str, Any],
            ) -> list[dict[str, Any]]:
                return cast(
                    list[dict[str, Any]],
                    await self._invoke_declared_service(
                        caller_plugin=plugin_name,
                        service_name="chat.reply",
                        principal=principal,
                        request_id=request_id,
                        args=(text, dict(event)),
                    ),
                )

            chat_reply = ChatReplyService(reply_via_chat)

        async def complete_ai_route(
            *,
            route_name: str,
            messages: list[dict[str, Any]],
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
        ) -> AICompletionResult:
            """用当前原子配置快照执行插件自己的 AI route。"""

            session = self.http_session
            if session is None or session.closed:
                raise RuntimeError("shared HTTP session is unavailable")
            snapshot = self.config_manager.snapshot()
            return await complete_configured_route(
                session=session,
                config=snapshot.config,
                secrets=snapshot.secrets,
                plugin_name=plugin_name,
                route_name=route_name,
                messages=messages,
                required_modalities=required_modalities,
                pinned_model=pinned_model,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
                max_retry=max_retry,
                retry_interval_seconds=retry_interval_seconds,
                tools=tools,
                tool_choice=tool_choice,
                extra_payload=extra_payload,
            )

        def list_ai_models(
            *,
            route_name: str,
            required_modalities: tuple[str, ...] = ("text",),
        ) -> tuple[AIModelInfo, ...]:
            snapshot = self.config_manager.snapshot()
            return cast(
                tuple[AIModelInfo, ...],
                list_configured_models(
                    config=snapshot.config,
                    secrets=snapshot.secrets,
                    plugin_name=plugin_name,
                    route_name=route_name,
                    required_modalities=required_modalities,
                ),
            )

        return PluginCapabilities(
            is_bot_admin=is_bot_admin,
            is_system=is_system,
            secret_admin=secret_admin,
            onebot_media=onebot_media,
            config_subscription=config_subscription,
            codex_arxiv_summary=codex_arxiv_summary,
            voice_synthesis=voice_synthesis,
            chat_reply=chat_reply,
            ai=AIService(complete_ai_route, list_ai_models),
        )

    def _plugin_config_view(
        self,
        plugin_name: str,
        config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return the immutable public config and this plugin's config namespace."""
        source = self.config if config is None else config
        return cast(Mapping[str, Any], _scoped_plugin_config(plugin_name, source))

    def _plugin_secrets_view(
        self,
        plugin_name: str,
        secrets: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return only this plugin's immutable secret namespace."""
        source = self.secrets if secrets is None else secrets
        return cast(Mapping[str, Any], _scoped_plugin_secrets(plugin_name, source))

    def _plugin_settings_snapshot(
        self,
        plugin_name: str,
        snapshot: ConfigSnapshot | None = None,
    ) -> PluginSettingsSnapshot:
        """Atomically scope one ConfigManager generation to a single plugin."""
        source = self.config_manager.snapshot() if snapshot is None else snapshot
        if self._plugin_settings_cache_revision != source.revision:
            self._plugin_settings_cache.clear()
            self._plugin_settings_cache_revision = source.revision
        cache_key = (plugin_name, source.revision)
        cached = self._plugin_settings_cache.get(cache_key)
        if cached is not None:
            return cached
        settings = PluginSettingsSnapshot(
            config=self._plugin_config_view(plugin_name, source.config),
            secrets=self._plugin_secrets_view(plugin_name, source.secrets),
            revision=source.revision,
            config_status=source.config_status.value,
            secrets_status=source.secrets_status.value,
        )
        self._plugin_settings_cache[cache_key] = settings
        return settings

    def _reload_plugins(self) -> asyncio.Task[None] | None:
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

    async def _reload_plugins_async_with_logging(self) -> None:
        """执行插件重载并记录结果"""
        try:
            async with self._reload_lock.get():
                if self._stopping:
                    return
                logger.info("Starting plugin reload...")
                completed = await self.plugin_manager.reload_all_plugins(
                    before_reload=self.session_manager.clear_plugin_sessions,
                )
                if not completed:
                    logger.error("Plugin reload stopped because a generation is quarantined")
                    return
                logger.info("Plugin reload completed successfully")
        except Exception as exc:
            logger.exception("Plugin reload failed: %s", exc)

    # ============================================================
    # 配置热更新
    # ============================================================

    def _apply_security_snapshot(self, snapshot: ConfigSnapshot) -> None:
        with self._runtime_auth_lock:
            self._apply_security_snapshot_locked(snapshot)

    def _apply_security_snapshot_locked(self, snapshot: ConfigSnapshot) -> None:
        """Synchronously revoke or rotate every already-published auth holder.

        This callback is intentionally free of awaits and task scheduling so it
        can run on ConfigManager's security publication path before ordinary
        reload callbacks.  A newer security revision also invalidates any
        cancellation-resistant runtime reconciliation that is still running.
        """

        if snapshot.revision < self._security_revision:
            logger.debug(
                "Ignoring stale security snapshot revision %d (latest=%d)",
                snapshot.revision,
                self._security_revision,
            )
            return

        new_security_generation = True
        if snapshot.revision == self._security_revision:
            accepted = self._security_snapshot
            if accepted is not None and snapshot == accepted:
                # ConfigManager.snapshot() may materialize another immutable
                # object for the same revision.  Equality is the publication
                # identity here; treating object identity as a new generation
                # can cancel the only task that is able to converge revocation.
                snapshot = accepted
                new_security_generation = False
            elif self._security_conflict_revision == snapshot.revision:
                logger.error(
                    "Ignoring another conflicting security snapshot at revision %d; "
                    "the revision remains fail-closed",
                    snapshot.revision,
                )
                if accepted is None:
                    return
                snapshot = accepted
                new_security_generation = False
            else:
                logger.critical(
                    "Conflicting security snapshots share revision %d; revoking runtime "
                    "credentials until a newer revision is published",
                    snapshot.revision,
                )
                baseline = accepted or snapshot
                snapshot = ConfigSnapshot(
                    config=baseline.config,
                    secrets={},
                    revision=snapshot.revision,
                    config_status=baseline.config_status,
                    secrets_status=ConfigSourceStatus.INCONSISTENT,
                )
                self._security_conflict_revision = snapshot.revision
        else:
            self._security_conflict_revision = None

        if new_security_generation:
            self._security_generation += 1
            self._security_snapshot = snapshot
        self._security_revision = snapshot.revision

        trusted_secrets = _trusted_secrets(snapshot)
        onebot_token, onebot_credentials_trusted = _onebot_credentials(snapshot)
        inbound_token = _inbound_credentials(snapshot)
        if (onebot_token, onebot_credentials_trusted) != (
            self._runtime_onebot_token,
            self._runtime_onebot_credentials_trusted,
        ):
            self._onebot_auth_generation += 1
        self._runtime_onebot_token = onebot_token
        self._runtime_onebot_credentials_trusted = onebot_credentials_trusted
        self._runtime_inbound_token = inbound_token
        self._load_admins(trusted_secrets)

        desired_http_base = str(snapshot.config.get("onebot_http_base", "") or "").strip()
        desired_http_base = desired_http_base.rstrip("/")
        sender = self.http_sender
        if sender is not None:
            try:
                holder_endpoint = sender.http_base
                holder_matches = bool(desired_http_base and holder_endpoint == desired_http_base)
                holder_trusted = holder_matches and onebot_credentials_trusted
                holder_token = onebot_token if holder_trusted else ""
                sender.update(
                    holder_endpoint,
                    holder_token,
                    credentials_trusted=holder_trusted,
                )
                _require_onebot_holder_credentials(
                    sender,
                    endpoint_attribute="http_base",
                    expected_endpoint=holder_endpoint,
                    expected_token=holder_token,
                    expected_trust=holder_trusted,
                )
            except BaseException as exc:
                logger.exception("Could not synchronously rotate HTTP auth", exc_info=exc)
                # HTTP senders own no background resource.  Detaching the
                # uncertain holder is the only safe publication after a
                # legacy or broken update implementation rejects revocation.
                if self.http_sender is sender:
                    self.http_sender = None

        desired_ws_uri = str(snapshot.config.get("onebot_ws_uri", "") or "").strip()
        desired_ws_enabled = bool(snapshot.config.get("enable_ws_client", True))
        ws_client = self.ws_client
        if ws_client is not None:
            try:
                holder_endpoint = ws_client.ws_uri
                holder_matches = bool(
                    desired_ws_enabled and desired_ws_uri and holder_endpoint == desired_ws_uri
                )
                holder_trusted = holder_matches and onebot_credentials_trusted
                holder_token = onebot_token if holder_trusted else ""
                ws_client.update(
                    holder_endpoint,
                    holder_token,
                    credentials_trusted=holder_trusted,
                )
                _require_onebot_holder_credentials(
                    ws_client,
                    endpoint_attribute="ws_uri",
                    expected_endpoint=holder_endpoint,
                    expected_token=holder_token,
                    expected_trust=holder_trusted,
                )
            except BaseException as exc:
                logger.exception("Could not synchronously rotate WebSocket auth", exc_info=exc)
                # Retain ownership so asynchronous reconciliation can stop the
                # socket, but remove it from every send/receive trust boundary
                # immediately.  Cancelling the listener is best-effort and is
                # thread-safe when the security callback runs off-loop.
                self._ws_client_auth_quarantine = ws_client
                ws_task = self._ws_client_task
                if ws_task is not None and not ws_task.done():
                    try:
                        ws_task.get_loop().call_soon_threadsafe(ws_task.cancel)
                    except BaseException as schedule_error:
                        try:
                            ws_task.cancel()
                        except BaseException as cancel_error:
                            logger.error(
                                "Could not cancel quarantined WebSocket listener after %s/%s",
                                type(schedule_error).__name__,
                                type(cancel_error).__name__,
                            )
            else:
                if self._ws_client_auth_quarantine is ws_client:
                    self._ws_client_auth_quarantine = None

        desired_inbound_key: tuple[Any, ...] | None = None
        try:
            desired_inbound_key = InboundManager.config_key_from_config(
                config=snapshot.config,
                token=inbound_token,
            )
        except BaseException as exc:
            logger.warning(
                "Invalid inbound configuration during synchronous auth rotation: %s",
                exc,
            )
        self._runtime_inbound_key = desired_inbound_key
        inbound_holders: list[InboundManager] = []
        for manager in (
            self.inbound_manager,
            *self._active_inbound_candidates(),
            *tuple(self._inbound_cleanup_pending),
        ):
            if manager is not None and all(manager is not item for item in inbound_holders):
                inbound_holders.append(manager)
        for manager in inbound_holders:
            try:
                safe_token = (
                    inbound_token
                    if desired_inbound_key is not None
                    and self._inbound_manager_key(manager) == desired_inbound_key
                    else ""
                )
                manager.update_token(safe_token)
            except BaseException as exc:
                logger.exception("Could not synchronously rotate inbound auth", exc_info=exc)

    def _claim_config_apply_owner(
        self,
        snapshot: ConfigSnapshot,
    ) -> _ConfigApplyOwner | None:
        with self._runtime_auth_lock:
            if snapshot.revision <= self._config_apply_revision:
                logger.debug(
                    "Ignoring duplicate or stale runtime-config snapshot revision %d (latest=%d)",
                    snapshot.revision,
                    self._config_apply_revision,
                )
                return None
            if snapshot.revision < self._security_revision:
                logger.debug(
                    "Ignoring runtime-config snapshot revision %d behind security revision %d",
                    snapshot.revision,
                    self._security_revision,
                )
                return None

            self._config_apply_generation += 1
            self._config_apply_revision = snapshot.revision
            owner = _ConfigApplyOwner(
                generation=self._config_apply_generation,
                revision=snapshot.revision,
                security_generation=self._security_generation,
            )
            self._config_apply_owner = owner
            return owner

    def _owns_config_apply(self, owner: _ConfigApplyOwner | None) -> bool:
        with self._runtime_auth_lock:
            return self._owns_config_apply_locked(owner)

    def _owns_config_apply_locked(self, owner: _ConfigApplyOwner | None) -> bool:
        return owner is None or (
            not self._stopping
            and owner == self._config_apply_owner
            and owner.security_generation == self._security_generation
            and owner.revision == self._config_apply_revision
        )

    def _latest_safe_inbound_token(self, manager: InboundManager) -> str:
        with self._runtime_auth_lock:
            if self._inbound_manager_key(manager) == self._runtime_inbound_key:
                return self._runtime_inbound_token
            return ""

    def _apply_config(self, snapshot: ConfigSnapshot) -> None:
        """应用配置变更"""
        try:
            self._apply_config_impl(snapshot)
        except Exception:
            raise
        except BaseException as exc:
            raise ApplicationLifecycleFatalError(exc) from None

    def _apply_config_impl(self, snapshot: ConfigSnapshot) -> None:
        self._apply_security_snapshot(snapshot)
        with self._runtime_auth_lock:
            if (
                snapshot.revision != self._security_revision
                or self._security_snapshot is None
                or snapshot != self._security_snapshot
            ):
                logger.error(
                    "Ignoring runtime configuration snapshot revision %d because its "
                    "security publication was not accepted",
                    snapshot.revision,
                )
                return
        owner = self._claim_config_apply_owner(snapshot)
        if owner is None:
            return
        if not self._owns_config_apply(owner):
            return
        if self._stopping:
            logger.debug("Ignoring configuration update while stopping")
            return
        config = snapshot.config

        # Validate every scalar before changing any runtime-owned component.
        session_timeout = _coerce_runtime_number(
            config.get("session_timeout", DEFAULT_SESSION_TIMEOUT_SEC),
            key="session_timeout",
            default=DEFAULT_SESSION_TIMEOUT_SEC,
            integer=False,
            minimum=0.001,
            maximum=604800.0,
        )
        concurrency = _coerce_runtime_number(
            config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            key="max_concurrency",
            default=DEFAULT_MAX_CONCURRENCY,
            integer=True,
            minimum=1,
            maximum=1024,
        )
        poll_interval = _coerce_runtime_number(
            config.get("plugin_poll_interval", 3600),
            key="plugin_poll_interval",
            default=3600.0,
            integer=False,
            minimum=0.01,
            maximum=86400.0,
        )

        self.dispatcher.refresh_prefix_cache()
        self._configure_plugin_execution(config)
        self._configure_plugin_watch(config, poll_interval=float(poll_interval))
        self.session_manager.set_default_timeout(session_timeout)

        if concurrency != self._dispatcher_concurrency:
            limiter = self.dispatcher.semaphore
            if isinstance(limiter, AdjustableSemaphore):
                limiter.resize(concurrency)
            else:
                # 兼容外部注入的旧 Dispatcher；生产路径从构造起使用可调 limiter。
                self.dispatcher.semaphore = AdjustableSemaphore(concurrency)
            self._dispatcher_concurrency = concurrency

        timezone = str(config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            scheduler = self.scheduler.scheduler
            if scheduler is None or not bool(getattr(scheduler, "running", False)):
                # A pre-start synchronous reload can safely update the lazy
                # factory input; the next start will create the right zone.
                self.scheduler.timezone = timezone
                return
            if timezone != self.scheduler.timezone:
                raise RuntimeError(
                    "cannot change a running scheduler timezone without an event loop"
                ) from None
            return

        # Timezone reconciliation is required even before the HTTP session
        # exists (for example after a clean startup rollback).  The runtime
        # method itself gates only the network components that need a session.
        for current_task in tuple(self._config_apply_tasks):
            if not current_task.done():
                current_task.cancel()
        task = asyncio.create_task(self._apply_runtime_config(snapshot, owner=owner))
        self._config_apply_tasks.add(task)
        self._config_apply_task = task

        def config_apply_done(done: asyncio.Task[None]) -> None:
            self._config_apply_tasks.discard(done)
            if self._config_apply_task is done:
                self._config_apply_task = None
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                logger.exception("Runtime configuration apply failed", exc_info=exc)

        task.add_done_callback(config_apply_done)

    def reload_config(self) -> None:
        """重新加载配置并应用变更"""
        if self._stopping:
            logger.debug("Ignoring configuration reload while stopping")
            return
        try:
            self.config_manager.reload(notify=True)
        except Exception:
            # Invalid config keeps the last-known-good public config but may
            # revoke secrets.  Apply that fail-closed snapshot before callers
            # observe the original reload error.
            snapshot = self.config_manager.snapshot()
            try:
                self._apply_config(snapshot)
            except BaseException as apply_error:
                logger.exception(
                    "Unable to apply fail-closed configuration snapshot",
                    exc_info=apply_error,
                )
            raise

    async def _apply_runtime_config(
        self,
        snapshot: ConfigSnapshot,
        *,
        owner: _ConfigApplyOwner | None = None,
    ) -> None:
        try:
            await self._apply_runtime_config_impl(snapshot, owner=owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise
        except BaseException as exc:
            raise ApplicationLifecycleFatalError(exc) from None

    async def _apply_runtime_config_impl(
        self,
        snapshot: ConfigSnapshot,
        *,
        owner: _ConfigApplyOwner | None = None,
    ) -> None:
        config = snapshot.config
        secrets = _trusted_secrets(snapshot)
        if not self._owns_config_apply(owner):
            return

        timezone = str(config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        if timezone != self.scheduler.timezone:
            if not self._owns_config_apply(owner):
                return
            await self.scheduler.reset_async(timezone)
            if not self._owns_config_apply(owner):
                return
            self._reschedule("startup")

        if not self._owns_config_apply(owner):
            return
        if not self.http_session:
            return

        http_base = str(config.get("onebot_http_base", "") or "").strip()
        onebot_token, onebot_credentials_trusted = _onebot_credentials(snapshot)
        with self._runtime_auth_lock:
            if not self._owns_config_apply_locked(owner):
                return
            if http_base:
                if not self.http_sender:
                    self.http_sender = OneBotHttpSender(
                        http_base,
                        onebot_token,
                        self.http_session,
                        credentials_trusted=onebot_credentials_trusted,
                    )
                else:
                    self.http_sender.update(
                        http_base,
                        onebot_token,
                        credentials_trusted=onebot_credentials_trusted,
                    )
            else:
                self.http_sender = None

        enable_ws = bool(config.get("enable_ws_client", True))
        ws_uri = str(config.get("onebot_ws_uri", "") or "").strip()
        ws_queue_size = self._parse_ws_queue_size(config)
        await self._reconcile_ws_client(
            enable_ws=enable_ws,
            ws_uri=ws_uri,
            token=onebot_token,
            credentials_trusted=onebot_credentials_trusted,
            queue_size=ws_queue_size,
            owner=owner,
        )

        if not self._owns_config_apply(owner):
            return
        await self._reconcile_inbound_manager(config, secrets, owner=owner)
        if not self._owns_config_apply(owner):
            return

    # ============================================================
    # 定时任务
    # ============================================================
