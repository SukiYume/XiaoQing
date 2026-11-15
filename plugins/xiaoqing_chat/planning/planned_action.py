"""定义一次回复规划交给生成的不可变数据。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlannedAction:
    think_level: int
    reasoning  : str
