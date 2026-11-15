"""整理各步共用的部分：模型调用的形状、事件写成行。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.clock import WEEKDAY_NAMES

from ..model import Event
from ..text import clip

# 提示词改版时加一；笔记和页面记下版本，改版后可以只重建旧版本的内容。
PROMPT_VERSION = 1

Complete = Callable[[list[dict[str, str]]], Awaitable[str]]


# 写着的日子过了以后，笔记再留这么久：被问到时还记得这件事发生过，之后结束。
ENDED_KEEP_DAYS = 30


def local_time(ts: float, zone: ZoneInfo) -> str:
    """带星期的本地时间，模型才能把“周五”“明天”换成具体日期。"""

    moment = datetime.fromtimestamp(ts, zone)
    return f"{moment:%m-%d} 周{WEEKDAY_NAMES[moment.weekday()]} {moment:%H:%M}"


def today_line(ts: float, zone: ZoneInfo) -> str:
    moment = datetime.fromtimestamp(ts, zone)
    return f"{moment:%Y-%m-%d} 周{WEEKDAY_NAMES[moment.weekday()]}"


def day_end(day: str, zone: ZoneInfo, *, said_at: float) -> float:
    """``YYYY-MM-DD`` 那天结束的时刻；格式不对，或者早于说这话的前一天、晚于一年以后时返回 0。"""

    try:
        parsed = datetime.strptime(str(day or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return 0.0
    said = datetime.fromtimestamp(said_at, zone).date()
    if (parsed - said).days < -1 or (parsed - said).days > 400:
        return 0.0
    return datetime.combine(parsed + timedelta(days=1), datetime.min.time(), zone).timestamp()


def event_line(event: Event, zone: ZoneInfo, bot_name: str, limit: int = 400) -> str:
    when = local_time(event.ts, zone)
    if event.kind == "task":
        who = f"{event.name or '有人'}<{event.user_id}>" if event.user_id else "有人"
        return f"e{event.seq} [{when}] （你帮{who}办的一件事）\n{clip(event.text, limit * 2)}"
    if event.role == "assistant":
        return f"e{event.seq} [{when}] {bot_name}（你）：{clip(event.text, limit)}"
    who = f"{event.name or '有人'}<{event.user_id}>" if event.user_id else (event.name or "有人")
    return f"e{event.seq} [{when}] {who}：{clip(event.text, limit)}"


def seq_of(value: Any) -> int:
    text = str(value or "").strip().lstrip("eE")
    try:
        return int(text)
    except ValueError:
        return 0
