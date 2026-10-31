"""按人设卡〔作息〕推出角色此刻大概在做什么，以及这对说话节奏的影响。

每条作息写成“日子 时段 在做什么（忙/睡/闲）”，例如 ``周一至周五 08:00-11:50 上课（忙）``。
前面的条目优先；跨午夜的时段写在开始那天。没有条目命中时视为自由时间，不注入任何状态。
作息只是语气和节奏的底色：它不改变人物事实，也不能成为拒绝正常交流的理由。
"""

from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Availability = Literal["free", "busy", "asleep"]

_WEEKDAY_CHARS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_DAY_ALIASES   = {
    "每天" : frozenset(range(7)),
    "天天" : frozenset(range(7)),
    "工作日": frozenset(range(5)),
    "周末" : frozenset({5, 6}),
}
_TAGS: dict[str, Availability] = {"忙": "busy", "睡": "asleep", "闲": "free"}
_LINE_RE                       = re.compile(
    r"^(?P<days>\S+)\s+(?P<start>\d{1,2}:\d{2})\s*[-~～–—至到]\s*(?P<end>\d{1,2}:\d{2})\s+"
    r"(?P<activity>.+?)\s*(?:[（(](?P<tag>[忙睡闲])[)）])?\s*$"
)
_RANGE_RE = re.compile(
    r"^(?:周|星期)?([一二三四五六日天])(?:至|到|-|~|～)(?:周|星期)?([一二三四五六日天])$"
)
_LIST_RE = re.compile(
    r"^(?:周|星期)?[一二三四五六日天](?:[、,，/](?:周|星期)?[一二三四五六日天])*$"
)


@dataclass(frozen=True, slots=True)
class RoutineSlot:
    days        : frozenset[int]
    start       : int
    end         : int
    activity    : str
    availability: Availability

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start


def _parse_days(spec: str) -> frozenset[int]:
    if spec in _DAY_ALIASES:
        return _DAY_ALIASES[spec]
    if match := _RANGE_RE.match(spec):
        first, last = _WEEKDAY_CHARS[match.group(1)], _WEEKDAY_CHARS[match.group(2)]
        span = (last - first) % 7
        return frozenset((first + offset) % 7 for offset in range(span + 1))
    if _LIST_RE.match(spec):
        return frozenset(_WEEKDAY_CHARS[char] for char in spec if char in _WEEKDAY_CHARS)
    raise ValueError(f"作息条目的日子看不懂：{spec}")


def _parse_clock(value: str) -> int:
    hours, minutes = (int(part) for part in value.split(":"))
    if hours > 24 or minutes > 59 or (hours == 24 and minutes):
        raise ValueError(f"作息条目的时间不对：{value}")
    return hours * 60 + minutes


def parse_routine(lines: Sequence[str]) -> tuple[RoutineSlot, ...]:
    """解析〔作息〕条目；格式不对时报出是哪一条。"""

    slots: list[RoutineSlot] = []
    for line in lines:
        match = _LINE_RE.match(str(line or "").strip())
        if match is None:
            raise ValueError(f"作息条目要写成“日子 开始-结束 在做什么（忙/睡/闲）”：{line}")
        start, end = _parse_clock(match.group("start")), _parse_clock(match.group("end"))
        if start == end:
            raise ValueError(f"作息条目的开始和结束不能相同：{line}")
        slots.append(
            RoutineSlot(
                days         = _parse_days(match.group("days")),
                start        = start,
                end          = end % (24 * 60) if end == 24 * 60 else end,
                activity     = match.group("activity").strip(),
                availability = _TAGS.get(match.group("tag") or "", "free"),
            )
        )
    return tuple(slots)


def _covers(slot: RoutineSlot, moment: datetime) -> bool:
    minute = moment.hour * 60 + moment.minute
    today  = moment.weekday()
    if not slot.crosses_midnight:
        return today in slot.days and slot.start <= minute < slot.end
    if today in slot.days and minute >= slot.start:
        return True
    yesterday = (moment - timedelta(days=1)).weekday()
    return yesterday in slot.days and minute < slot.end


def current_slot(slots: Sequence[RoutineSlot], moment: datetime) -> RoutineSlot | None:
    """此刻命中的第一条作息；都不命中时是自由时间。"""

    return next((slot for slot in slots if _covers(slot, moment)), None)


def state_text(slot: RoutineSlot | None) -> str:
    """给回复模型的作息底色；自由时间不注入。"""

    if slot is None:
        return ""
    if slot.availability == "asleep":
        return (
            f"按作息这会儿你该在{slot.activity}。被叫到就像刚被吵醒或还没睡着那样，"
            "困一点、短一点；对方有正事时照常帮忙。"
        )
    if slot.availability == "busy":
        return (
            f"按作息你现在在{slot.activity}，抽空看的手机：回得短一点。"
            "不用每句都提自己在忙，对方问你在干嘛或这事影响回复时再顺口说。"
        )
    return (
        f"按作息你现在大概在{slot.activity}。只当语气底色，不必主动提；对方问你在干嘛时照这个说。"
    )


def participation_factor(slot: RoutineSlot | None, *, busy: float, asleep: float) -> float:
    """普通群聊插话概率的乘数；明确召唤不受影响。"""

    if slot is None or slot.availability == "free":
        return 1.0
    return asleep if slot.availability == "asleep" else busy


def delay_factor(slot: RoutineSlot | None, *, busy: float, asleep: float) -> float:
    """打字延迟的乘数：忙或睡时回得更慢。"""

    if slot is None or slot.availability == "free":
        return 1.0
    return asleep if slot.availability == "asleep" else busy


@functools.lru_cache(maxsize=8)
def _parsed_routine(lines: tuple[str, ...]) -> tuple[RoutineSlot, ...]:
    return parse_routine(lines)


def routine_slot_now(card, moment: datetime) -> RoutineSlot | None:
    """从人设卡读取作息并取此刻的时段；人设卡没写作息时返回 None。

    每条消息都会问一次，解析结果按作息原文缓存，人设卡改了才重新解析。
    """

    lines = getattr(card, "routine", ())
    if not lines:
        return None
    return current_slot(_parsed_routine(tuple(lines)), moment)
