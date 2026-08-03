# mypy: disable-error-code=attr-defined
"""Application-owned plugin watcher supervision."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from .app_support import (
    _AppLifecycleState,
    _coerce_runtime_number,
    _run_background_operation,
)
from .plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class AppPluginWatchMixin:
    plugin_manager: PluginManager
    _lifecycle_state: _AppLifecycleState
    _plugin_watch_desired: bool
    _plugin_watch_restart_base_delay_seconds: float
    _plugin_watch_restart_failures: int
    _plugin_watch_restart_max_delay_seconds: float
    _plugin_watch_restart_pending: bool
    _plugin_watch_restart_task: asyncio.Task[None] | None
    _plugin_watch_stable_reset_seconds: float
    _plugin_watch_task: asyncio.Task[None] | None
    _plugin_watch_tasks: set[asyncio.Task[None]]
    _stopping: bool

    def _configure_plugin_execution(self, config: Mapping[str, Any]) -> None:
        configure = getattr(self.plugin_manager, "configure_execution", None)
        if callable(configure):
            configure(config.get("plugin_execution", {}))

    def _plugin_watch_enabled(self, config: Mapping[str, Any] | None = None) -> bool:
        source = config if config is not None else self.config
        return bool(source.get("enable_plugin_watcher", False)) and bool(
            getattr(self.plugin_manager, "hot_reload_supported", True)
        )

    def _plugin_watch_poll_interval(self, config: Mapping[str, Any] | None = None) -> float:
        source = config if config is not None else self.config
        raw_interval = source.get("plugin_poll_interval", 3600)
        try:
            interval = _coerce_runtime_number(
                raw_interval,
                key="plugin_poll_interval",
                default=3600.0,
                integer=False,
                minimum=0.01,
                maximum=86400.0,
            )
        except ValueError:
            return 3600.0
        return float(interval)

    def _watch_runtime_active(self) -> bool:
        # The plugin watcher is an application-owned control-plane service.
        # Its liveness must not depend on the unrelated config watcher task:
        # one background service failing must not permanently disable the
        # other's supervisor.
        return not self._stopping and self._lifecycle_state in {
            _AppLifecycleState.STARTING,
            _AppLifecycleState.RUNNING,
        }

    def _plugin_watch_can_restart(self) -> bool:
        return (
            self._plugin_watch_desired
            and not self._stopping
            and self._watch_runtime_active()
            and not any(not item.done() for item in self._plugin_watch_tasks)
        )

    def _schedule_plugin_watch_restart(self, *, immediate: bool = False) -> None:
        """Schedule one supervised restart with bounded exponential backoff."""

        existing = self._plugin_watch_restart_task
        if existing is not None and not existing.done():
            return
        if not self._plugin_watch_can_restart():
            return

        if immediate:
            delay = 0.0
        else:
            exponent = min(self._plugin_watch_restart_failures, 30)
            delay = min(
                self._plugin_watch_restart_max_delay_seconds,
                self._plugin_watch_restart_base_delay_seconds * (2**exponent),
            )
            self._plugin_watch_restart_failures += 1

        async def restart_after_delay() -> None:
            if delay > 0:
                await asyncio.sleep(delay)
            current = asyncio.current_task()
            if self._plugin_watch_restart_task is current:
                self._plugin_watch_restart_task = None
            if self._plugin_watch_can_restart():
                self._start_plugin_watch_generation()

        restart_task = asyncio.create_task(restart_after_delay())
        self._plugin_watch_restart_task = restart_task

        def restart_done(done: asyncio.Task[None]) -> None:
            if self._plugin_watch_restart_task is done:
                self._plugin_watch_restart_task = None
            cancelled = False
            try:
                done.result()
            except asyncio.CancelledError:
                cancelled = True
            except BaseException as exc:
                logger.error(
                    "Plugin watcher restart supervisor failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            if (
                cancelled
                and self._plugin_watch_restart_pending
                and self._plugin_watch_can_restart()
            ):
                self._plugin_watch_restart_pending = False
                self._schedule_plugin_watch_restart(immediate=True)

        restart_task.add_done_callback(restart_done)

    def _start_plugin_watch_generation(self) -> None:
        if not self._plugin_watch_can_restart():
            return
        started_at = asyncio.get_running_loop().time()
        task = asyncio.create_task(_run_background_operation(self.plugin_manager.watch))
        self._plugin_watch_tasks.add(task)
        self._plugin_watch_task = task

        def plugin_watch_done(done: asyncio.Task[None]) -> None:
            ran_for = max(0.0, asyncio.get_running_loop().time() - started_at)
            self._plugin_watch_tasks.discard(done)
            is_current_generation = self._plugin_watch_task is done
            if is_current_generation:
                self._plugin_watch_task = None
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                logger.error(
                    "Plugin watcher task failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            else:
                if is_current_generation and self._plugin_watch_desired and not self._stopping:
                    logger.warning("Plugin watcher exited unexpectedly; scheduling restart")

            # A done callback can be queued while configuration already starts
            # a newer generation.  It may consume/log its own outcome, but it
            # must not mutate the newer generation's restart ownership.
            if not is_current_generation:
                return
            was_restart_pending = self._plugin_watch_restart_pending
            self._plugin_watch_restart_pending = False
            if ran_for >= self._plugin_watch_stable_reset_seconds:
                self._plugin_watch_restart_failures = 0

            if self._plugin_watch_can_restart():
                # A disable->enable transition deliberately cancelled the old
                # generation and can restart immediately once it is terminal.
                # Unexpected return/failure uses backoff to prevent a broken
                # watch implementation from creating a hot task loop.
                self._schedule_plugin_watch_restart(immediate=was_restart_pending)

        task.add_done_callback(plugin_watch_done)

    def _configure_plugin_watch(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        poll_interval: float | None = None,
    ) -> None:
        if self._stopping:
            logger.debug("Ignoring plugin watcher configuration while stopping")
            return
        self.plugin_manager.update_poll_interval(
            self._plugin_watch_poll_interval(config) if poll_interval is None else poll_interval
        )
        enabled = self._plugin_watch_enabled(config)
        self._plugin_watch_desired = enabled
        if not self._watch_runtime_active():
            return

        live_tasks = [task for task in self._plugin_watch_tasks if not task.done()]
        current = self._plugin_watch_task
        if current is not None and not current.done() and current not in live_tasks:
            self._plugin_watch_tasks.add(current)
            live_tasks.append(current)

        if enabled:
            if live_tasks:
                self._plugin_watch_restart_pending = any(
                    task.cancelling() > 0 for task in live_tasks
                )
            elif (
                self._plugin_watch_restart_task is not None
                and not self._plugin_watch_restart_task.done()
            ):
                # A failed generation already owns the delayed restart.
                self._plugin_watch_restart_pending = (
                    self._plugin_watch_restart_task.cancelling() > 0
                )
                return
            else:
                self._plugin_watch_restart_pending = False
                self._start_plugin_watch_generation()
            return

        self._plugin_watch_restart_pending = False
        self._plugin_watch_restart_failures = 0
        restart_task = self._plugin_watch_restart_task
        if restart_task is not None and not restart_task.done():
            restart_task.cancel()
        for task in live_tasks:
            task.cancel()
