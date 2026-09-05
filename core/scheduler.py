"""定时任务的事务式替换、回滚与有界关闭管理。

常规排程只使用 APScheduler 公开接口。运行中 job 的精确收敛由
``scheduler_compat`` 在运行期验证私有布局后提供；探测失败时降级到公开 shutdown，
不会因补丁版本或新解释器而在导入期拒绝启动。
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .scheduler_compat import (
    AsyncIOSchedulerDrain,
    SchedulerDrainCapability,
    probe_asyncio_scheduler_drain,
)

logger                  = logging.getLogger(__name__)
_REAL_ASYNCIO_SCHEDULER = AsyncIOScheduler


@dataclass(frozen=True, slots=True)
class ScheduledJobSpec:
    """用于事务式替换的一份完整 Cron 任务声明。"""

    job_id: str
    func: Callable[..., Any]
    cron: Mapping[str, Any]
    description: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedJob:
    job_id: str
    func: Callable[..., Any]
    trigger: CronTrigger
    description: str | None


@dataclass(frozen=True, slots=True)
class _JobSnapshot:
    job_id: str
    func: Callable[..., Any]
    trigger: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    name: str
    misfire_grace_time: int | None
    coalesce: bool
    max_instances: int
    next_run_time: Any
    executor: str


class SchedulerManager:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.timezone                                                = timezone
        self.scheduler: AsyncIOScheduler | None                      = None
        self._started                                                = False
        self._shutdown_timeout_seconds                               = 5.0
        self._scheduler_drain: AsyncIOSchedulerDrain | None          = None
        self._public_shutdown_owner: AsyncIOScheduler | None         = None
        self._last_drain_capability: SchedulerDrainCapability | None = None
        self._public_fallback_logged                                 = False
        self._job_mutation_lock                                      = threading.RLock()
        # 只有当前线程已有事件循环时才立即初始化，否则延迟到首次使用。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._init_scheduler()

    def _create_started_scheduler(self, timezone: str) -> AsyncIOScheduler:
        candidate = AsyncIOScheduler(timezone=timezone)
        try:
            candidate.start()
        except BaseException:
            # A start implementation may fail after acquiring resources.  Do
            # not publish an inert candidate, but retain a demonstrably running
            # one so application rollback can still shut it down.
            if bool(getattr(candidate, "running", False)):
                self.scheduler = candidate
                self._started  = True
            raise
        return candidate

    def _init_scheduler(self) -> None:
        """Initialize and start the scheduler (requires event loop)"""
        if self._started:
            return
        existing = self.scheduler
        if existing is not None:
            if (
                self._scheduler_drain is not None and self._scheduler_drain.scheduler is existing
            ) or self._public_shutdown_owner is existing:
                raise RuntimeError("cannot start scheduler while previous cleanup is incomplete")
            if bool(getattr(existing, "running", False)):
                self._started = True
                return
            # An unstarted candidate owns no scheduler resources and must not
            # poison every later retry.
            self.scheduler = None
        candidate      = self._create_started_scheduler(self.timezone)
        self.scheduler = candidate
        self._started  = True

    def ensure_started(self) -> None:
        """Ensure scheduler is initialized and started (requires event loop)"""
        if not self._started:
            self._init_scheduler()

    def reset(self, timezone: str | None = None) -> None:
        """Reset scheduler with a new timezone and clear existing jobs."""
        # Do not publish a new timezone while the old generation is still
        # running.  A failed shutdown retains ownership of that scheduler and
        # must abort the reset so callers can retry cleanup explicitly.
        target_timezone   = timezone or self.timezone
        previous_timezone = self.timezone
        self.shutdown()
        try:
            candidate = self._create_started_scheduler(target_timezone)
        except BaseException:
            # The old generation is already stopped, but keeping the published
            # timezone unchanged ensures the same desired config retries.
            self.timezone = previous_timezone
            raise
        self.scheduler = candidate
        self._started  = True
        self.timezone  = target_timezone

    async def reset_async(self, timezone: str | None = None) -> None:
        """Asynchronously stop the old event-loop generation before replacement."""
        target_timezone   = timezone or self.timezone
        previous_timezone = self.timezone
        await self.shutdown_async()
        try:
            candidate = self._create_started_scheduler(target_timezone)
        except BaseException:
            self.timezone = previous_timezone
            raise
        self.scheduler = candidate
        self._started  = True
        self.timezone  = target_timezone

    def _private_drain_for(
        self,
        scheduler: AsyncIOScheduler,
    ) -> AsyncIOSchedulerDrain | None:
        current = self._scheduler_drain
        if current is not None:
            if current.scheduler is not scheduler:
                raise RuntimeError("scheduler cleanup ownership changed unexpectedly")
            return current
        capability                  = probe_asyncio_scheduler_drain(scheduler)
        self._last_drain_capability = capability
        if not capability.available:
            if not self._public_fallback_logged:
                logger.warning(
                    "APScheduler %s private job drain is unavailable; using public "
                    "shutdown semantics: %s",
                    capability.apscheduler_version,
                    capability.reason or "capability probe failed",
                )
                self._public_fallback_logged = True
            return None
        current = AsyncIOSchedulerDrain(
            scheduler,
            timeout_seconds=self._shutdown_timeout_seconds,
        )
        self._scheduler_drain = current
        return current

    def _complete_shutdown(self) -> None:
        self._scheduler_drain       = None
        self._public_shutdown_owner = None
        self.scheduler              = None
        self._started               = False

    async def _shutdown_public_async(
        self,
        scheduler: AsyncIOScheduler,
        *,
        wait: bool,
    ) -> None:
        if self._public_shutdown_owner is None:
            try:
                scheduler.shutdown(wait=wait)
            except SchedulerNotRunningError:
                if not bool(getattr(scheduler, "running", False)):
                    self._complete_shutdown()
                    return
                self.scheduler = scheduler
                self._started  = True
                raise
            except BaseException:
                self.scheduler = scheduler
                self._started  = bool(getattr(scheduler, "running", self._started))
                raise
            self._public_shutdown_owner = scheduler
            self._started               = False
        elif self._public_shutdown_owner is not scheduler:
            raise RuntimeError("scheduler public shutdown ownership changed unexpectedly")

        loop     = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout_seconds
        while bool(getattr(scheduler, "running", False)) and loop.time() < deadline:
            await asyncio.sleep(0.01)
        if bool(getattr(scheduler, "running", False)):
            self.scheduler = scheduler
            self._started  = False
            raise RuntimeError(
                f"scheduler did not stop within {self._shutdown_timeout_seconds:.3f}s"
            )
        self._complete_shutdown()

    async def shutdown_async(self, *, wait: bool = True) -> None:
        """Stop the current scheduler, using exact drain only when verified."""

        scheduler = self.scheduler
        if scheduler is None:
            self._complete_shutdown()
            return
        cleanup_owned = (
            self._scheduler_drain is not None or self._public_shutdown_owner is scheduler
        )
        if (
            not cleanup_owned
            and not self._started
            and not bool(getattr(scheduler, "running", False))
        ):
            self._complete_shutdown()
            return

        if isinstance(scheduler, _REAL_ASYNCIO_SCHEDULER):
            drain = self._private_drain_for(scheduler)
            if drain is not None:
                self._started = False
                try:
                    await drain.shutdown_async(wait=wait)
                except BaseException:
                    self.scheduler = scheduler
                    raise
                self._complete_shutdown()
                return
        await self._shutdown_public_async(scheduler, wait=wait)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop synchronously, retaining incomplete cleanup for an async retry."""

        scheduler = self.scheduler
        if scheduler is None:
            self._complete_shutdown()
            return
        cleanup_owned = (
            self._scheduler_drain is not None or self._public_shutdown_owner is scheduler
        )
        if (
            not cleanup_owned
            and not self._started
            and not bool(getattr(scheduler, "running", False))
        ):
            self._complete_shutdown()
            return

        if isinstance(scheduler, _REAL_ASYNCIO_SCHEDULER):
            drain = self._private_drain_for(scheduler)
            if drain is not None:
                self._started = False
                try:
                    drain.shutdown(wait=wait)
                except BaseException:
                    self.scheduler = scheduler
                    raise
                self._complete_shutdown()
                return
            if self._public_shutdown_owner is scheduler:
                if bool(getattr(scheduler, "running", False)):
                    raise RuntimeError(
                        "scheduler public shutdown is still pending; "
                        "use shutdown_async() to await it"
                    )
                self._complete_shutdown()
                return

        try:
            scheduler.shutdown(wait=wait)
        except SchedulerNotRunningError:
            if not bool(getattr(scheduler, "running", False)):
                self._complete_shutdown()
                return
            self.scheduler = scheduler
            self._started  = True
            raise
        except BaseException:
            self.scheduler = scheduler
            self._started  = bool(getattr(scheduler, "running", self._started))
            raise

        if isinstance(scheduler, _REAL_ASYNCIO_SCHEDULER) and bool(
            getattr(scheduler, "running", False)
        ):
            self._public_shutdown_owner = scheduler
            self._started               = False
            raise RuntimeError(
                "APScheduler public shutdown is asynchronous; "
                "use shutdown_async() to await completion"
            )
        self._complete_shutdown()

    def add_job(
        self,
        job_id: str,
        func: Callable[..., Any],
        cron: Mapping[str, Any],
        *,
        description: str | None = None,
    ) -> None:
        self.ensure_started()
        scheduler = self.scheduler
        if scheduler is None:
            raise RuntimeError("scheduler failed to start")
        prepared = self._prepare_job(
            scheduler,
            ScheduledJobSpec(job_id, func, cron, description),
        )
        with self._job_mutation_lock:
            previous          = scheduler.get_job(job_id)
            previous_snapshot = self._snapshot_job(previous) if previous is not None else None
            try:
                self._add_prepared_job(scheduler, prepared)
            except BaseException as replacement_error:
                current = scheduler.get_job(job_id)
                if current is not previous:
                    try:
                        if previous_snapshot is None:
                            if current is not None:
                                scheduler.remove_job(job_id)
                        else:
                            self._restore_job(scheduler, previous_snapshot)
                    except BaseException as rollback_error:
                        raise RuntimeError(
                            f"failed to replace scheduled job {job_id!r}; rollback also "
                            f"failed: {type(rollback_error).__name__}: {rollback_error}"
                        ) from replacement_error
                raise
        logger.info("Scheduled job %s (%s)", job_id, description or job_id)

    def replace_prefix(
        self,
        prefix: str,
        jobs: Iterable[ScheduledJobSpec],
    ) -> None:
        """Atomically replace every owned job under ``prefix``.

        Cron triggers and job metadata are constructed before the first live
        mutation.  If an add or removal still fails, the exact previous job
        definitions are restored before the error is propagated.
        """

        if type(prefix) is not str or not prefix:
            raise ValueError("schedule prefix must be a non-empty string")
        self.ensure_started()
        scheduler = self.scheduler
        if scheduler is None:
            raise RuntimeError("scheduler failed to start")

        specs       = tuple(jobs)
        prepared    = tuple(self._prepare_job(scheduler, spec) for spec in specs)
        desired_ids = {job.job_id for job in prepared}
        if len(desired_ids) != len(prepared):
            raise ValueError(f"duplicate scheduled job id under prefix {prefix!r}")
        outside_prefix = sorted(job_id for job_id in desired_ids if not job_id.startswith(prefix))
        if outside_prefix:
            raise ValueError(
                f"scheduled job ids do not belong to prefix {prefix!r}: {outside_prefix!r}"
            )

        with self._job_mutation_lock:
            previous_jobs = {
                job.id: job for job in scheduler.get_jobs() if job.id.startswith(prefix)
            }
            previous_snapshots = {
                job_id: self._snapshot_job(job) for job_id, job in previous_jobs.items()
            }
            try:
                for job in prepared:
                    self._add_prepared_job(scheduler, job)
                for job_id in previous_jobs.keys() - desired_ids:
                    scheduler.remove_job(job_id)
            except BaseException as replacement_error:
                rollback_errors = self._rollback_prefix(
                    scheduler,
                    prefix,
                    previous_jobs,
                    previous_snapshots,
                )
                if rollback_errors:
                    details = "; ".join(
                        f"{type(error).__name__}: {error}" for error in rollback_errors
                    )
                    raise RuntimeError(
                        f"failed to replace scheduled jobs under {prefix!r}; rollback also "
                        f"failed: {details}"
                    ) from replacement_error
                raise

        logger.info(
            "Replaced %d scheduled job(s) under prefix %s",
            len(prepared),
            prefix,
        )

    @staticmethod
    def _prepare_job(scheduler: AsyncIOScheduler, spec: ScheduledJobSpec) -> _PreparedJob:
        if not isinstance(spec, ScheduledJobSpec):
            raise TypeError("jobs must contain ScheduledJobSpec values")
        if type(spec.job_id) is not str or not spec.job_id.strip():
            raise ValueError("scheduled job id must be a non-empty string")
        if not callable(spec.func):
            raise TypeError(f"scheduled job {spec.job_id!r} handler must be callable")
        if not isinstance(spec.cron, Mapping):
            raise TypeError(f"scheduled job {spec.job_id!r} cron must be a mapping")
        if spec.description is not None and type(spec.description) is not str:
            raise TypeError(f"scheduled job {spec.job_id!r} description must be a string")
        cron = dict(spec.cron)
        if any(type(key) is not str for key in cron):
            raise TypeError(f"scheduled job {spec.job_id!r} cron keys must be strings")
        cron.setdefault("timezone", scheduler.timezone)
        trigger = CronTrigger(**cron)
        return _PreparedJob(spec.job_id, spec.func, trigger, spec.description)

    @staticmethod
    def _add_prepared_job(scheduler: AsyncIOScheduler, job: _PreparedJob) -> None:
        scheduler.add_job(
            job.func,
            trigger            = job.trigger,
            id                 = job.job_id,
            coalesce           = True,
            max_instances      = 1,
            misfire_grace_time = 60,
            name               = job.description or job.job_id,
            replace_existing   = True,
        )

    @staticmethod
    def _snapshot_job(job: Any) -> _JobSnapshot:
        return _JobSnapshot(
            job_id             = job.id,
            func               = job.func,
            trigger            = job.trigger,
            args               = tuple(job.args),
            kwargs             = dict(job.kwargs),
            name               = job.name,
            misfire_grace_time = job.misfire_grace_time,
            coalesce           = job.coalesce,
            max_instances      = job.max_instances,
            next_run_time      = job.next_run_time,
            executor           = job.executor,
        )

    @staticmethod
    def _restore_job(scheduler: AsyncIOScheduler, snapshot: _JobSnapshot) -> None:
        scheduler.add_job(
            snapshot.func,
            trigger            = snapshot.trigger,
            args               = snapshot.args,
            kwargs             = snapshot.kwargs,
            id                 = snapshot.job_id,
            name               = snapshot.name,
            misfire_grace_time = snapshot.misfire_grace_time,
            coalesce           = snapshot.coalesce,
            max_instances      = snapshot.max_instances,
            next_run_time      = snapshot.next_run_time,
            executor           = snapshot.executor,
            replace_existing   = True,
        )

    def _rollback_prefix(
        self,
        scheduler: AsyncIOScheduler,
        prefix: str,
        previous_jobs: Mapping[str, Any],
        previous_snapshots: Mapping[str, _JobSnapshot],
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for job_id, snapshot in previous_snapshots.items():
            if scheduler.get_job(job_id) is previous_jobs[job_id]:
                continue
            try:
                self._restore_job(scheduler, snapshot)
            except BaseException as error:
                errors.append(error)
        for job in tuple(scheduler.get_jobs()):
            if not job.id.startswith(prefix) or job.id in previous_snapshots:
                continue
            try:
                scheduler.remove_job(job.id)
            except BaseException as error:
                errors.append(error)
        return errors

    def remove_job(self, job_id: str) -> None:
        if not self.scheduler:
            return
        with self._job_mutation_lock:
            try:
                self.scheduler.remove_job(job_id)
            except JobLookupError:
                return
