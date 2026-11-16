"""
SchedulerManager 单元测试
"""

import asyncio
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
import pytest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.scheduler import (
    ScheduledJobSpec,
    SchedulerManager,
    probe_asyncio_scheduler_drain,
)
from tests.helpers.asyncio_tools import cancellation_then_release_callback

# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture
async def scheduler():
    """创建 SchedulerManager 实例"""
    manager = SchedulerManager()
    yield manager
    # 清理
    await manager.shutdown_async()


def _fire_now(manager: SchedulerManager, job_id: str) -> None:
    """把任务的下次运行时间改成现在并唤醒调度器，立即触发一次。"""

    scheduler = manager.scheduler
    assert scheduler is not None
    job = scheduler.get_job(job_id)
    assert job is not None
    job.modify(next_run_time=datetime.now(scheduler.timezone))
    scheduler.wakeup()


def _break_private_layout(scheduler: AsyncIOScheduler) -> None:
    """模拟 APScheduler 私有结构变了：执行器的待完成任务登记不再是集合。"""

    next(iter(scheduler._executors.values()))._pending_futures = []
    assert probe_asyncio_scheduler_drain(scheduler).available is False


async def _wait_until(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.005)


# ============================================================
# 初始化测试
# ============================================================


class TestSchedulerManagerInit:
    """SchedulerManager 初始化测试"""

    @pytest.mark.asyncio
    async def test_private_drain_is_selected_by_capability_not_exact_version(self):
        manager   = SchedulerManager()
        scheduler = manager.scheduler
        assert scheduler is not None

        capability = probe_asyncio_scheduler_drain(scheduler)

        assert capability.available is True
        assert capability.reason is None
        assert capability.apscheduler_version
        await manager.shutdown_async()

    @pytest.mark.asyncio
    async def test_missing_private_layout_falls_back_to_public_shutdown(self):
        manager   = SchedulerManager()
        scheduler = manager.scheduler
        assert scheduler is not None
        _break_private_layout(scheduler)

        await manager.shutdown_async()

        assert scheduler.running is False
        assert manager.scheduler is None
        assert manager._scheduler_drain is None
        capability = manager._last_drain_capability
        assert capability is not None
        assert capability.available is False
        assert capability.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param({}, "Asia/Shanghai", id="default"),
            pytest.param({"timezone": "UTC"}, "UTC", id="custom"),
        ],
    )
    async def test_initialization_timezone(self, kwargs, expected):
        """默认时区为 Asia/Shanghai，也可以自定义"""
        manager = SchedulerManager(**kwargs)
        assert str(manager.scheduler.timezone) == expected
        await manager.shutdown_async()

    @pytest.mark.asyncio
    async def test_scheduler_is_running(self, scheduler: SchedulerManager):
        """测试调度器正在运行"""
        assert scheduler.scheduler.running

    @pytest.mark.asyncio
    async def test_ensure_started_is_idempotent(self, monkeypatch):
        """重复 ensure_started 只启动一个调度器"""
        starts         = []
        original_start = AsyncIOScheduler.start

        def counting_start(scheduler, *args, **kwargs):
            starts.append(scheduler)
            return original_start(scheduler, *args, **kwargs)

        manager = SchedulerManager()
        manager.shutdown()
        monkeypatch.setattr(AsyncIOScheduler, "start", counting_start)

        for _ in range(10):
            manager.ensure_started()

        assert starts == [manager.scheduler]
        assert manager.scheduler.running is True
        manager.shutdown()

    @pytest.mark.asyncio
    async def test_reset_stops_old_scheduler_and_applies_new_timezone(self):
        """reset 返回时旧调度器已停止，新调度器按新时区运行"""
        manager = SchedulerManager()
        old     = manager.scheduler
        assert old is not None

        manager.reset("UTC")

        assert old.running is False
        assert manager.timezone == "UTC"
        assert manager.scheduler is not old
        assert manager.scheduler.running is True
        assert str(manager.scheduler.timezone) == "UTC"
        manager.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_resets_scheduler_and_allows_lazy_restart(self):
        manager = SchedulerManager()
        first   = manager.scheduler

        manager.shutdown()

        assert manager.scheduler is None
        assert manager._started is False

        manager.ensure_started()
        assert manager.scheduler is not None
        assert manager.scheduler is not first
        assert manager.scheduler.running is True
        manager.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self):
        manager   = SchedulerManager()
        scheduler = manager.scheduler
        assert scheduler is not None

        await manager.shutdown_async()
        await manager.shutdown_async()

        assert scheduler.running is False
        assert scheduler._eventloop is None
        assert manager.scheduler is None

    @pytest.mark.asyncio
    async def test_real_scheduler_cleanup_failure_is_owned_and_retried(self):
        manager   = SchedulerManager()
        scheduler = manager.scheduler
        assert scheduler is not None
        executor          = next(iter(scheduler._executors.values()))
        original_shutdown = executor.shutdown
        calls             = 0

        def fail_once(wait: bool = True) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("executor stop failed")
            original_shutdown(wait)

        executor.shutdown = Mock(side_effect=fail_once)

        with pytest.raises(RuntimeError, match="executor stop failed"):
            await manager.shutdown_async()

        drain = manager._scheduler_drain
        assert drain is not None
        assert manager.scheduler is scheduler
        assert drain.scheduler is scheduler
        assert drain.pending_executor_cleanup == [executor]
        assert scheduler._eventloop is not None

        await manager.shutdown_async()

        assert calls == 2
        assert manager.scheduler is None
        assert manager._scheduler_drain is None
        assert drain.pending_executor_cleanup == []
        assert drain.pending_jobstore_cleanup == []
        assert drain.timer_cleanup_pending is False
        assert scheduler._eventloop is None

    @pytest.mark.asyncio
    async def test_real_scheduler_resistant_job_is_retained_until_retry(self):
        manager                           = SchedulerManager()
        manager._shutdown_timeout_seconds = 0.01
        scheduler                         = manager.scheduler
        assert scheduler is not None
        executor          = next(iter(scheduler._executors.values()))
        started           = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release           = asyncio.Event()

        resistant_job = cancellation_then_release_callback(started, cancellation_seen, release)

        scheduler.add_job(
            resistant_job,
            "date",
            run_date=datetime.now(scheduler.timezone),
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        job_task = next(
            future for future in executor._pending_futures if isinstance(future, asyncio.Task)
        )

        try:
            with pytest.raises(RuntimeError, match="scheduled jobs"):
                await manager.shutdown_async()

            assert cancellation_seen.is_set()
            assert job_task.done() is False
            drain = manager._scheduler_drain
            assert drain is not None
            assert manager.scheduler is scheduler
            assert drain.scheduler is scheduler
            assert executor in drain.pending_executor_cleanup
            assert drain.pending_job_futures == {job_task}
            assert scheduler._eventloop is not None

            release.set()
            await job_task
            await manager.shutdown_async()

            assert manager.scheduler is None
            assert manager._scheduler_drain is None
            assert drain.pending_job_futures == set()
            assert scheduler._eventloop is None
        finally:
            release.set()
            if not job_task.done():
                job_task.cancel()
            await asyncio.gather(job_task, return_exceptions=True)
            if manager.scheduler is not None:
                manager._shutdown_timeout_seconds = 0.5
                await manager.shutdown_async()

    @pytest.mark.asyncio
    async def test_thread_backed_job_future_is_not_falsely_cancelled(self):
        manager                           = SchedulerManager()
        manager._shutdown_timeout_seconds = 0.01
        scheduler                         = manager.scheduler
        assert scheduler is not None
        executor = next(iter(scheduler._executors.values()))
        started  = threading.Event()
        release  = threading.Event()

        def blocking_job() -> None:
            started.set()
            release.wait()

        scheduler.add_job(blocking_job, "date")
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        job_future = next(
            future for future in executor._pending_futures if not isinstance(future, asyncio.Task)
        )

        try:
            with pytest.raises(RuntimeError, match="scheduled jobs"):
                await manager.shutdown_async()

            assert job_future.done() is False
            assert job_future.cancelled() is False
            drain = manager._scheduler_drain
            assert drain is not None
            assert manager.scheduler is scheduler
            assert drain.pending_job_futures == {job_future}

            release.set()
            await job_future
            await manager.shutdown_async()

            assert manager.scheduler is None
            assert scheduler._eventloop is None
        finally:
            release.set()
            if not job_future.done():
                await job_future
            if manager.scheduler is not None:
                manager._shutdown_timeout_seconds = 0.5
                await manager.shutdown_async()

    @pytest.mark.asyncio
    async def test_cancelled_shutdown_keeps_cleanup_gate_closed_until_retry(self):
        manager                           = SchedulerManager()
        manager._shutdown_timeout_seconds = 5
        scheduler                         = manager.scheduler
        assert scheduler is not None
        executor          = next(iter(scheduler._executors.values()))
        started           = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release           = asyncio.Event()

        resistant_job = cancellation_then_release_callback(started, cancellation_seen, release)

        scheduler.add_job(resistant_job, "date")
        await asyncio.wait_for(started.wait(), timeout=1)
        job_task = next(
            future for future in executor._pending_futures if isinstance(future, asyncio.Task)
        )

        try:
            first_shutdown = asyncio.create_task(manager.shutdown_async())
            await cancellation_seen.wait()
            first_shutdown.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_shutdown

            drain = manager._scheduler_drain
            assert drain is not None
            assert manager.scheduler is scheduler
            assert drain.scheduler is scheduler
            assert manager._started is False
            assert scheduler.running is False
            assert drain.pending_job_futures == {job_task}
            with pytest.raises(RuntimeError, match="cleanup is incomplete"):
                manager.ensure_started()
            with pytest.raises(RuntimeError, match="cleanup is incomplete"):
                manager.add_job("must-not-start", lambda: None, {"second": "*"})

            second_shutdown = asyncio.create_task(manager.shutdown_async())
            await asyncio.sleep(0)
            second_shutdown.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_shutdown

            assert manager.scheduler is scheduler
            assert manager._scheduler_drain is drain
            assert manager._started is False
            assert job_task.done() is False

            release.set()
            await job_task
            await manager.shutdown_async()

            assert manager.scheduler is None
            assert manager._scheduler_drain is None
            assert manager._started is False
            assert scheduler._eventloop is None
        finally:
            release.set()
            if not job_task.done():
                job_task.cancel()
            await asyncio.gather(job_task, return_exceptions=True)
            if manager.scheduler is not None:
                manager._shutdown_timeout_seconds = 0.5
                await manager.shutdown_async()

    @pytest.mark.asyncio
    async def test_shutdown_failure_retains_scheduler_for_retry(self, monkeypatch):
        manager   = SchedulerManager()
        scheduler = manager.scheduler
        assert scheduler is not None
        # 私有排空不可用时走 APScheduler 公开 shutdown。
        _break_private_layout(scheduler)
        original_shutdown = AsyncIOScheduler.shutdown
        calls             = 0

        def fail_once(instance, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("stop failed")
            return original_shutdown(instance, *args, **kwargs)

        monkeypatch.setattr(AsyncIOScheduler, "shutdown", fail_once)

        with pytest.raises(RuntimeError, match="stop failed"):
            manager.shutdown()

        assert manager.scheduler is scheduler
        assert manager._started is True
        assert scheduler.running is True

        await manager.shutdown_async()
        assert manager.scheduler is None
        assert manager._started is False
        assert scheduler.running is False
        assert calls == 2

    @pytest.mark.asyncio
    async def test_reset_failure_keeps_old_timezone_and_does_not_start_new_scheduler(
        self,
        monkeypatch,
    ):
        manager       = SchedulerManager("Asia/Shanghai")
        old_scheduler = manager.scheduler
        assert old_scheduler is not None
        _break_private_layout(old_scheduler)
        original_shutdown = AsyncIOScheduler.shutdown
        original_start    = AsyncIOScheduler.start
        starts            = []
        fail_shutdown     = True

        def shutdown(instance, *args, **kwargs):
            if fail_shutdown:
                raise RuntimeError("stop failed")
            return original_shutdown(instance, *args, **kwargs)

        def start(instance, *args, **kwargs):
            starts.append(instance)
            return original_start(instance, *args, **kwargs)

        monkeypatch.setattr(AsyncIOScheduler, "shutdown", shutdown)
        monkeypatch.setattr(AsyncIOScheduler, "start", start)

        with pytest.raises(RuntimeError, match="stop failed"):
            manager.reset("UTC")

        assert manager.timezone == "Asia/Shanghai"
        assert manager.scheduler is old_scheduler
        assert manager._started is True
        assert old_scheduler.running is True
        assert starts == []

        fail_shutdown = False
        await manager.shutdown_async()
        assert old_scheduler.running is False

    @pytest.mark.asyncio
    async def test_constructor_does_not_swallow_scheduler_start_failure(self, monkeypatch):
        def failing_start(instance, *args, **kwargs):
            raise RuntimeError("executor start failed")

        monkeypatch.setattr(AsyncIOScheduler, "start", failing_start)

        with pytest.raises(RuntimeError, match="executor start failed"):
            SchedulerManager()

    @pytest.mark.asyncio
    async def test_unstarted_scheduler_residue_is_abandoned_before_retry(self):
        manager = SchedulerManager()
        manager.shutdown()
        # 真实调度器对象从未启动：它不占任何资源，不该调用它的 shutdown。
        residue = AsyncIOScheduler(timezone="UTC")
        residue.shutdown = Mock(side_effect=RuntimeError("must not be called"))
        manager.scheduler = residue
        manager._started  = False

        manager.shutdown()

        residue.shutdown.assert_not_called()
        assert manager.scheduler is None
        assert manager._started is False

        manager.ensure_started()
        assert manager.scheduler is not None
        assert manager.scheduler is not residue
        assert manager.scheduler.running is True
        manager.shutdown()

    @pytest.mark.asyncio
    async def test_reset_start_failure_keeps_previous_timezone_for_same_config_retry(
        self,
        monkeypatch,
    ):
        manager       = SchedulerManager("Asia/Shanghai")
        old_scheduler = manager.scheduler
        assert old_scheduler is not None
        original_start = AsyncIOScheduler.start
        starts         = 0

        def fail_first_start(instance, *args, **kwargs):
            nonlocal starts
            starts += 1
            if starts == 1:
                raise RuntimeError("new start failed")
            return original_start(instance, *args, **kwargs)

        monkeypatch.setattr(AsyncIOScheduler, "start", fail_first_start)

        with pytest.raises(RuntimeError, match="new start failed"):
            manager.reset("UTC")

        assert manager.timezone == "Asia/Shanghai"
        assert manager.scheduler is None
        assert manager._started is False
        await asyncio.sleep(0)
        assert old_scheduler.running is False

        manager.reset("UTC")
        assert manager.timezone == "UTC"
        assert manager.scheduler is not None
        assert str(manager.scheduler.timezone) == "UTC"
        assert manager.scheduler.running is True
        assert manager._started is True
        manager.shutdown()

    @pytest.mark.asyncio
    async def test_partially_started_scheduler_is_retained_for_cleanup(self, monkeypatch):
        manager = SchedulerManager()
        manager.shutdown()
        original_start = AsyncIOScheduler.start

        def fail_after_start(instance, *args, **kwargs):
            original_start(instance, *args, **kwargs)
            raise RuntimeError("partial start")

        monkeypatch.setattr(AsyncIOScheduler, "start", fail_after_start)

        with pytest.raises(RuntimeError, match="partial start"):
            manager.ensure_started()

        candidate = manager.scheduler
        assert candidate is not None
        assert candidate.running is True
        assert manager._started is True
        manager.shutdown()
        assert candidate.running is False
        assert manager.scheduler is None


# ============================================================
# add_job 测试
# ============================================================


class TestAddJob:
    """add_job 方法测试"""

    @pytest.mark.asyncio
    async def test_add_cron_job_uses_manifest_description_as_job_name(
        self,
        scheduler: SchedulerManager,
    ):
        scheduler.add_job(
            "test_job",
            lambda: None,
            {"second": "*/1"},
            description="Manifest-visible schedule",
        )

        job = next(job for job in scheduler.scheduler.get_jobs() if job.id == "test_job")
        assert job.name == "Manifest-visible schedule"

    @pytest.mark.asyncio
    async def test_add_job_prevents_overlapping_backlog(self, scheduler: SchedulerManager):
        def job_func():
            return None

        scheduler.add_job("bounded_job", job_func, {"second": "*/1"})
        job = next(job for job in scheduler.scheduler.get_jobs() if job.id == "bounded_job")

        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 60

    @pytest.mark.asyncio
    async def test_add_job_removes_existing(self, scheduler: SchedulerManager):
        """测试添加任务时移除同名任务"""
        count1 = []

        def job1():
            count1.append(1)

        count2 = []

        def job2():
            count2.append(2)

        scheduler.add_job("duplicate", job1, {"second": "*/1"})

        # 添加同名任务（应该替换）
        scheduler.add_job("duplicate", job2, {"second": "*/1"})

        # 直接触发这个任务，不等 cron 到点
        _fire_now(scheduler, "duplicate")
        await _wait_until(lambda: count2)

        # 只有第二个任务应该执行
        assert len(count2) > 0
        assert count1 == []

    @pytest.mark.asyncio
    async def test_invalid_replacement_keeps_existing_job(self, scheduler: SchedulerManager):
        def original():
            return "original"

        scheduler.add_job("stable", original, {"minute": "*"})

        with pytest.raises(TypeError, match="unexpected keyword argument"):
            scheduler.add_job("stable", lambda: None, {"not_a_cron_field": "*"})

        job = scheduler.scheduler.get_job("stable")
        assert job is not None
        assert job.func is original

    @pytest.mark.asyncio
    async def test_add_multiple_jobs(self, scheduler: SchedulerManager):
        """测试添加多个任务"""
        executed = []

        def job_func(name):
            executed.append(name)

        scheduler.add_job("job1", lambda: job_func("job1"), {"second": "*/1"})
        scheduler.add_job("job2", lambda: job_func("job2"), {"second": "*/2"})

        jobs    = scheduler.scheduler.get_jobs()
        job_ids = [job.id for job in jobs]
        assert "job1" in job_ids
        assert "job2" in job_ids


class TestReplacePrefix:
    @pytest.mark.asyncio
    async def test_replace_prefix_removes_stale_jobs_after_validating_all(
        self,
        scheduler: SchedulerManager,
    ):
        scheduler.add_job("plugin.demo.old", lambda: None, {"minute": "*"})
        scheduler.add_job("plugin.other.keep", lambda: None, {"minute": "*"})

        def replacement():
            return None

        scheduler.replace_prefix(
            "plugin.demo.",
            [
                ScheduledJobSpec(
                    "plugin.demo.new",
                    replacement,
                    {"hour": "1"},
                    "new job",
                )
            ],
        )

        jobs = {job.id: job for job in scheduler.scheduler.get_jobs()}
        assert "plugin.demo.old" not in jobs
        assert jobs["plugin.demo.new"].func is replacement
        assert jobs["plugin.demo.new"].name == "new job"
        assert "plugin.other.keep" in jobs

    @pytest.mark.asyncio
    async def test_replace_prefix_rolls_back_partial_add_failure(
        self,
        scheduler: SchedulerManager,
        monkeypatch,
    ):
        def old_first():
            return "old-first"

        def old_second():
            return "old-second"

        scheduler.add_job("plugin.demo.first", old_first, {"minute": "1"})
        scheduler.add_job("plugin.demo.second", old_second, {"minute": "2"})
        before = {
            job.id: (job.func, str(job.trigger), job.next_run_time)
            for job in scheduler.scheduler.get_jobs()
            if job.id.startswith("plugin.demo.")
        }
        real_add_job = scheduler.scheduler.add_job
        failed       = False

        def fail_second_once(*args, **kwargs):
            nonlocal failed
            if kwargs.get("id") == "plugin.demo.second" and not failed:
                failed = True
                raise RuntimeError("injected add failure")
            return real_add_job(*args, **kwargs)

        monkeypatch.setattr(scheduler.scheduler, "add_job", fail_second_once)

        with pytest.raises(RuntimeError, match="injected add failure"):
            scheduler.replace_prefix(
                "plugin.demo.",
                [
                    ScheduledJobSpec("plugin.demo.first", lambda: None, {"hour": "3"}),
                    ScheduledJobSpec("plugin.demo.second", lambda: None, {"hour": "4"}),
                    ScheduledJobSpec("plugin.demo.new", lambda: None, {"hour": "5"}),
                ],
            )

        after = {
            job.id: (job.func, str(job.trigger), job.next_run_time)
            for job in scheduler.scheduler.get_jobs()
            if job.id.startswith("plugin.demo.")
        }
        assert after == before

    @pytest.mark.asyncio
    async def test_replace_prefix_invalid_later_cron_never_mutates_live_jobs(
        self,
        scheduler: SchedulerManager,
    ):
        def original():
            return None

        scheduler.add_job("plugin.demo.stable", original, {"minute": "7"})

        with pytest.raises(TypeError, match="unexpected keyword argument"):
            scheduler.replace_prefix(
                "plugin.demo.",
                [
                    ScheduledJobSpec("plugin.demo.stable", lambda: None, {"hour": "1"}),
                    ScheduledJobSpec(
                        "plugin.demo.invalid",
                        lambda: None,
                        {"not_a_cron_field": "*"},
                    ),
                ],
            )

        jobs = {
            job.id: job
            for job in scheduler.scheduler.get_jobs()
            if job.id.startswith("plugin.demo.")
        }
        assert list(jobs) == ["plugin.demo.stable"]
        assert jobs["plugin.demo.stable"].func is original


# ============================================================
# remove_job 测试
# ============================================================


class TestRemoveJob:
    """remove_job 方法测试"""

    @pytest.mark.asyncio
    async def test_remove_existing_job(self, scheduler: SchedulerManager):
        """测试移除已存在的任务"""

        def dummy():
            pass

        scheduler.add_job("to_remove", dummy, {"second": "*/1"})
        assert any(job.id == "to_remove" for job in scheduler.scheduler.get_jobs())

        scheduler.remove_job("to_remove")
        assert not any(job.id == "to_remove" for job in scheduler.scheduler.get_jobs())

    @pytest.mark.asyncio
    async def test_remove_nonexistent_job(self, scheduler: SchedulerManager):
        """测试移除不存在的任务（不应抛出异常）"""
        # 应该不抛出异常
        scheduler.remove_job("nonexistent_job")


# ============================================================
# 任务执行测试
# ============================================================


class TestJobExecution:
    """任务执行测试"""

    @pytest.mark.asyncio
    async def test_multiple_jobs_execute(self, scheduler: SchedulerManager):
        """测试多个任务执行"""
        results = {"a": 0, "b": 0}

        def job_a():
            results["a"] += 1

        def job_b():
            results["b"] += 1

        scheduler.add_job("job_a", job_a, {"second": "*/1"})
        scheduler.add_job("job_b", job_b, {"second": "*/2"})

        # 直接触发两个任务，不等 cron 到点
        fired_at = datetime.now(scheduler.scheduler.timezone)
        _fire_now(scheduler, "job_a")
        _fire_now(scheduler, "job_b")
        await _wait_until(lambda: results["a"] >= 1 and results["b"] >= 1)
        # 执行后任务仍按 cron 留在调度器里，可以再次触发
        _fire_now(scheduler, "job_a")
        await _wait_until(lambda: results["a"] >= 2)

        assert results["a"] >= 2
        assert results["b"] >= 1
        # 执行后按各自的 cron 节奏排下一次：a 每秒一次，b 每两秒一次
        next_a = scheduler.scheduler.get_job("job_a").next_run_time
        next_b = scheduler.scheduler.get_job("job_b").next_run_time
        assert fired_at < next_a <= fired_at + timedelta(seconds=2)
        assert fired_at < next_b <= fired_at + timedelta(seconds=3)
