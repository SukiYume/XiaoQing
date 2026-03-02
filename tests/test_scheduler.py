"""
SchedulerManager 单元测试
"""

import asyncio
import pytest
import pytest_asyncio
from datetime import datetime
from typing import Any

from core.scheduler import SchedulerManager

# ============================================================
# Fixtures
# ============================================================

@pytest_asyncio.fixture
async def scheduler():
    """创建 SchedulerManager 实例"""
    manager = SchedulerManager()
    yield manager
    # 清理
    manager.scheduler.shutdown()

# ============================================================
# 初始化测试
# ============================================================

class TestSchedulerManagerInit:
    """SchedulerManager 初始化测试"""

    @pytest.mark.asyncio
    async def test_initialization_default_timezone(self):
        """测试默认时区初始化"""
        manager = SchedulerManager()
        assert str(manager.scheduler.timezone) == "Asia/Shanghai"
        manager.scheduler.shutdown()

    @pytest.mark.asyncio
    async def test_initialization_custom_timezone(self):
        """测试自定义时区初始化"""
        manager = SchedulerManager(timezone="UTC")
        assert str(manager.scheduler.timezone) == "UTC"
        manager.scheduler.shutdown()

    @pytest.mark.asyncio
    async def test_scheduler_is_running(self, scheduler: SchedulerManager):
        """测试调度器正在运行"""
        assert scheduler.scheduler.running

    @pytest.mark.asyncio
    async def test_ensure_started_thread_safe_single_init(self, monkeypatch):
        """并发 ensure_started 只初始化一次"""
        from core import scheduler as scheduler_module

        init_count = 0
        start_count = 0

        class FakeScheduler:
            def __init__(self, timezone):
                nonlocal init_count
                init_count += 1
                self.timezone = timezone
                self.running = False

            def start(self):
                nonlocal start_count
                start_count += 1
                self.running = True

        monkeypatch.setattr(scheduler_module, "AsyncIOScheduler", FakeScheduler)

        manager = SchedulerManager()
        manager.scheduler = None
        manager._started = False
        init_count = 0
        start_count = 0

        await asyncio.gather(*(asyncio.to_thread(manager.ensure_started) for _ in range(10)))

        assert init_count == 1
        assert start_count == 1

# ============================================================
# add_job 测试
# ============================================================

class TestAddJob:
    """add_job 方法测试"""

    @pytest.mark.asyncio
    async def test_add_cron_job(self, scheduler: SchedulerManager):
        """测试添加 cron 任务"""
        executed = []

        def job_func():
            executed.append(datetime.now())

        scheduler.add_job(
            "test_job",
            job_func,
            {"second": "*/1"}  # 每秒执行
        )

        # 验证任务已添加
        jobs = scheduler.scheduler.get_jobs()
        assert any(job.id == "test_job" for job in jobs)

    @pytest.mark.asyncio
    async def test_add_job_removes_existing(self, scheduler: SchedulerManager):
        """测试添加任务时移除同名任务"""
        count1 = []

        def job1():
            count1.append(1)

        count2 = []

        def job2():
            count2.append(2)

        # 添加第一个任务
        scheduler.add_job("duplicate", job1, {"second": "*/1"})

        # 添加同名任务（应该替换）
        scheduler.add_job("duplicate", job2, {"second": "*/1"})

        # 等待执行
        await asyncio.sleep(1.5)

        # 只有第二个任务应该执行
        assert len(count2) > 0

    @pytest.mark.asyncio
    async def test_add_multiple_jobs(self, scheduler: SchedulerManager):
        """测试添加多个任务"""
        executed = []

        def job_func(name):
            executed.append(name)

        scheduler.add_job("job1", lambda: job_func("job1"), {"second": "*/1"})
        scheduler.add_job("job2", lambda: job_func("job2"), {"second": "*/2"})

        jobs = scheduler.scheduler.get_jobs()
        job_ids = [job.id for job in jobs]
        assert "job1" in job_ids
        assert "job2" in job_ids

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

    @pytest.mark.asyncio
    async def test_remove_job_twice(self, scheduler: SchedulerManager):
        """测试移除同一任务两次"""
        def dummy():
            pass

        scheduler.add_job("job", dummy, {"second": "*/1"})
        scheduler.remove_job("job")
        # 第二次移除应该不报错
        scheduler.remove_job("job")

# ============================================================
# clear_prefix 测试
# ============================================================

class TestClearPrefix:
    """clear_prefix 方法测试"""

    @pytest.mark.asyncio
    async def test_clear_prefix_removes_matching_jobs(self, scheduler: SchedulerManager):
        """测试清除指定前缀的任务"""
        def dummy():
            pass

        scheduler.add_job("plugin1.job1", dummy, {"second": "*/1"})
        scheduler.add_job("plugin1.job2", dummy, {"second": "*/1"})
        scheduler.add_job("plugin2.job1", dummy, {"second": "*/1"})
        scheduler.add_job("standalone", dummy, {"second": "*/1"})

        scheduler.clear_prefix("plugin1.")

        jobs = scheduler.scheduler.get_jobs()
        job_ids = [job.id for job in jobs]

        assert "plugin1.job1" not in job_ids
        assert "plugin1.job2" not in job_ids
        assert "plugin2.job1" in job_ids
        assert "standalone" in job_ids

    @pytest.mark.asyncio
    async def test_clear_prefix_removes_all_with_prefix(self, scheduler: SchedulerManager):
        """测试清除所有带前缀的任务"""
        def dummy():
            pass

        for i in range(5):
            scheduler.add_job(f"prefix.job{i}", dummy, {"second": "*/1"})

        scheduler.clear_prefix("prefix.")

        job_ids = [job.id for job in scheduler.scheduler.get_jobs()]
        assert not any(id.startswith("prefix.") for id in job_ids)

    @pytest.mark.asyncio
    async def test_clear_prefix_empty_prefix(self, scheduler: SchedulerManager):
        """测试空前缀（应该移除所有任务）"""
        def dummy():
            pass

        scheduler.add_job("job1", dummy, {"second": "*/1"})
        scheduler.add_job("job2", dummy, {"second": "*/1"})

        scheduler.clear_prefix("")

        # 空前缀意味着所有任务都匹配
        job_ids = [job.id for job in scheduler.scheduler.get_jobs()]
        assert len(job_ids) == 0

# ============================================================
# 任务执行测试
# ============================================================

class TestJobExecution:
    """任务执行测试"""

    @pytest.mark.asyncio
    async def test_job_executes_on_schedule(self, scheduler: SchedulerManager):
        """测试任务按计划执行"""
        executed = []

        def job():
            executed.append(datetime.now())

        scheduler.add_job("frequent_job", job, {"second": "*/1"})

        # 等待至少执行一次
        await asyncio.sleep(1.5)

        assert len(executed) >= 1

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

        await asyncio.sleep(2.5)

        assert results["a"] >= 2
        assert results["b"] >= 1

# ============================================================
# Cron 表达式测试
# ============================================================

class TestCronExpressions:
    """Cron 表达式测试"""

    @pytest.mark.asyncio
    async def test_cron_every_second(self, scheduler: SchedulerManager):
        """测试每秒执行"""
        def dummy():
            pass

        scheduler.add_job("every_second", dummy, {"second": "*"})

        job = [j for j in scheduler.scheduler.get_jobs() if j.id == "every_second"][0]
        assert job is not None
        assert job.id == "every_second"

    @pytest.mark.asyncio
    async def test_cron_every_minute(self, scheduler: SchedulerManager):
        """测试每分钟执行"""
        def dummy():
            pass

        scheduler.add_job("every_minute", dummy, {"minute": "*"})

        job = [j for j in scheduler.scheduler.get_jobs() if j.id == "every_minute"][0]
        assert job is not None

    @pytest.mark.asyncio
    async def test_cron_specific_hour(self, scheduler: SchedulerManager):
        """测试特定小时执行"""
        def dummy():
            pass

        scheduler.add_job("daily_at_9", dummy, {"hour": "9", "minute": "0"})

        job = [j for j in scheduler.scheduler.get_jobs() if j.id == "daily_at_9"][0]
        assert job is not None

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
