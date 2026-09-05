# mypy: disable-error-code=attr-defined
"""主应用启动、停止与资源回收的单一生命周期边界。"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import aiohttp

from .app_support import (
    _STARTUP_OWNERSHIP_MAX_ATTEMPTS,
    _STARTUP_OWNERSHIP_RETRY_BASE_DELAY_SECONDS,
    _STARTUP_OWNERSHIP_RETRY_MAX_DELAY_SECONDS,
    _STARTUP_OWNERSHIP_TIMEOUT_SECONDS,
    ApplicationLifecycleFatalError,
    _AppLifecycleState,
    _coerce_runtime_number,
    _ConfigApplyOwner,
    _onebot_credentials,
    _run_background_operation,
    _trusted_secrets,
)
from .config import ConfigManager, ConfigSnapshot
from .constants import (
    DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_SESSION_TIMEOUT_SEC,
)
from .dispatcher import AdjustableSemaphore
from .lifecycle import DeferredCancellation as _DeferredCancellation
from .lifecycle import OwnedTaskFatalError as _OwnedTaskFatalError
from .lifecycle import await_owned_task as _await_owned_task
from .lifecycle import run_owned_operation as _run_owned_operation
from .onebot import OneBotHttpSender
from .server import InboundManager

if TYPE_CHECKING:
    from .onebot import OneBotWsClient
    from .plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class AppLifecycleMixin:
    """串行管理启动所有权、控制面任务和逆序资源回收。"""

    _background_task_stop_timeout_seconds: float
    _config_apply_owner: _ConfigApplyOwner | None
    _config_apply_generation: int
    _config_apply_revision: int
    _config_apply_task: asyncio.Task[None] | None
    _config_apply_tasks: set[asyncio.Task[None]]
    _config_watch_task: asyncio.Task[None] | None
    _defer_plugin_schedule_updates: bool
    _inbound_cleanup_pending: list[InboundManager]
    _last_shutdown_errors: tuple[str, ...]
    _lifecycle_state: _AppLifecycleState
    _plugin_watch_desired: bool
    _plugin_watch_restart_failures: int
    _plugin_watch_restart_pending: bool
    _plugin_watch_restart_task: asyncio.Task[None] | None
    _plugin_watch_task: asyncio.Task[None] | None
    _plugin_watch_tasks: set[asyncio.Task[None]]
    _session_cleanup_task: asyncio.Task[None] | None
    _security_generation: int
    _security_snapshot: ConfigSnapshot | None
    _shutdown_task: asyncio.Task[None] | None
    _stopping: bool
    _ws_client_auth_quarantine: OneBotWsClient | None
    _ws_client_stop_task: asyncio.Task[Any] | None
    http_sender: OneBotHttpSender | None
    http_session: aiohttp.ClientSession | None
    inbound_manager: InboundManager | None
    config_manager: ConfigManager
    plugin_manager: PluginManager
    ws_client: OneBotWsClient | None

    @property
    def shutdown_errors(self) -> tuple[str, ...]:
        """返回最近一次关闭的失败快照，供进程入口判断退出状态。"""
        return self._last_shutdown_errors

    if TYPE_CHECKING:

        def _apply_security_snapshot_locked(self, snapshot: ConfigSnapshot) -> None: ...

        def _claim_config_apply_owner(
            self,
            snapshot: ConfigSnapshot,
        ) -> _ConfigApplyOwner | None: ...

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
        delay    = min(
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

            self._lifecycle_state      = _AppLifecycleState.STARTING
            self._last_shutdown_errors = ()
            try:
                await self._start_runtime()
            except BaseException as start_error:
                self._stopping             = True
                rollback_errors: list[str] = []
                deferred_cancellation      = _DeferredCancellation()
                cleanup_task               = asyncio.create_task(
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
                    self._stopping        = self._shutdown_task is not None
                    # A clean rollback owns no runtime side effects.  Release
                    # its startup-only revision claim so a same-revision
                    # pre-start apply/retry can run, while keeping generation
                    # counters and the fail-closed security revision monotonic.
                    with self._runtime_auth_lock:
                        self._config_apply_owner    = None
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
        startup_config   = startup_snapshot.config

        # 注入的测试 dispatcher 可以没有 limiter；生产 limiter 始终原地调容。
        concurrency = _coerce_runtime_number(
            startup_config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            key     = "max_concurrency",
            default = DEFAULT_MAX_CONCURRENCY,
            integer = True,
            minimum = 1,
            maximum = 1024,
        )
        session_timeout = _coerce_runtime_number(
            startup_config.get("session_timeout", DEFAULT_SESSION_TIMEOUT_SEC),
            key     = "session_timeout",
            default = DEFAULT_SESSION_TIMEOUT_SEC,
            integer = False,
            minimum = 0.001,
            maximum = 604800.0,
        )
        if self.dispatcher.semaphore is None:
            self.dispatcher.semaphore = AdjustableSemaphore(concurrency)
        elif isinstance(self.dispatcher.semaphore, AdjustableSemaphore):
            self.dispatcher.semaphore.resize(concurrency)
        self.session_manager.set_default_timeout(session_timeout)

        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total   = DEFAULT_HTTP_TIMEOUT_SECONDS,
                connect = DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS,
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
        ownership_attempt    = 0
        while not self._stopping:
            ownership_attempt += 1
            startup_snapshot = self._latest_startup_snapshot()
            owner            = self._claim_or_reuse_startup_owner(startup_snapshot)
            if owner is None:
                await self._wait_for_startup_ownership_retry(
                    attempt    = ownership_attempt,
                    started_at = ownership_started_at,
                    reason     = "claim_rejected",
                )
                continue
            config  = startup_snapshot.config
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
                    self.http_sender      = None
                    ownership_lost_reason = ""

            if ownership_lost_reason:
                await self._wait_for_startup_ownership_retry(
                    attempt    = ownership_attempt,
                    started_at = ownership_started_at,
                    reason     = ownership_lost_reason,
                )
                continue

            await self._reconcile_ws_client(
                enable_ws           = bool(config.get("enable_ws_client", True)),
                ws_uri              = str(config.get("onebot_ws_uri", "") or "").strip(),
                token               = onebot_token,
                credentials_trusted = onebot_credentials_trusted,
                queue_size          = self._parse_ws_queue_size(config),
                owner               = owner,
            )
            if not self._owns_config_apply(owner):
                await self._wait_for_startup_ownership_retry(
                    attempt    = ownership_attempt,
                    started_at = ownership_started_at,
                    reason     = "after_ws_reconcile",
                )
                continue
            await self._reconcile_inbound_manager(config, secrets, owner=owner)
            if self._owns_config_apply(owner):
                break
            await self._wait_for_startup_ownership_retry(
                attempt    = ownership_attempt,
                started_at = ownership_started_at,
                reason     = "after_inbound_reconcile",
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
            key     = "plugin_poll_interval",
            default = 3600.0,
            integer = False,
            minimum = 0.01,
            maximum = 86400.0,
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
            self._stopping      = True
            task                = asyncio.create_task(_run_owned_operation(self._stop_async))
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
        config_tasks    = set(self._config_apply_tasks)
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
        except TimeoutError as exc:
            raise RuntimeError("timed out waiting for inbound reconciliation to finish") from exc
        try:
            current                        = self.inbound_manager
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
            failed_managers               = [manager for manager, _ in failed]
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
                self.ws_client            = client
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
            stop_task                 = None

        if stop_task is None:
            if not self._owns_config_apply(owner):
                return
            stop_task                 = asyncio.create_task(_run_owned_operation(client.stop))
            self._ws_client_stop_task = stop_task
        stop_timeout   = self._background_task_stop_timeout_seconds
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
                self.ws_client            = client
                raise ApplicationLifecycleFatalError(exc.original) from None
            except BaseException:
                self._ws_client_stop_task = None
                self.ws_client            = client
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
        budget   = self._plugin_shutdown_budget_seconds()
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
        if raw_timeout is None:
            timeout = self._background_task_stop_timeout_seconds
        else:
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
        close_manager   = getattr(self.plugin_manager, "close", None)
        close_broker    = getattr(self.plugin_manager, "close_execution_broker", None)
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
            self.http_sender  = None
            logger.info("HTTP session closed")
        else:
            self.http_sender = None

    async def _cancel_config_apply_tasks(self) -> None:
        tasks   = set(self._config_apply_tasks)
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
        self._plugin_watch_desired          = False
        self._plugin_watch_restart_pending  = False
        self._plugin_watch_restart_failures = 0
        tasks                               = set(self._plugin_watch_tasks)
        current                             = self._plugin_watch_task
        if current is not None:
            tasks.add(current)
        restart_task = self._plugin_watch_restart_task
        if restart_task is not None:
            tasks.add(restart_task)
        if not tasks:
            self._plugin_watch_task         = None
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

        pending_watchers         = pending - ({restart_task} if restart_task is not None else set())
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
