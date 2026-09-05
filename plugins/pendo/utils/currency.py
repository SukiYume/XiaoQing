"""账目币种边界：无汇率时分别统计，各币种金额保持独立。"""

from collections import defaultdict
from collections.abc import Iterable
from typing import TypeVar

from ..models.item import Item

T = TypeVar("T", bound=Item)


def currency_code(value: object) -> str:
    """旧记录的空币种按历史默认人民币解释。"""
    return str(value or "CNY").strip().upper() or "CNY"


def currency_label(value: object) -> str:
    """人民币保留既有符号，其他币种使用无歧义的 ISO 代码。"""
    code = currency_code(value)
    return "¥" if code == "CNY" else f"{code} "


def group_by_currency(items: Iterable[T]) -> dict[str, list[T]]:
    """按币种分组后再聚合，禁止先相加再补单位。"""
    groups: dict[str, list[T]] = defaultdict(list)
    for item in items:
        groups[currency_code(getattr(item, "currency", None))].append(item)
    return dict(sorted(groups.items()))
