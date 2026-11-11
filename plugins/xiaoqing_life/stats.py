"""每天的运行计数：上了几次网、各活动的结果、研究、小课题、核实、查词，以及交给聊天的内容。

排查时不用再挨个翻 JSON：跨日时记一行 ``xiaoqing_life step=daily_summary``，``/life stats`` 看最近 7 天。
只记数，不记内容；日子按 ``days.day_key``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

KEEP_DAYS = 7

# 研究 / 查词 / 浏览在 ``SessionOutcome.details`` 里报的数，原样计进当天。
_DETAIL_KEYS = ("rounds", "findings", "answered", "parked", "memes", "hits", "misses")


def bump(state: dict[str, Any], day: str, counts: Mapping[str, float]) -> None:
    if not day:
        return
    days: dict[str, dict[str, float]] = state.setdefault("stats", {})
    row                               = days.setdefault(day, {})
    for key, value in counts.items():
        if value:
            row[key] = row.get(key, 0) + value
    for old in sorted(days)[:-KEEP_DAYS]:
        del days[old]


def record_session(
    state: dict[str, Any], day: str, choice: str, status: str, reason: str, outcome: Any
) -> None:
    counts: dict[str, float] = {
        f"session.{choice}"          : 1,
        f"outcome.{reason or status}": 1,
        "episodes"                   : outcome.episodes,
        "terms_seen"                 : outcome.terms,
    }
    for key in _DETAIL_KEYS:
        value = outcome.details.get(key)
        if isinstance(value, (int, float)) and value:
            counts[f"{choice}.{key}"] = float(value)
    bump(state, day, counts)


def summary(state: dict[str, Any], day: str) -> str:
    """``key=value`` 一行，按键排序；没有记录给空串。"""

    row = state.get("stats", {}).get(day) or {}
    return " ".join(f"{key}={_fmt(value)}" for key, value in sorted(row.items()))


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def stats_text(state: dict[str, Any]) -> str:
    days = state.get("stats", {})
    if not days:
        return "还没有运行计数。"
    return "\n".join(f"{day}：{summary(state, day)}" for day in sorted(days, reverse=True))
