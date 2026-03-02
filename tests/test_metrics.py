"""
Tests for core/metrics.py - MetricsCollector and related classes
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from core.metrics import (
    ExecutionStats,
    MetricsCollector,
    ExecutionTimer,
    timed_async,
    get_metrics_collector,
    set_metrics_collector,
)


# ============================================================
# ExecutionStats Tests
# ============================================================

@pytest.mark.unit
def test_execution_stats_initialization():
    """Test ExecutionStats initial values"""
    stats = ExecutionStats()

    assert stats.total_calls == 0
    assert stats.total_time == 0.0
    assert stats.min_time == float("inf")
    assert stats.max_time == 0.0
    assert stats.slow_calls == 0
    assert stats.errors == 0
    assert stats.last_call_time == 0.0


@pytest.mark.unit
def test_execution_stats_record_single():
    """Test recording a single execution"""
    stats = ExecutionStats()
    stats.record(0.5, slow_threshold=5.0, is_error=False)

    assert stats.total_calls == 1
    assert stats.total_time == 0.5
    assert stats.min_time == 0.5
    assert stats.max_time == 0.5
    assert stats.slow_calls == 0
    assert stats.errors == 0
    assert stats.last_call_time > 0


@pytest.mark.unit
def test_execution_stats_record_multiple():
    """Test recording multiple executions"""
    stats = ExecutionStats()
    stats.record(0.1)
    stats.record(0.5)
    stats.record(1.0)

    assert stats.total_calls == 3
    assert stats.total_time == 1.6
    assert stats.min_time == 0.1
    assert stats.max_time == 1.0


@pytest.mark.unit
def test_execution_stats_slow_calls():
    """Test slow call tracking"""
    stats = ExecutionStats()
    stats.record(1.0, slow_threshold=2.0)  # Not slow
    stats.record(3.0, slow_threshold=2.0)  # Slow
    stats.record(5.0, slow_threshold=2.0)  # Slow

    assert stats.slow_calls == 2


@pytest.mark.unit
def test_execution_stats_errors():
    """Test error tracking"""
    stats = ExecutionStats()
    stats.record(0.5, is_error=False)
    stats.record(0.3, is_error=True)
    stats.record(0.7, is_error=True)

    assert stats.errors == 2


@pytest.mark.unit
def test_execution_stats_avg_time():
    """Test average time calculation"""
    stats = ExecutionStats()
    stats.record(1.0)
    stats.record(2.0)
    stats.record(3.0)

    assert stats.avg_time == 2.0


@pytest.mark.unit
def test_execution_stats_avg_time_no_calls():
    """Test average time with no calls"""
    stats = ExecutionStats()

    assert stats.avg_time == 0.0


@pytest.mark.unit
def test_execution_stats_success_rate():
    """Test success rate calculation"""
    stats = ExecutionStats()
    stats.record(0.5, is_error=False)
    stats.record(0.5, is_error=False)
    stats.record(0.5, is_error=True)

    assert stats.success_rate == 2/3


@pytest.mark.unit
def test_execution_stats_success_rate_no_errors():
    """Test success rate with no errors"""
    stats = ExecutionStats()
    stats.record(0.5)
    stats.record(0.5)

    assert stats.success_rate == 1.0


@pytest.mark.unit
def test_execution_stats_success_rate_no_calls():
    """Test success rate with no calls"""
    stats = ExecutionStats()

    assert stats.success_rate == 1.0


@pytest.mark.unit
def test_execution_stats_to_dict():
    """Test converting stats to dict"""
    stats = ExecutionStats()
    stats.record(0.5, slow_threshold=2.0)
    stats.record(1.5, slow_threshold=2.0)
    stats.record(3.0, slow_threshold=2.0)  # Slow with threshold 2.0
    stats.record(0.2, is_error=True)

    data = stats.to_dict()

    assert data["total_calls"] == 4
    assert data["total_time"] == 5.2
    assert data["avg_time"] == 1.3
    assert data["min_time"] == 0.2
    assert data["max_time"] == 3.0
    assert data["slow_calls"] == 1
    assert data["errors"] == 1
    assert data["success_rate"] == 0.75


@pytest.mark.unit
def test_execution_stats_to_dict_empty():
    """Test converting empty stats to dict"""
    stats = ExecutionStats()
    data = stats.to_dict()

    assert data["total_calls"] == 0
    assert data["total_time"] == 0
    assert data["avg_time"] == 0
    assert data["min_time"] == 0  # inf becomes 0
    assert data["max_time"] == 0


# ============================================================
# MetricsCollector Tests
# ============================================================

@pytest.mark.unit
def test_metrics_collector_initialization():
    """Test MetricsCollector initialization"""
    collector = MetricsCollector(slow_threshold=3.0)

    assert collector._slow_threshold == 3.0
    assert collector.uptime >= 0


@pytest.mark.unit
def test_metrics_collector_default_threshold():
    """Test MetricsCollector default slow threshold"""
    collector = MetricsCollector()

    assert collector._slow_threshold == 5.0


@pytest.mark.unit
def test_metrics_collector_lock_type():
    collector = MetricsCollector()
    assert not isinstance(collector._lock, asyncio.Lock)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_record_plugin_execution():
    """Test recording plugin execution"""
    collector = MetricsCollector()

    await collector.record_plugin_execution(
        plugin_name="test_plugin",
        command_name="test_command",
        duration=1.5,
        is_error=False,
    )

    plugin_stats = await collector.get_plugin_stats("test_plugin")
    assert plugin_stats["total_calls"] == 1
    assert plugin_stats["total_time"] == 1.5


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_record_multiple_plugins():
    """Test recording multiple plugins"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("plugin1", "cmd1", 1.0)
    await collector.record_plugin_execution("plugin2", "cmd1", 2.0)
    await collector.record_plugin_execution("plugin1", "cmd2", 0.5)

    all_stats = await collector.get_plugin_stats()
    assert "plugin1" in all_stats
    assert "plugin2" in all_stats
    assert all_stats["plugin1"]["total_calls"] == 2
    assert all_stats["plugin2"]["total_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_get_plugin_stats_single():
    """Test getting stats for single plugin"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("test_plugin", "cmd1", 1.0)
    await collector.record_plugin_execution("test_plugin", "cmd2", 2.0)

    stats = await collector.get_plugin_stats("test_plugin")

    assert stats["total_calls"] == 2
    assert stats["total_time"] == 3.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_get_plugin_stats_not_found():
    """Test getting stats for non-existent plugin"""
    collector = MetricsCollector()

    stats = await collector.get_plugin_stats("nonexistent")

    assert stats == {}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_get_command_stats():
    """Test getting command stats"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("plugin1", "cmd1", 1.0)
    await collector.record_plugin_execution("plugin1", "cmd1", 2.0)
    await collector.record_plugin_execution("plugin1", "cmd2", 0.5)

    cmd_stats = await collector.get_command_stats()

    assert "plugin1.cmd1" in cmd_stats
    assert "plugin1.cmd2" in cmd_stats
    assert cmd_stats["plugin1.cmd1"]["total_calls"] == 2
    assert cmd_stats["plugin1.cmd2"]["total_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_get_summary():
    """Test getting summary"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("plugin1", "cmd1", 1.0)
    await collector.record_plugin_execution("plugin2", "cmd1", 2.0)
    await collector.record_plugin_execution("plugin3", "cmd1", 3.0)

    summary = await collector.get_summary()

    assert "uptime_seconds" in summary
    assert "global" in summary
    assert "plugins_count" in summary
    assert "commands_count" in summary
    assert "top_slow_plugins" in summary
    assert summary["plugins_count"] == 3
    assert summary["commands_count"] == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_get_summary_top_slow():
    """Test top slow plugins in summary"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("fast", "cmd", 0.1)
    await collector.record_plugin_execution("medium", "cmd", 1.0)
    await collector.record_plugin_execution("slow", "cmd", 3.0)

    summary = await collector.get_summary()
    top_slow = summary["top_slow_plugins"]

    # Slowest should be first
    assert top_slow[0]["plugin"] == "slow"
    assert len(top_slow) <= 5


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_reset():
    """Test resetting collector"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("plugin1", "cmd1", 1.0)
    await collector.record_plugin_execution("plugin2", "cmd1", 2.0)

    await collector.reset()

    summary = await collector.get_summary()
    assert summary["plugins_count"] == 0
    assert summary["commands_count"] == 0
    assert summary["global"]["total_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_slow_call_logging(caplog):
    """Test slow call logging"""
    import logging

    collector = MetricsCollector(slow_threshold=1.0)

    with caplog.at_level(logging.WARNING):
        await collector.record_plugin_execution("test_plugin", "test_cmd", 2.0)

    # Should log warning for slow call
    assert any("Slow plugin execution" in record.message for record in caplog.records)


# ============================================================
# timed_async Decorator Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_timed_async_decorator():
    """Test timed_async decorator"""
    collector = MetricsCollector()

    @timed_async(collector, "test_plugin", "test_cmd")
    async def test_function():
        await asyncio.sleep(0.1)
        return "result"

    result = await test_function()

    assert result == "result"

    stats = await collector.get_plugin_stats("test_plugin")
    assert stats["total_calls"] == 1
    assert stats["total_time"] >= 0.1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_timed_async_decorator_with_error():
    """Test timed_async decorator tracks errors"""
    collector = MetricsCollector()

    @timed_async(collector, "test_plugin", "test_cmd")
    async def failing_function():
        await asyncio.sleep(0.05)
        raise ValueError("Test error")

    with pytest.raises(ValueError):
        await failing_function()

    stats = await collector.get_plugin_stats("test_plugin")
    assert stats["total_calls"] == 1
    assert stats["errors"] == 1


# ============================================================
# ExecutionTimer Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_execution_timer():
    """Test ExecutionTimer context manager"""
    collector = MetricsCollector()

    async with ExecutionTimer(collector, "test_plugin", "test_cmd") as timer:
        await asyncio.sleep(0.1)

    assert timer.duration >= 0.1

    stats = await collector.get_plugin_stats("test_plugin")
    assert stats["total_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_execution_timer_with_error():
    """Test ExecutionTimer tracks errors"""
    collector = MetricsCollector()

    try:
        async with ExecutionTimer(collector, "test_plugin", "test_cmd"):
            await asyncio.sleep(0.05)
            raise ValueError("Test error")
    except ValueError:
        pass

    stats = await collector.get_plugin_stats("test_plugin")
    assert stats["total_calls"] == 1
    assert stats["errors"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_execution_timer_duration():
    """Test ExecutionTimer sets duration"""
    collector = MetricsCollector()

    async with ExecutionTimer(collector, "test_plugin", "test_cmd") as timer:
        pass  # No-op

    assert timer.duration >= 0


# ============================================================
# Global Collector Tests
# ============================================================

@pytest.mark.unit
def test_get_metrics_collector_singleton():
    """Test global metrics collector is singleton"""
    collector1 = get_metrics_collector()
    collector2 = get_metrics_collector()

    assert collector1 is collector2


@pytest.mark.unit
def test_set_metrics_collector():
    """Test setting global metrics collector"""
    new_collector = MetricsCollector(slow_threshold=10.0)
    set_metrics_collector(new_collector)

    retrieved = get_metrics_collector()

    assert retrieved is new_collector
    assert retrieved._slow_threshold == 10.0


# ============================================================
# Thread Safety Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_concurrent_access():
    """Test MetricsCollector handles concurrent access"""
    collector = MetricsCollector()

    async def record_many():
        for i in range(10):
            await collector.record_plugin_execution(
                f"plugin_{i % 3}",
                f"cmd_{i % 2}",
                0.1,
            )

    # Run concurrent tasks
    tasks = [record_many() for _ in range(5)]
    await asyncio.gather(*tasks)

    summary = await collector.get_summary()
    assert summary["global"]["total_calls"] == 50  # 10 * 5


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_lock_contention():
    """Test lock works correctly under contention"""
    collector = MetricsCollector()

    async def record_and_get():
        await collector.record_plugin_execution("test", "cmd", 0.1)
        await collector.get_plugin_stats()

    tasks = [record_and_get() for _ in range(20)]
    await asyncio.gather(*tasks)

    # Should complete without deadlock
    stats = await collector.get_plugin_stats("test")
    assert stats["total_calls"] == 20


# ============================================================
# Edge Cases Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_zero_duration():
    """Test recording zero duration"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("test", "cmd", 0.0)

    stats = await collector.get_plugin_stats("test")
    assert stats["total_calls"] == 1
    assert stats["total_time"] == 0.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_negative_duration():
    """Test recording negative duration (edge case)"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("test", "cmd", -0.1)

    stats = await collector.get_plugin_stats("test")
    # Should still record
    assert stats["total_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_very_long_duration():
    """Test recording very long duration"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("test", "cmd", 9999.9)

    stats = await collector.get_plugin_stats("test")
    assert stats["max_time"] == 9999.9


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_unicode_plugin_names():
    """Test plugin names with unicode characters"""
    collector = MetricsCollector()

    await collector.record_plugin_execution("测试插件", "命令", 1.0)

    stats = await collector.get_plugin_stats("测试插件")
    assert stats["total_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_metrics_collector_uptime():
    """Test uptime calculation"""
    collector = MetricsCollector()

    initial_uptime = collector.uptime
    assert initial_uptime >= 0

    await asyncio.sleep(0.1)

    later_uptime = collector.uptime
    assert later_uptime > initial_uptime
