"""
性能监控模块

提供插件执行时间统计、消息处理监控等功能。
"""

import asyncio
import functools
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

@dataclass
class ExecutionStats:
    """执行统计数据"""
    total_calls: int = 0
    total_time: float = 0.0
    min_time: float = float("inf")
    max_time: float = 0.0
    slow_calls: int = 0  # 超过阈值的调用次数
    errors: int = 0
    last_call_time: float = 0.0
    
    def record(self, duration: float, slow_threshold: float = 5.0, is_error: bool = False) -> None:
        """记录一次执行"""
        self.total_calls += 1
        self.total_time += duration
        self.min_time = min(self.min_time, duration)
        self.max_time = max(self.max_time, duration)
        self.last_call_time = time.time()
        
        if duration > slow_threshold:
            self.slow_calls += 1
        
        if is_error:
            self.errors += 1
    
    @property
    def avg_time(self) -> float:
        """平均执行时间"""
        if self.total_calls == 0:
            return 0.0
        return self.total_time / self.total_calls
    
    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total_calls == 0:
            return 1.0
        return (self.total_calls - self.errors) / self.total_calls
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "total_calls": self.total_calls,
            "total_time": round(self.total_time, 3),
            "avg_time": round(self.avg_time, 3),
            "min_time": round(self.min_time, 3) if self.min_time != float("inf") else 0,
            "max_time": round(self.max_time, 3),
            "slow_calls": self.slow_calls,
            "errors": self.errors,
            "success_rate": round(self.success_rate, 4),
        }

class MetricsCollector:
    """
    性能指标收集器
    
    用于收集和统计插件执行、命令处理等性能数据。
    """
    
    def __init__(self, slow_threshold: float = 5.0):
        """
        Args:
            slow_threshold: 慢调用阈值（秒），超过此阈值的调用会被标记
        """
        self._slow_threshold = slow_threshold
        self._plugin_stats: dict[str, ExecutionStats] = defaultdict(ExecutionStats)
        self._command_stats: dict[str, ExecutionStats] = defaultdict(ExecutionStats)
        self._global_stats = ExecutionStats()
        self._start_time = time.time()
        self._lock = threading.Lock()
    
    @property
    def uptime(self) -> float:
        """运行时间（秒）"""
        return time.time() - self._start_time
    
    async def record_plugin_execution(
        self,
        plugin_name: str,
        command_name: str,
        duration: float,
        is_error: bool = False,
    ) -> None:
        """
        记录插件执行
        
        Args:
            plugin_name: 插件名称
            command_name: 命令名称
            duration: 执行时间（秒）
            is_error: 是否执行出错
        """
        with self._lock:
            self._plugin_stats[plugin_name].record(
                duration, self._slow_threshold, is_error
            )
            self._command_stats[f"{plugin_name}.{command_name}"].record(
                duration, self._slow_threshold, is_error
            )
            self._global_stats.record(duration, self._slow_threshold, is_error)
        
        # 慢调用日志
        if duration > self._slow_threshold:
            logger.warning(
                "Slow plugin execution: %s.%s took %.2fs",
                plugin_name, command_name, duration
            )
    
    async def get_plugin_stats(self, plugin_name: Optional[str] = None) -> dict[str, Any]:
        """
        获取插件统计数据
        
        Args:
            plugin_name: 指定插件名称，None 表示获取所有插件
        """
        with self._lock:
            if plugin_name:
                stats = self._plugin_stats.get(plugin_name)
                return stats.to_dict() if stats else {}
            return {
                name: stats.to_dict()
                for name, stats in self._plugin_stats.items()
            }
    
    async def get_command_stats(self) -> dict[str, Any]:
        """获取命令统计数据"""
        with self._lock:
            return {
                name: stats.to_dict()
                for name, stats in self._command_stats.items()
            }
    
    async def get_summary(self) -> dict[str, Any]:
        """获取汇总统计"""
        with self._lock:
            return {
                "uptime_seconds": round(self.uptime, 1),
                "global": self._global_stats.to_dict(),
                "plugins_count": len(self._plugin_stats),
                "commands_count": len(self._command_stats),
                "top_slow_plugins": self._get_top_slow_plugins(5),
            }
    
    def _get_top_slow_plugins(self, n: int = 5) -> list[dict[str, Any]]:
        """获取最慢的插件"""
        sorted_plugins = sorted(
            self._plugin_stats.items(),
            key=lambda x: x[1].avg_time,
            reverse=True
        )[:n]
        return [
            {"plugin": name, "avg_time": round(stats.avg_time, 3)}
            for name, stats in sorted_plugins
            if stats.total_calls > 0
        ]
    
    async def reset(self) -> None:
        """重置所有统计数据"""
        with self._lock:
            self._plugin_stats.clear()
            self._command_stats.clear()
            self._global_stats = ExecutionStats()
            self._start_time = time.time()
            logger.info("Metrics reset")

def timed_async(collector: MetricsCollector, plugin_name: str, command_name: str):
    """
    异步函数计时装饰器
    
    用法:
        @timed_async(metrics, "my_plugin", "my_command")
        async def my_handler(...):
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            is_error = False
            try:
                return await func(*args, **kwargs)
            except Exception:
                is_error = True
                raise
            finally:
                duration = time.perf_counter() - start
                await collector.record_plugin_execution(
                    plugin_name, command_name, duration, is_error
                )
        return wrapper
    return decorator

class ExecutionTimer:
    """
    执行计时器（上下文管理器）
    
    用法:
        async with ExecutionTimer(metrics, "plugin", "command") as timer:
            # 执行代码
            pass
        print(f"耗时: {timer.duration}s")
    """
    
    def __init__(
        self,
        collector: MetricsCollector,
        plugin_name: str,
        command_name: str,
    ):
        self.collector = collector
        self.plugin_name = plugin_name
        self.command_name = command_name
        self.start_time: float = 0
        self.duration: float = 0
        self._is_error = False
    
    async def __aenter__(self) -> "ExecutionTimer":
        self.start_time = time.perf_counter()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.duration = time.perf_counter() - self.start_time
        self._is_error = exc_type is not None
        await self.collector.record_plugin_execution(
            self.plugin_name,
            self.command_name,
            self.duration,
            self._is_error,
        )

# 全局默认收集器
_default_collector: MetricsCollector | None = None

def get_metrics_collector() -> MetricsCollector:
    """获取全局默认指标收集器"""
    global _default_collector
    if _default_collector is None:
        _default_collector = MetricsCollector()
    return _default_collector

def set_metrics_collector(collector: MetricsCollector) -> None:
    """设置全局默认指标收集器"""
    global _default_collector
    _default_collector = collector

__all__ = [
    "ExecutionStats",
    "MetricsCollector",
    "ExecutionTimer",
    "timed_async",
    "get_metrics_collector",
    "set_metrics_collector",
]
