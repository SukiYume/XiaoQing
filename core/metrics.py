"""性能监控：按插件、命令和全局三个维度统计执行耗时、慢调用与失败次数。"""

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


### 一组调用的累计统计
@dataclass
class ExecutionStats:
    """执行统计数据"""

    total_calls: int   = 0
    total_time : float = 0.0
    min_time   : float = float("inf")
    max_time   : float = 0.0
    slow_calls : int   = 0  # 超过阈值的调用次数
    errors     : int   = 0
    cancelled  : int   = 0

    def record(
        self,
        duration: float,
        slow_threshold: float = 5.0,
        is_error: bool        = False,
        is_cancelled: bool    = False,
    ) -> None:

        self.total_calls += 1
        self.total_time += duration
        self.min_time = min(self.min_time, duration)
        self.max_time = max(self.max_time, duration)
        if duration > slow_threshold:
            self.slow_calls += 1
        # 取消与普通错误分开计数
        if is_cancelled:
            self.cancelled += 1
        elif is_error:
            self.errors += 1

    @property
    def avg_time(self) -> float:
        return self.total_time / self.total_calls if self.total_calls else 0.0

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return (self.total_calls - self.errors - self.cancelled) / self.total_calls

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_calls" : self.total_calls,
            "total_time"  : round(self.total_time, 3),
            "avg_time"    : round(self.avg_time, 3),
            "min_time"    : round(self.min_time, 3) if self.min_time != float("inf") else 0,
            "max_time"    : round(self.max_time, 3),
            "slow_calls"  : self.slow_calls,
            "errors"      : self.errors,
            "cancelled"   : self.cancelled,
            "success_rate": round(self.success_rate, 4),
        }


### 性能指标收集器
class MetricsCollector:
    """
    公开方法保留 async 签名，因为 ``PluginContext.metrics`` 是插件接口，调用方都会 await。
    统计只做短小的内存操作，用线程锁保护，健康检查也能同步读取快照；持锁期间不得 await 或做 I/O。
    """

    def __init__(self, slow_threshold: float = 5.0):

        self._slow_threshold                           = slow_threshold  # 慢调用阈值（秒）
        self._plugin_stats: dict[str, ExecutionStats]  = defaultdict(ExecutionStats)
        self._command_stats: dict[str, ExecutionStats] = defaultdict(ExecutionStats)
        self._global_stats                             = ExecutionStats()
        self._start_time                               = time.time()
        self._lock                                     = threading.Lock()

    @property
    def uptime(self) -> float:
        return time.time() - self._start_time

    async def record_plugin_execution(
        self,
        plugin_name: str,
        command_name: str,
        duration: float,
        is_error: bool     = False,
        is_cancelled: bool = False,
    ) -> None:
        """同时计入插件、命令（plugin.command）和全局三份统计。"""

        with self._lock:
            for stats in (
                self._plugin_stats[plugin_name],
                self._command_stats[f"{plugin_name}.{command_name}"],
                self._global_stats,
            ):
                stats.record(duration, self._slow_threshold, is_error, is_cancelled)
        if duration > self._slow_threshold:
            logger.warning(
                "Slow plugin execution: %s.%s took %.2fs", plugin_name, command_name, duration
            )

    def summary_snapshot(self) -> dict[str, Any]:
        """同步获取汇总快照，附带平均耗时最长的 5 个插件。"""

        with self._lock:
            slowest = sorted(
                self._plugin_stats.items(), key=lambda item: item[1].avg_time, reverse=True
            )[:5]
            return {
                "uptime_seconds"  : round(self.uptime, 1),
                "global"          : self._global_stats.to_dict(),
                "plugins_count"   : len(self._plugin_stats),
                "commands_count"  : len(self._command_stats),
                "top_slow_plugins": [
                    {"plugin": name, "avg_time": round(stats.avg_time, 3)}
                    for name, stats in slowest
                    if stats.total_calls > 0
                ],
            }

    async def get_summary(self) -> dict[str, Any]:
        return self.summary_snapshot()


__all__ = ["ExecutionStats", "MetricsCollector"]
