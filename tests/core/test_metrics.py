"""core/metrics.py 的执行统计与汇总快照。"""

import asyncio
import logging

import pytest

from core.metrics import ExecutionStats, MetricsCollector


@pytest.mark.unit
def test_execution_stats_record_and_rates():
    stats = ExecutionStats()
    assert stats.avg_time == 0.0 and stats.success_rate == 1.0
    assert stats.to_dict()["min_time"] == 0

    stats.record(1.0)
    stats.record(6.0, slow_threshold=5.0)
    stats.record(2.0, is_error=True)
    stats.record(3.0, is_error=True, is_cancelled=True)

    assert stats.total_calls == 4
    assert stats.total_time == 12.0
    assert stats.min_time == 1.0 and stats.max_time == 6.0
    assert stats.slow_calls == 1
    assert stats.errors == 1 and stats.cancelled == 1
    assert stats.avg_time == 3.0
    assert stats.success_rate == 0.5
    assert stats.to_dict() == {
        "total_calls" : 4,
        "total_time"  : 12.0,
        "avg_time"    : 3.0,
        "min_time"    : 1.0,
        "max_time"    : 6.0,
        "slow_calls"  : 1,
        "errors"      : 1,
        "cancelled"   : 1,
        "success_rate": 0.5,
    }


@pytest.mark.asyncio
@pytest.mark.unit
async def test_collector_summary_counts_plugins_commands_and_slowest():
    collector = MetricsCollector()
    await collector.record_plugin_execution("fast", "cmd", 0.1)
    await collector.record_plugin_execution("slow", "cmd", 3.0)
    await collector.record_plugin_execution("slow", "other", 1.0, is_error=True)

    summary = await collector.get_summary()
    assert summary == collector.summary_snapshot() | {"uptime_seconds": summary["uptime_seconds"]}
    assert summary["plugins_count"] == 2
    assert summary["commands_count"] == 3
    assert summary["global"]["total_calls"] == 3
    assert summary["global"]["errors"] == 1
    assert [item["plugin"] for item in summary["top_slow_plugins"]] == ["slow", "fast"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_collector_logs_slow_calls(caplog):
    collector = MetricsCollector(slow_threshold=0.5)
    with caplog.at_level(logging.WARNING, logger="core.metrics"):
        await collector.record_plugin_execution("plugin", "cmd", 1.0)
    assert "Slow plugin execution: plugin.cmd" in caplog.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_collector_concurrent_records_are_all_counted():
    collector = MetricsCollector()

    async def record_many():
        for index in range(10):
            await collector.record_plugin_execution(f"plugin_{index % 3}", f"cmd_{index % 2}", 0.1)

    await asyncio.gather(*(record_many() for _ in range(5)))
    assert collector.summary_snapshot()["global"]["total_calls"] == 50
