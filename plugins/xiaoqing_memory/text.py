"""记忆插件共用的文字工具：空白归一、截断、相邻两字重合打分。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SPACE_RE = re.compile(r"\s+")
_WORD_RE  = re.compile(r"[a-z0-9_]{2,}")
_HAN_RE   = re.compile(r"[一-鿿]+")
# 太常见、不说明话题的相邻两字。
_STOP_BIGRAMS = frozenset(
    {"自己", "这个", "一个", "时候", "可以", "就是", "什么", "我们", "你们", "他们", "没有", "不是"}
)


def squash(text: object) -> str:
    """把连续空白收成一个空格并去掉首尾空白。"""

    return _SPACE_RE.sub(" ", str(text or "")).strip()


def compact(text: object) -> str:
    """去掉全部空白，用来比较摘录和原文。"""

    return _SPACE_RE.sub("", str(text or ""))


def clip(text: object, limit: int) -> str:
    value = squash(text)
    if limit <= 0:
        return ""
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def tokens(text: object) -> set[str]:
    """英文和数字取整词，汉字取相邻两字。"""

    value = str(text or "").casefold()
    found = set(_WORD_RE.findall(value))
    for phrase in _HAN_RE.findall(value):
        if len(phrase) == 1:
            found.add(phrase)
        found.update(phrase[i : i + 2] for i in range(len(phrase) - 1))
    return found - _STOP_BIGRAMS


def overlap(left: object, right: object) -> int:
    return len(tokens(left) & tokens(right))


def similarity(left: object, right: object) -> float:
    """两段文字的相邻两字重合度，按较短一方归一。"""

    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def quote_in(quote: object, source: object) -> bool:
    """摘录是不是原文里的连续片段（空白归一后比较）。"""

    needle = compact(quote)
    return bool(needle) and needle in compact(source)


def clamped(value: Any, default: float, low: float, high: float) -> float:
    """转成数并夹在 [low, high] 里；不是数或是 NaN 时给 ``default``。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return min(high, max(low, number))


def mapping(value: Any) -> dict[str, Any]:
    """是映射就复制成字典，否则给空字典。"""

    return dict(value) if isinstance(value, Mapping) else {}


def listing(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def plain_fields(value: Any) -> dict[str, Any]:
    """调用方给的附加字段里能原样存下的：标量，以及字符串或整数组成的列表；键名截到 32 字。"""

    return {
        str(key)[:32]: item
        for key, item in mapping(value).items()
        if isinstance(item, (str, int, float, bool))
        or (isinstance(item, list) and all(isinstance(entry, (str, int)) for entry in item))
    }
