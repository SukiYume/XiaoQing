"""Capability-gated APScheduler shutdown integration.

APScheduler 3.x does not expose a public API that both stops new scheduling
and retains ownership of every running asyncio Future until it is truly done.
The adapter below contains the small private-API surface needed for that
stronger drain.  Callers must probe the concrete scheduler first and fall back
to APScheduler's public shutdown contract when the layout is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import apscheduler
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN, SchedulerEvent
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_STOPPED

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerDrainCapability:
    available: bool
    reason: str | None
    apscheduler_version: str


def _unavailable(reason: str) -> SchedulerDrainCapability:
    return SchedulerDrainCapability(
        available=False,
        reason=reason,
        apscheduler_version=str(getattr(apscheduler, "__version__", "unknown")),
    )


def probe_asyncio_scheduler_drain(scheduler: object) -> SchedulerDrainCapability:
    """Inspect the concrete 3.x scheduler layout without mutating it."""

    if not isinstance(scheduler, AsyncIOScheduler):
        return _unavailable("scheduler is not APScheduler's AsyncIOScheduler")

    for name in (
        "_executors_lock",
        "_jobstores_lock",
        "_executors",
        "_jobstores",
        "_eventloop",
        "_logger",
        "state",
    ):
        if not hasattr(scheduler, name):
            return _unavailable(f"AsyncIOScheduler is missing {name}")
    for name in ("_stop_timer", "_dispatch_event"):
        if not callable(getattr(scheduler, name, None)):
            return _unavailable(f"AsyncIOScheduler has incompatible {name}")

    try:
        with scheduler._executors_lock, scheduler._jobstores_lock:
            executors = tuple(scheduler._executors.values())
            tuple(scheduler._jobstores.values())
    except BaseException as exc:
        return _unavailable(
            f"AsyncIOScheduler resource registry probe failed: {type(exc).__name__}"
        )

    for executor in executors:
        if not isinstance(executor, AsyncIOExecutor):
            continue
        pending = getattr(executor, "_pending_futures", None)
        if not isinstance(pending, set):
            return _unavailable("AsyncIOExecutor pending Future registry is incompatible")

    scheduler_logger = getattr(scheduler, "_logger", None)
    if not callable(getattr(scheduler_logger, "info", None)):
        return _unavailable("AsyncIOScheduler logger is incompatible")

    return SchedulerDrainCapability(
        available=True,
        reason=None,
        apscheduler_version=str(getattr(apscheduler, "__version__", "unknown")),
    )


class AsyncIOSchedulerDrain:
    """Own one capability-verified private shutdown transaction."""

    def __init__(self, scheduler: AsyncIOScheduler, *, timeout_seconds: float) -> None:
        capability = probe_asyncio_scheduler_drain(scheduler)
        if not capability.available:
            raise ValueError(capability.reason or "APScheduler private drain is unavailable")
        self.scheduler = scheduler
        self.timeout_seconds = timeout_seconds
        self.pending_executor_cleanup: list[Any] = []
        self.pending_jobstore_cleanup: list[Any] = []
        self.pending_job_futures: set[asyncio.Future[Any]] = set()
        self.job_futures_cancel_requested: set[asyncio.Task[Any]] = set()
        self.timer_cleanup_pending = False
        self._started = False

    def _begin(self) -> None:
        if not self._started:
            with self.scheduler._executors_lock, self.scheduler._jobstores_lock:
                self.pending_executor_cleanup = list(self.scheduler._executors.values())
                self.pending_jobstore_cleanup = list(self.scheduler._jobstores.values())
            self.pending_job_futures.clear()
            self.job_futures_cancel_requested.clear()
            self.timer_cleanup_pending = True
            self._started = True
        self.scheduler.state = STATE_STOPPED

    def _cleanup_resources(self, *, wait: bool) -> list[tuple[str, BaseException]]:
        self._begin()
        errors: list[tuple[str, BaseException]] = []

        for executor in tuple(self.pending_executor_cleanup):
            if isinstance(executor, AsyncIOExecutor):
                self.pending_job_futures.update(
                    future for future in tuple(executor._pending_futures) if not future.done()
                )
                self._prune_finished_jobs()
                if self.pending_job_futures:
                    # AsyncIOExecutor.shutdown() cancels and drops this set
                    # immediately.  Retain it until every underlying job has
                    # reached a real terminal state.
                    continue
            try:
                executor.shutdown(wait)
            except BaseException as exc:
                errors.append(("executor", exc))
            else:
                self.pending_executor_cleanup.remove(executor)

        for jobstore in tuple(self.pending_jobstore_cleanup):
            try:
                jobstore.shutdown()
            except BaseException as exc:
                errors.append(("job store", exc))
            else:
                self.pending_jobstore_cleanup.remove(jobstore)

        if self.timer_cleanup_pending:
            try:
                self.scheduler._stop_timer()
            except BaseException as exc:
                errors.append(("timer", exc))
            else:
                self.timer_cleanup_pending = False

        return errors

    def _prune_finished_jobs(self) -> None:
        finished = {future for future in self.pending_job_futures if future.done()}
        self.pending_job_futures.difference_update(finished)
        finished_cancel_requests = {
            task for task in self.job_futures_cancel_requested if task.done()
        }
        self.job_futures_cancel_requested.difference_update(finished_cancel_requests)

    def _request_job_cancellation(self) -> list[tuple[str, BaseException]]:
        errors: list[tuple[str, BaseException]] = []
        self._prune_finished_jobs()
        for future in tuple(self.pending_job_futures):
            if not isinstance(future, asyncio.Task):
                # Cancelling run_in_executor()'s asyncio wrapper would mark it
                # done while the underlying thread continued running.
                continue
            if future in self.job_futures_cancel_requested:
                continue
            try:
                future.cancel()
            except BaseException as exc:
                errors.append(("scheduled job cancellation", exc))
            else:
                self.job_futures_cancel_requested.add(future)
        return errors

    async def _drain_jobs(self) -> list[tuple[str, BaseException]]:
        errors = self._request_job_cancellation()
        pending = set(self.pending_job_futures)
        if pending:
            try:
                done, _still_pending = await asyncio.wait(
                    pending,
                    timeout=self.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                errors.append(("scheduled job drain", exc))
            else:
                self.pending_job_futures.difference_update(done)
        self._prune_finished_jobs()
        if self.pending_job_futures:
            errors.append(
                (
                    "scheduled jobs",
                    RuntimeError(
                        f"{len(self.pending_job_futures)} job(s) did not stop within "
                        f"{self.timeout_seconds:.3f}s"
                    ),
                )
            )
        return errors

    @staticmethod
    def _raise_failure(errors: list[tuple[str, BaseException]]) -> None:
        summary = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in errors)
        raise RuntimeError(f"scheduler cleanup failed: {summary}") from errors[0][1]

    def _finish(self) -> None:
        self.scheduler._eventloop = None
        try:
            self.scheduler._logger.info("Scheduler has been shut down")
            self.scheduler._dispatch_event(SchedulerEvent(EVENT_SCHEDULER_SHUTDOWN))
        except BaseException as exc:
            # Event notification owns no resources.  Observer failures after
            # teardown must not resurrect a dead scheduler generation.
            logger.error(
                "Scheduler shutdown notification failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        self.pending_executor_cleanup.clear()
        self.pending_jobstore_cleanup.clear()
        self.pending_job_futures.clear()
        self.job_futures_cancel_requested.clear()
        self.timer_cleanup_pending = False

    async def shutdown_async(self, *, wait: bool) -> None:
        errors = self._cleanup_resources(wait=wait)
        errors.extend(await self._drain_jobs())
        if not errors:
            errors.extend(self._cleanup_resources(wait=wait))
        if errors:
            self._raise_failure(errors)
        self._finish()

    def shutdown(self, *, wait: bool) -> None:
        errors = self._cleanup_resources(wait=wait)
        errors.extend(self._request_job_cancellation())
        self._prune_finished_jobs()
        if self.pending_job_futures:
            errors.append(
                (
                    "scheduled jobs",
                    RuntimeError(
                        f"{len(self.pending_job_futures)} job(s) are still stopping; "
                        "use shutdown_async() to drain them"
                    ),
                )
            )
        elif not errors:
            errors.extend(self._cleanup_resources(wait=wait))
        if errors:
            self._raise_failure(errors)
        self._finish()
