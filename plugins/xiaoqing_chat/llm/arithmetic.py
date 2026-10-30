"""数值依据核对：从对话读出份数、折扣和单位，判断回复里的数值能否由证据一步推得。"""

from __future__ import annotations

import re
from collections.abc import Sequence, Set

_MEASURED_VALUE_RE = re.compile(
    r"(?P<number>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万半]+)\s*"
    r"(?P<unit>块钱|元钱|块|元|小时|分钟|秒钟|秒|公里|千米|厘米|毫米|米|千克|公斤|克)"
    r"(?P<jiao>(?<=[块元])(?:\d|五)(?![\d.]))?"
)


_MEASUREMENT_UNITS = {
    "块钱": ("money", 1),
    "元钱": ("money", 1),
    "块" : ("money", 1),
    "元" : ("money", 1),
    "小时": ("time", 3600),
    "分钟": ("time", 60),
    "秒钟": ("time", 1),
    "秒" : ("time", 1),
    "公里": ("length", 1000),
    "千米": ("length", 1000),
    "米" : ("length", 1),
    "厘米": ("length", 0.01),
    "毫米": ("length", 0.001),
    "千克": ("mass", 1000),
    "公斤": ("mass", 1000),
    "克" : ("mass", 1),
}


def _measurement_number(text: str) -> float:
    if text == "半":
        return 0.5
    if text[0].isdigit():
        return float(text)
    digits = dict(
        zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9), strict=True)
    )
    total = section = digit = 0
    for char in text:
        if char in digits:
            digit = digits[char]
        elif char == "万":
            total += (section + digit) * 10000
            section = digit = 0
        elif char in "十百千":
            section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
        else:
            return -1
    return float(total + section + digit)


def _measured_value(match: re.Match[str]) -> tuple[str, float]:
    dimension, scale = _MEASUREMENT_UNITS[match["unit"]]
    value = _measurement_number(match["number"])
    if value < 0:
        return dimension, -1.0
    jiao = match["jiao"]
    if jiao:
        # “13块5”“两块五”的尾数是角。
        value += (5 if jiao == "五" else int(jiao)) / 10
    return dimension, round(value * scale, 6)


_CN_NUMBER = r"[零〇一二两三四五六七八九十百]+"


# 份数：“两杯”“11份”“十一个人”；量词限于日常计价对象，不含金额和时间单位。
_COUNT_RE = re.compile(
    rf"(?P<n>\d+|{_CN_NUMBER})\s*(?:杯|份|个|件|张|瓶|盒|碗|人|位|次|袋|包|支|只|斤)"
)


# 折扣：“七折”“八五折”“7.5折”“半价”“打8折”。
_DISCOUNT_RE = re.compile(r"(?P<d>\d+(?:\.\d+)?|[一二三四五六七八九][一二三四五六七八九五]?)\s*折")


_CN_DIGITS = dict(zip("一二三四五六七八九", range(1, 10), strict=True))


def _arithmetic_factors(text: str) -> tuple[set[float], set[float]]:
    """从对话和回复里读出份数与折扣率，作为价格推导的乘数。"""
    counts: set[float] = {2.0}
    for match in _COUNT_RE.finditer(text):
        value = _measurement_number(match["n"])
        if 0 < value <= 1000:
            counts.add(value)
    rates: set[float] = set()
    if "半价" in text:
        rates.add(0.5)
    for match in _DISCOUNT_RE.finditer(text):
        raw = match["d"]
        if raw[0].isdigit():
            number = float(raw)
            rate   = number / 10 if number < 10 else number / 100
        else:
            digits = [_CN_DIGITS[char] for char in raw]
            rate   = digits[0] / 10 + (digits[1] / 100 if len(digits) > 1 else 0)
        if 0 < rate < 1:
            rates.add(rate)
    return counts, rates


def _mixed_rate_totals(value: float, count: int, rates: Set[float]) -> set[float]:
    """n 份里 k 份打折、其余原价的总价；份数很大时只算全价、全折和单份打折。"""
    if count <= 0:
        return set()
    discounted_counts = range(count + 1) if count <= 50 else (0, 1, count)
    return {
        value * (count - discounted) + value * rate * discounted
        for rate in (rates or {1.0})
        for discounted in discounted_counts
    }


def _derived_values(
    known: Sequence[float],
    *,
    dimension: str,
    counts: Set[float] = frozenset({2.0}),
    rates: Set[float]  = frozenset({0.5}),
) -> set[float]:
    """证据数值经一步日常算术可得的结果。

    所有量纲允许两项加减；价格另外允许 n 份里任意 k 份按对话里的折扣计价
    （“仅第二杯七折，其余原价”的三杯是 9×2 + 9×0.7），原价再加或减其中一项，
    以及把单价或总价按份数均分。
    """
    single = set(known)
    if dimension == "money":
        for value in known:
            single.update(value * rate for rate in rates)
            for count in counts:
                single.add(value / count)
                single.update(_mixed_rate_totals(value, int(count), rates))
    derived = set(single)
    for left in known:
        for right in single:
            derived.update((left + right, abs(left - right)))
    if dimension == "money":
        for index, left in enumerate(known):
            for right in known[index + 1 :]:
                derived.update((left + right) / count for count in counts)
        totals = [value for value in single if value not in known]
        derived.update(total / count for total in totals for count in counts)
    # 口语报价常保留到分或角。
    return {round(value, 2) for value in derived} | {round(value, 1) for value in derived}


def measured_values_are_supported(claim: str, evidence: str) -> bool:
    """事实清单中的测量值需要数值依据，容纳单位换算和简单计算。

    只比对证据中已有同类量纲的数值；证据未给出该量纲时，数字是夸张修辞还是
    虚构事实属于语义判断，交给模型的前提核对。算错的结果仍然拦截。
    """
    values: dict[str, list[float]] = {}
    for match in _MEASURED_VALUE_RE.finditer(evidence):
        dimension, value = _measured_value(match)
        if value >= 0:
            values.setdefault(dimension, []).append(value)
    counts, rates = _arithmetic_factors(f"{evidence}\n{claim}")
    for match in _MEASURED_VALUE_RE.finditer(claim):
        dimension, expected = _measured_value(match)
        if expected < 0:
            continue
        known = values.get(dimension, [])
        if not known or expected in known:
            continue
        if round(expected, 2) not in _derived_values(
            known, dimension=dimension, counts=counts, rates=rates
        ):
            return False
    return True
