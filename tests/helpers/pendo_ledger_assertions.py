"""账目 API 的单币种汇总契约断言。"""

from typing import Any


def assert_cny_aggregate(actual: Any, expected: dict[str, int | float]) -> None:
    """兼验选中币种、各币种汇总和已有整数分计算结果。"""
    assert actual == {**expected, "currency": "CNY", "by_currency": {"CNY": expected}}
