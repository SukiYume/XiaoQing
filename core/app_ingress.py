# 接入重配事务：候选连接通过验证后发布，旧连接排空后回收。
"""Transactional OneBot WebSocket and inbound server reconciliation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from .app_support import (
    ApplicationLifecycleFatalError,
    InboundReconcileError,
    _AppLifecycleState,
    _ConfigApplyOwner,
    _run_background_operation,
)
from .constants import (
    DEFAULT_INBOUND_WS_QUEUE_SIZE,
)
from .lifecycle import (
    DeferredCancellation as _DeferredCancellation,
)
from .lifecycle import (
    await_owned_task as _await_owned_task,
)
from .lifecycle import (
    run_owned_operation as _run_owned_operation,
)
from .lifecycle import (
    unwrap_owned_failure as _unwrap_owned_failure,
)
from .metrics import MetricsCollector
from .onebot import (
    OneBotWsClient,
)
from .plugin_manager import PluginManager
from .scheduler import SchedulerManager
from .server import InboundManager
from .session import SessionManager

logger = logging.getLogger(__name__)


class AppIngressMixin:
    # 由组合类实际提供的成员；这里只为类型检查声明契约，运行时不存在。
    if TYPE_CHECKING:

        async def _cancel_task(self, attr_name: str) -> None:
            raise NotImplementedError

        async def _handle_inbound_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
            raise NotImplementedError

        async def _handle_upstream_event(
            self,
            event: dict[str, Any],
            *,
            source_client: OneBotWsClient | None = None,
        ) -> None:
            raise NotImplementedError

        _inbound_candidates_active: set[InboundManager]

        _inbound_candidates_lock: Any

        _inbound_reconcile_lock: Any

        def _latest_safe_inbound_token(self, manager: InboundManager) -> str:
            raise NotImplementedError

        _lifecycle_state: Any

        async def _on_ws_connected(self) -> None:
            raise NotImplementedError

        def _owns_config_apply(self, owner: _ConfigApplyOwner | None) -> bool:
            raise NotImplementedError

        def _owns_config_apply_locked(self, owner: _ConfigApplyOwner | None) -> bool:
            raise NotImplementedError

        _runtime_auth_lock: Any

        async def _stop_ws_client(self, *, owner: _ConfigApplyOwner | None = None) -> None:
            raise NotImplementedError

        def _unregister_inbound_candidate(self, manager: InboundManager) -> None:
            raise NotImplementedError

    inbound_manager            : InboundManager | None
    metrics                    : MetricsCollector
    plugin_manager             : PluginManager
    scheduler                  : SchedulerManager
    session_manager            : SessionManager
    ws_client                  : OneBotWsClient | None
    _inbound_cleanup_pending   : list[InboundManager]
    _inbound_cleanup_quarantine: dict[int, BaseException]
    _onebot_auth_generation    : int
    _runtime_inbound_key       : tuple[Any, ...] | None
    _runtime_inbound_token     : str
    _stopping                  : bool
    _ws_client_auth_generation : int
    _ws_client_task            : asyncio.Task[None] | None
    _ws_client_auth_quarantine : OneBotWsClient | None

    def _parse_ws_queue_size(self, config: Mapping[str, Any]) -> int:
        ws_queue_size_raw = config.get("ws_queue_size", DEFAULT_INBOUND_WS_QUEUE_SIZE)
        try:
            return cast(int, int(ws_queue_size_raw))
        except (TypeError, ValueError):
            return cast(int, DEFAULT_INBOUND_WS_QUEUE_SIZE)

    async def _stop_current_ws_client(self, owner: _ConfigApplyOwner | None) -> bool:
        """停止当前 WS 客户端及其监听任务；期间失去配置所有权时返回 False。"""

        if self.ws_client:
            await self._stop_ws_client(owner=owner)
            if not self._owns_config_apply(owner):
                return False
            await self._cancel_task("_ws_client_task")
        return self._owns_config_apply(owner)

    async def _reconcile_ws_client(
        self,
        *,
        enable_ws: bool,
        ws_uri: str,
        token: str,
        queue_size: int,
        credentials_trusted: bool       = True,
        owner: _ConfigApplyOwner | None = None,
    ) -> None:
        if not self._owns_config_apply(owner):
            return
        # 关闭 WS 或凭据不可信时只停止旧连接
        if not enable_ws or not ws_uri or not credentials_trusted:
            await self._stop_current_ws_client(owner)
            return

        needs_restart = (
            self.ws_client is None
            or self.ws_client.ws_uri != ws_uri
            or self.ws_client.auth_token != token
            or self.ws_client.credentials_trusted != credentials_trusted
            or self._ws_client_auth_generation != self._onebot_auth_generation
            or getattr(self.ws_client, "_queue_size", queue_size) != queue_size
            or self._ws_client_task is None
            or self._ws_client_task.done()
        )
        if not needs_restart:
            return

        if not await self._stop_current_ws_client(owner):
            return

        with self._runtime_auth_lock:
            if not self._owns_config_apply_locked(owner):
                return
            ws_client = OneBotWsClient(
                ws_uri,
                token,
                queue_size          = queue_size,
                credentials_trusted = credentials_trusted,
            )
            if not self._owns_config_apply_locked(owner):
                ws_client.update(ws_uri, "", credentials_trusted=False)
                return
            ws_client.set_on_connect(self._on_ws_connected)
            self.ws_client                  = ws_client
            self._ws_client_auth_quarantine = None
            self._ws_client_auth_generation = self._onebot_auth_generation

            async def handle_upstream_event(event: dict[str, Any]) -> None:
                await self._handle_upstream_event(event, source_client=ws_client)

            self._ws_client_task = asyncio.create_task(
                _run_background_operation(
                    lambda: ws_client.connect_and_listen(handle_upstream_event)
                )
            )

    async def _reconcile_inbound_manager(
        self,
        config: Mapping[str, Any],
        secrets: Mapping[str, Any],
        *,
        owner: _ConfigApplyOwner | None = None,
    ) -> None:
        if self._stopping or not self._owns_config_apply(owner):
            return
        raw_inbound_token = secrets.get("inbound_token", "")
        if type(raw_inbound_token) is str:
            inbound_token = raw_inbound_token
        else:
            inbound_token = ""
            logger.error(
                "Invalid inbound_token type during runtime reconciliation: %s; inbound "
                "authentication is revoked",
                type(raw_inbound_token).__name__,
            )
        current = self.inbound_manager
        try:
            with self._inbound_candidates_lock:
                desired = InboundManager.from_config(
                    config  = config,
                    token   = inbound_token,
                    handler = self._handle_inbound_event,
                )
                if desired is not None:
                    self._inbound_candidates_active.add(desired)
        except BaseException:
            if current is not None:
                current.update_token("")
            raise
        try:
            with self._runtime_auth_lock:
                if not self._owns_config_apply_locked(owner):
                    if desired is not None:
                        desired.update_token("")
                    return
                current = self._sync_current_inbound_token(inbound_token, desired)
            deferred_cancellation = _DeferredCancellation()
            try:
                await self._reconcile_inbound_manager_impl(
                    inbound_token,
                    deferred_cancellation,
                    desired,
                    owner=owner,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                deferred_cancellation.raise_if_requested(cause=exc)
                raise
            except BaseException as exc:
                deferred_cancellation.raise_if_requested(cause=exc)
                raise ApplicationLifecycleFatalError(exc) from None
            deferred_cancellation.raise_if_requested()
        finally:
            if desired is not None:
                self._unregister_inbound_candidate(desired)

    def _sync_current_inbound_token(
        self,
        inbound_token: str,
        desired: InboundManager | None,
    ) -> InboundManager | None:
        """监听配置未变时当前监听器沿用新 token，否则先吊销，避免旧端口接受新凭据。"""

        current = self.inbound_manager
        if current is not None:
            same_listener = desired is not None and self._inbound_manager_key(
                current
            ) == self._inbound_manager_key(desired)
            current.update_token(inbound_token if same_listener else "")
        return current

    async def _discard_inbound_candidate(
        self,
        desired: InboundManager,
        deferred_cancellation: _DeferredCancellation,
    ) -> None:
        """吊销候选监听器的 token 并停止它。"""

        desired.update_token("")
        await self._cleanup_inbound_candidate(desired, deferred_cancellation)

    async def _rollback_inbound_manager(
        self,
        current: InboundManager,
        error: BaseException | None,
        deferred_cancellation: _DeferredCancellation,
    ) -> None:
        """恢复最新可用 token 后重新启动旧监听器；error 为空表示配置所有权已变化。"""

        current.update_token(self._latest_safe_inbound_token(current))
        await self._restore_inbound_manager(
            current,
            error or RuntimeError("runtime configuration ownership changed"),
            deferred_cancellation,
        )

    async def _reconcile_inbound_manager_impl(
        self,
        inbound_token: str,
        deferred_cancellation: _DeferredCancellation,
        desired: InboundManager | None,
        *,
        owner: _ConfigApplyOwner | None = None,
    ) -> None:
        with self._runtime_auth_lock:
            if self._stopping or not self._owns_config_apply_locked(owner):
                return
            current = self._sync_current_inbound_token(inbound_token, desired)
        async with self._inbound_reconcile_lock.get():
            with self._runtime_auth_lock:
                if self._stopping or not self._owns_config_apply_locked(owner):
                    return
                current = self._sync_current_inbound_token(inbound_token, desired)
            await self._drain_inbound_cleanup_pending(deferred_cancellation)
            if not self._owns_config_apply(owner):
                if desired is not None:
                    await self._discard_inbound_candidate(desired, deferred_cancellation)
                return
            with self._runtime_auth_lock:
                if not self._owns_config_apply_locked(owner):
                    if desired is not None:
                        desired.update_token("")
                    return
                current = self._sync_current_inbound_token(inbound_token, desired)
            desired_key = self._inbound_manager_key(desired)
            current_key = self._inbound_manager_key(current)

            if desired is None:
                if current is not None:
                    try:
                        await current.stop()
                    except BaseException as stop_error:
                        if not self._stopping:
                            await self._restore_inbound_manager(
                                current,
                                stop_error,
                                deferred_cancellation,
                            )
                        raise
                    if not self._owns_config_apply(owner):
                        await self._rollback_inbound_manager(current, None, deferred_cancellation)
                        return
                    if self.inbound_manager is current:
                        self.inbound_manager = None
                return

            if current is None:
                self._bind_inbound_status_providers(desired)
                try:
                    await desired.start()
                    if not self._owns_config_apply(owner):
                        await self._discard_inbound_candidate(desired, deferred_cancellation)
                        return
                except BaseException:
                    cleanup_error = await self._cleanup_inbound_candidate(
                        desired,
                        deferred_cancellation,
                    )
                    if cleanup_error is not None:
                        desired.update_token("")
                        with self._runtime_auth_lock:
                            if self.inbound_manager is None:
                                self.inbound_manager = desired
                    raise
                with self._runtime_auth_lock:
                    publish_candidate = not self._stopping and self._owns_config_apply_locked(owner)
                    if publish_candidate:
                        self.inbound_manager = desired
                if not publish_candidate:
                    await self._discard_inbound_candidate(desired, deferred_cancellation)
                    return
                return

            if current_key == desired_key:
                with self._runtime_auth_lock:
                    if not self._owns_config_apply_locked(owner):
                        return
                    current.update_token(inbound_token)
                self._bind_inbound_status_providers(current)
                return

            self._bind_inbound_status_providers(desired)
            disjoint_ports = current.binding_ports.isdisjoint(desired.binding_ports)
            if disjoint_ports:
                await self._switch_disjoint_inbound_manager(
                    current,
                    desired,
                    deferred_cancellation,
                    owner=owner,
                )
            else:
                await self._switch_overlapping_inbound_manager(
                    current,
                    desired,
                    deferred_cancellation,
                    owner=owner,
                )

    async def _cleanup_inbound_candidate(
        self,
        manager: InboundManager,
        deferred_cancellation: _DeferredCancellation,
    ) -> BaseException | None:
        cleanup_task = asyncio.create_task(_run_owned_operation(manager.stop))
        try:
            await _await_owned_task(cleanup_task, deferred_cancellation)
        except BaseException as exc:
            cleanup_error = _unwrap_owned_failure(exc)
            if all(manager is not pending for pending in self._inbound_cleanup_pending):
                self._inbound_cleanup_pending.append(manager)
            if self._lifecycle_state is _AppLifecycleState.STARTING:
                self._inbound_cleanup_quarantine[id(manager)] = cleanup_error
            logger.exception("Failed to clean an inbound candidate", exc_info=exc)
            return cast(BaseException, cleanup_error)
        self._inbound_cleanup_quarantine.pop(id(manager), None)
        self._inbound_cleanup_pending = [
            pending for pending in self._inbound_cleanup_pending if pending is not manager
        ]
        return None

    async def _drain_inbound_cleanup_pending(
        self,
        deferred_cancellation: _DeferredCancellation,
    ) -> None:
        for manager in tuple(self._inbound_cleanup_pending):
            cleanup_error = await self._cleanup_inbound_candidate(
                manager,
                deferred_cancellation,
            )
            if cleanup_error is not None:
                raise RuntimeError(
                    "an earlier inbound candidate still cannot be cleaned"
                ) from cleanup_error

    async def _restore_inbound_manager(
        self,
        manager: InboundManager,
        original_error: BaseException,
        deferred_cancellation: _DeferredCancellation,
    ) -> None:
        restore_task = asyncio.create_task(_run_owned_operation(manager.start))
        try:
            await _await_owned_task(restore_task, deferred_cancellation)
        except BaseException as restore_error:
            restore_error        = _unwrap_owned_failure(restore_error)
            self.inbound_manager = None
            if all(manager is not pending for pending in self._inbound_cleanup_pending):
                self._inbound_cleanup_pending.append(manager)
            raise InboundReconcileError(original_error, restore_error) from original_error
        self.inbound_manager = manager

    async def _switch_disjoint_inbound_manager(
        self,
        current: InboundManager,
        desired: InboundManager,
        deferred_cancellation: _DeferredCancellation,
        *,
        owner: _ConfigApplyOwner | None = None,
    ) -> None:
        if not self._owns_config_apply(owner):
            return
        try:
            # Bind disjoint replacement ports first, but keep admission closed
            # until the old manager has drained its shared HTTP/WS dispatcher.
            # This preserves preflight bind safety without allowing two
            # generations to execute the same session concurrently.
            await desired.start(accept_events=False)
        except BaseException:
            await self._cleanup_inbound_candidate(desired, deferred_cancellation)
            raise
        if self._stopping or not self._owns_config_apply(owner):
            await self._discard_inbound_candidate(desired, deferred_cancellation)
            return
        try:
            await current.stop()
        except BaseException as stop_error:
            await self._discard_inbound_candidate(desired, deferred_cancellation)
            if not self._stopping:
                await self._rollback_inbound_manager(current, stop_error, deferred_cancellation)
            raise
        if not self._owns_config_apply(owner):
            await self._discard_inbound_candidate(desired, deferred_cancellation)
            await self._rollback_inbound_manager(current, None, deferred_cancellation)
            return
        commit_error: BaseException | None = None
        try:
            with self._runtime_auth_lock:
                publish_candidate = not self._stopping and self._owns_config_apply_locked(owner)
                if publish_candidate:
                    desired.commit_admission()
                    self.inbound_manager = desired
        except BaseException as exc:
            commit_error      = exc
            publish_candidate = False
        if not publish_candidate:
            await self._discard_inbound_candidate(desired, deferred_cancellation)
            if not self._stopping:
                await self._rollback_inbound_manager(current, commit_error, deferred_cancellation)
            if commit_error is not None:
                raise commit_error
            return

    async def _switch_overlapping_inbound_manager(
        self,
        current: InboundManager,
        desired: InboundManager,
        deferred_cancellation: _DeferredCancellation,
        *,
        owner: _ConfigApplyOwner | None = None,
    ) -> None:
        if not self._owns_config_apply(owner):
            return
        try:
            await current.stop()
        except BaseException as stop_error:
            if not self._stopping:
                await self._rollback_inbound_manager(current, stop_error, deferred_cancellation)
            raise
        if not self._owns_config_apply(owner):
            await self._rollback_inbound_manager(current, None, deferred_cancellation)
            return
        if self._stopping:
            return
        try:
            await desired.start()
        except BaseException as candidate_error:
            await self._discard_inbound_candidate(desired, deferred_cancellation)
            if self._stopping:
                raise
            await self._rollback_inbound_manager(current, candidate_error, deferred_cancellation)
            raise
        with self._runtime_auth_lock:
            publish_candidate = not self._stopping and self._owns_config_apply_locked(owner)
            if publish_candidate:
                self.inbound_manager = desired
        if not publish_candidate:
            await self._discard_inbound_candidate(desired, deferred_cancellation)
            if not self._stopping:
                await self._rollback_inbound_manager(current, None, deferred_cancellation)
            return

    @staticmethod
    def _inbound_manager_key(manager: InboundManager | None) -> tuple[Any, ...] | None:
        if manager is None:
            return None
        return cast(tuple[Any, ...], manager.config_key)

    def _bind_inbound_status_providers(self, manager: InboundManager) -> None:
        def _plugins_count() -> int:
            return len(self.plugin_manager.list_plugins())

        def _sessions_count() -> int:
            return cast(int, self.session_manager.active_count)

        def _pending_jobs() -> int:
            scheduler = self.scheduler.scheduler
            if not scheduler:
                return 0
            return len(scheduler.get_jobs())

        def _metrics() -> dict[str, Any]:
            return cast(dict[str, Any], self.metrics.summary_snapshot())

        manager.set_status_providers(
            plugins_count  = _plugins_count,
            sessions_count = _sessions_count,
            pending_jobs   = _pending_jobs,
            metrics        = _metrics,
        )
