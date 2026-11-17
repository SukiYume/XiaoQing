"""一轮对话各阶段的耗时记录。"""

from __future__ import annotations

from typing import Any


def accumulate_timing(timings: dict[str, Any], key: str, seconds: float) -> None:
    """把一段耗时累加到本轮耗时记录。"""
    timings[key] = round(float(timings.get(key, 0.0)) + seconds, 3)


def append_timing(timings: dict[str, Any], key: str, value: Any) -> None:
    """逐次记录：按 1、2、3… 编号存进子字典，每一次都能单独看到。"""
    bucket                       = timings.setdefault(key, {})
    bucket[str(len(bucket) + 1)] = value
