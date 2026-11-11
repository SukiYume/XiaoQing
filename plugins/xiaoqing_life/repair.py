"""一次性的数据修复：2026-11-07 起的生产数据里、按现在的规则不该有的记录。

每项修复在 ``state.repairs`` 里记一次名字，只跑一次。生产上跑过之后（下一次发布时）整个模块可以删掉。

- ``life_day_v1``：日记日期、睡前整理的日子原来按日历记（零点半写的日记记成第二天），按写的时刻重算；
  不改的话部署后第一晚会以为“今天已经整理过了”。
- ``done_on_v1``：``wound_down_day`` / ``followup_day`` / ``method_day`` / ``verify_day`` 收进 ``done_on``。
- ``research_evidence_v1``：研究搜出来的条目原来也给它们的话题记兴趣证据、长好奇边界；生产里一个锅具
  问题因此堆出了“这两周常刷到锅具 / 厨房安全 / 水滴测试”的快照。删掉这些证据、靠它们才成立的快照和
  来自研究经历的好奇边界。
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from .days import day_key
from .lifecycle import refresh_interests
from .stores import LifeStore

LIFE_DAY          = "life_day_v1"
DONE_ON           = "done_on_v1"
RESEARCH_EVIDENCE = "research_evidence_v1"
RESEARCH_ACTIVITY = "thread_continue"
OLD_DAILY_KEYS    = {
    "wound_down_day": "wind_down",
    "followup_day"  : "followup",
    "method_day"    : "method",
    "verify_day"    : "verify",
}


def recompute_life_days(
    state: dict[str, Any], journal: list[dict[str, Any]], tz: tzinfo | None
) -> None:
    latest = 0.0
    for entry in journal:
        if entry.get("kind") == "diary" and entry.get("at"):
            at           = float(entry["at"])
            entry["day"] = day_key(datetime.fromtimestamp(at, tz))
            latest       = max(latest, at)
    if latest:
        state["wound_down_day"] = day_key(datetime.fromtimestamp(latest, tz))
        diary                   = state.get("diary")
        if isinstance(diary, dict) and diary.get("day"):
            diary["day"] = state["wound_down_day"]


def fold_daily_keys(state: dict[str, Any]) -> None:
    done: dict[str, str] = state.setdefault("done_on", {})
    for old, name in OLD_DAILY_KEYS.items():
        value = state.pop(old, "")
        if value and name not in done:
            done[name] = str(value)


def _supported(snapshot: dict[str, Any], interests: dict[str, dict[str, Any]]) -> bool:
    """快照时段里还剩的正向证据够不够它的档位。"""

    entry = interests.get(str(snapshot.get("topic", "")))
    if entry is None:
        return False
    since, until = float(snapshot.get("since", 0.0)), float(snapshot.get("until", 0.0))
    items = {
        e.get("item")
        for e in entry.get("evidence", [])
        if e.get("delta", 0) > 0 and since <= float(e.get("at", 0.0)) <= until
    }
    return len(items) >= int(snapshot.get("tier", 0))


def drop_research_evidence(store: LifeStore, now: float) -> None:
    research = {e["id"] for e in store.episodes if e.get("activity") == RESEARCH_ACTIVITY}
    if not research:
        return
    for entry in store.interests.values():
        entry["evidence"] = [
            e for e in entry.get("evidence", []) if e.get("episode") not in research
        ]
    store.frontier[:] = [f for f in store.frontier if f.get("from_episode") not in research]
    refresh_interests(store.interests, now)
    store.snapshots[:] = [
        s for s in store.snapshots if s.get("kind") == "tone" or _supported(s, store.interests)
    ]


def run(store: LifeStore, tz: tzinfo | None, now: float) -> bool:
    """在内存里修；返回是否改了（调用方随下一次落盘保存）。顺序固定：先重算日子，再收标记。"""

    done: list[str] = store.state.setdefault("repairs", [])
    pending         = [name for name in (LIFE_DAY, DONE_ON, RESEARCH_EVIDENCE) if name not in done]
    for name in pending:
        if name == LIFE_DAY:
            recompute_life_days(store.state, store.journal, tz)
        elif name == DONE_ON:
            fold_daily_keys(store.state)
        else:
            drop_research_evidence(store, now)
        done.append(name)
    return bool(pending)
