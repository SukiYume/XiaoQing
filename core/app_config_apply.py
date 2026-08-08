# mypy: disable-error-code=attr-defined
"""应用配置快照的安全发布与运行时热更新边界。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .app_support import (
    ApplicationLifecycleFatalError,
    _coerce_runtime_number,
    _ConfigApplyOwner,
    _inbound_credentials,
    _onebot_credentials,
    _require_onebot_holder_credentials,
    _trusted_secrets,
)
from .config import ConfigSnapshot, ConfigSourceStatus
from .constants import DEFAULT_MAX_CONCURRENCY, DEFAULT_SESSION_TIMEOUT_SEC
from .dispatcher import AdjustableSemaphore
from .onebot import OneBotHttpSender
from .server import InboundManager

if TYPE_CHECKING:
    from .config import ConfigManager
    from .dispatcher import Dispatcher
    from .onebot import OneBotWsClient
    from .scheduler import SchedulerManager
    from .session import SessionManager

logger = logging.getLogger(__name__)


class AppConfigApplyMixin:
    """按配置代际串行发布认证、传输、调度与插件运行参数。"""

    # 这些字段由 XiaoQingApp.__init__ 创建；在边界模块中显式声明联合类型，避免
    # Mypy 根据某一条热更新分支的首次赋值把可空生命周期字段错误收窄。
    config_manager: ConfigManager
    dispatcher: Dispatcher
    scheduler: SchedulerManager
    session_manager: SessionManager
    http_sender: OneBotHttpSender | None
    inbound_manager: InboundManager | None
    ws_client: OneBotWsClient | None
    _ws_client_task: asyncio.Task[None] | None
    _ws_client_auth_quarantine: OneBotWsClient | None
    _config_apply_task: asyncio.Task[None] | None
    _config_apply_tasks: set[asyncio.Task[None]]
    _config_apply_owner: _ConfigApplyOwner | None
    _config_apply_generation: int
    _config_apply_revision: int
    _security_snapshot: ConfigSnapshot | None
    _security_conflict_revision: int | None
    _security_generation: int
    _security_revision: int
    _runtime_onebot_token: str
    _runtime_onebot_credentials_trusted: bool
    _runtime_inbound_token: str
    _runtime_inbound_key: tuple[Any, ...] | None
    _onebot_auth_generation: int
    _dispatcher_concurrency: int
    _inbound_cleanup_pending: list[InboundManager]
    _stopping: bool

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
