"""小课题状态机：open → reviewing → done（或 parked / dropped）。打算和完成分开，没查完只能说“在查”。

小课题只由研究循环（``research``）推进：浏览时的消化只能开新课题。``reviewing`` 到 ``done`` 要睡前整理
写收获并引用经历 ID；答完或步数到上限时先进 ``reviewing``。研究卡住（stuck）或连续两次研究都没有真收获时
搁置（``parked``），过了 ``park_days`` 天可以被再拿起；搁置两次后再卡住就放弃。``done_when`` 是开题时定下的
“弄明白到什么程度就算完”，研究对着它判断答没答完，免得越查越细。同一个小课题一天只查一次（``planner``）。
"""

from __future__ import annotations

from typing import Any

from .config import LifeConfig
from .days import DAY
from .lifecycle import merge_topic
from .safety import similar
from .stores import new_id

DONE_KEEP_SECONDS = 30 * 86400
MAX_DEPTH         = 3
MAX_NOTES         = 16  # 一次研究最多 3 轮 × 3 条要点
MAX_ACTIONS       = 12
MAX_CORRECTIONS   = 4
MAX_EVENTS        = 10
MAX_PARKED        = 2
PARK_LIMIT_DAYS   = 30
MAX_PARK_COUNT    = 2
DEFAULT_PARK_WHY  = "资料不太够"
ACTIVE            = ("open", "reviewing")
# fact：事实、原理、定义、来历，先查百科 / 维基；practice：做法、体验、评测、梗怎么用，先搜 B站。
QUESTION_KINDS = ("fact", "practice")
STALL_WHY      = "连着两次都没查到新东西"


def on_thread_event(thread: dict[str, Any], kind: str, **data: Any) -> None:
    """小课题的重要变化（开 / 搁置 / 放弃 / 更正 / done）都从这一个函数出去：留一份有界的事件记录。

    主题档案不靠它推送，而是由 ``dossier.sync`` 从小课题当前状态幂等地同步。
    """

    events: list[dict[str, Any]] = thread.setdefault("events", [])
    events.append({"kind": kind, "at": data.get("now", thread.get("updated_at", 0.0))})
    del events[:-MAX_EVENTS]


def active_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in threads if t.get("status") in ACTIVE]


def parked_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in threads if t.get("status") == "parked"]


def get_thread(threads: list[dict[str, Any]], thread_id: str) -> dict[str, Any] | None:
    return next((t for t in threads if t.get("id") == thread_id), None)


def find_similar(threads: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    return next(
        (t for t in active_threads(threads) if similar(str(t.get("title", "")), title)), None
    )


def pick_topic(interests: dict[str, dict[str, Any]], topic: str, title: str, query: str) -> str:
    """话题：模型给的 > 把搜索词 / 标题并到已有兴趣 > 标题前 8 字。"""

    given = topic.strip()[:12]
    if given:
        return given
    for text in (query, title):
        merged = merge_topic(interests, text) if text.strip() else ""
        if merged and merged in interests:
            return merged
    return title.strip()[:8]


def open_thread(
    threads: list[dict[str, Any]],
    cfg: LifeConfig,
    *,
    title: str,
    why: str,
    next_query: str,
    now: float,
    parent_id: str = "",
    depth: int     = 0,
    topic: str     = "",
    done_when: str = "",
    kind: str      = "",
) -> dict[str, Any] | None:
    """名额已满或已有相近的小课题时不新开；后续问题带 ``parent_id`` 和 ``depth``（最多 3 层）。"""

    title = title.strip()[:20]
    if not title or len(active_threads(threads)) >= cfg.max_open_threads:
        return None
    if find_similar(threads, title) is not None:
        return None
    thread = {
        "id"           : new_id("t"),
        "title"        : title,
        "topic"        : topic.strip()[:12] or title[:8],
        "why"          : why.strip()[:60],
        "done_when"    : done_when.strip()[:30],
        "kind"         : kind if kind in QUESTION_KINDS else "",
        "next_query"   : (next_query.strip() or title)[:30],
        "status"       : "open",
        "steps"        : 0,
        "empty_streak" : 0,
        "notes"        : [],
        "note_seq"     : 0,
        "takeaway"     : "",
        "created_at"   : now,
        "updated_at"   : now,
        "parent_id"    : parent_id,
        "depth"        : depth,
        "open_question": "",
        "next_action"  : {},
        "actions"      : [],
        "corrections"  : [],
        "methods_used" : [],
        "park_reason"  : "",
        "resume_hint"  : "",
        "parked_until" : 0.0,
        "park_count"   : 0,
        "drop_reason"  : "",
    }
    threads.append(thread)
    on_thread_event(thread, "opened", now=now)
    return thread


# ───────────────────────────── 笔记 ─────────────────────────────


def live_notes(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """没被后来的发现取代的笔记。"""

    return [n for n in thread.get("notes", []) if not n.get("superseded_by")]


def add_finding(
    thread: dict[str, Any],
    *,
    episode_id: str,
    text: str,
    quote: str,
    revises: str,
    now: float,
) -> str:
    """研究要点 → 笔记 ``{id, episode_id, note, quote, at, kind}``；有效 ``revises`` 同时记一条更正。"""

    thread["note_seq"] = int(thread.get("note_seq", 0)) + 1
    note_id = f"n{thread['note_seq']}"
    old = next((n for n in live_notes(thread) if n.get("id") == revises), None) if revises else None
    thread["notes"].append(
        {
            "id"        : note_id,
            "episode_id": episode_id,
            "note"      : text.strip()[:80],
            "quote"     : quote.strip()[:60],
            "at"        : now,
            "kind"      : "finding",
        }
    )
    if old is not None:
        old["superseded_by"]              = note_id
        corrections: list[dict[str, Any]] = thread.setdefault("corrections", [])
        correction                        = {
            "id"        : new_id("c"),
            "before"    : str(old.get("note", ""))[:80],
            "after"     : text.strip()[:80],
            "episode_id": episode_id,
            "at"        : now,
        }
        corrections.append(correction)
        del corrections[:-MAX_CORRECTIONS]
        on_thread_event(thread, "correction", now=now, correction=correction)
    del thread["notes"][:-MAX_NOTES]
    return note_id


def record_step(thread: dict[str, Any], cfg: LifeConfig, *, new_content: bool, now: float) -> bool:
    """记一次研究：有真要点步数 +1，到上限进回看；没有就记空步。连着两次空步返回 True（调用方搁置它）。"""

    thread["updated_at"] = now
    if not new_content:
        thread["empty_streak"] = int(thread.get("empty_streak", 0)) + 1
        return bool(thread["empty_streak"] >= 2)
    thread["empty_streak"] = 0
    thread["steps"]        = int(thread.get("steps", 0)) + 1
    if thread["steps"] >= cfg.thread_max_steps:
        thread["status"] = "reviewing"
    return False


def mark_answered(thread: dict[str, Any], now: float) -> None:
    thread["updated_at"] = now
    thread["status"]     = "reviewing"


def finish(thread: dict[str, Any], takeaway: str, episode_ids: list[str], now: float) -> bool:
    """收获必须引用至少一个她自己的经历 ID，否则不能 done。"""

    known = {n.get("episode_id") for n in thread.get("notes", [])}
    if not takeaway.strip() or not any(e in known for e in episode_ids):
        return False
    thread["takeaway"]   = takeaway.strip()[:80]
    thread["status"]     = "done"
    thread["updated_at"] = now
    on_thread_event(thread, "done", now=now)
    return True


def drop(thread: dict[str, Any], now: float, reason: str = "") -> None:
    thread["status"]      = "dropped"
    thread["updated_at"]  = now
    thread["drop_reason"] = reason.strip()[:30]
    on_thread_event(thread, "dropped", now=now, reason=thread["drop_reason"])


# ───────────────────────────── 搁置 ─────────────────────────────


def park(
    threads: list[dict[str, Any]],
    thread: dict[str, Any],
    cfg: LifeConfig,
    *,
    reason: str,
    hint: str,
    now: float,
) -> str:
    """研究卡住：已搁置过 2 次就放弃（``drop_reason`` = 搁置原因）；否则搁置 ``park_days`` 天。返回新状态。"""

    why = reason.strip()[:30] or DEFAULT_PARK_WHY
    if int(thread.get("park_count", 0)) >= MAX_PARK_COUNT:
        drop(thread, now, why)
        return "dropped"
    thread["status"]       = "parked"
    thread["empty_streak"] = 0
    thread["park_reason"]  = why
    thread["resume_hint"]  = hint.strip()[:30]
    thread["parked_until"] = now + cfg.park_days * DAY
    thread["parked_at"]    = now
    thread["park_count"]   = int(thread.get("park_count", 0)) + 1
    thread["updated_at"]   = now
    on_thread_event(thread, "parked", now=now, reason=why)
    enforce_parked_cap(threads, now)
    return str(thread["status"])


def resume(thread: dict[str, Any], now: float) -> None:
    """搁置的小课题被选中续做：回到 open。"""

    if thread.get("status") == "parked":
        thread["status"]       = "open"
        thread["parked_until"] = 0.0
        thread["updated_at"]   = now


def enforce_parked_cap(threads: list[dict[str, Any]], now: float) -> None:
    """搁置的最多同时 2 个，多出的最旧的放弃。"""

    parked = sorted(parked_threads(threads), key=lambda t: float(t.get("parked_at", 0.0)))
    for thread in parked[: max(0, len(parked) - MAX_PARKED)]:
        drop(thread, now, "放不下了")


def expire_parked(threads: list[dict[str, Any]], now: float) -> None:
    """搁置超过 30 天没再拿起：放弃（“放太久了”）。"""

    for thread in parked_threads(threads):
        if (
            now - float(thread.get("parked_at", thread.get("updated_at", now)))
            > PARK_LIMIT_DAYS * DAY
        ):
            drop(thread, now, "放太久了")


def prune_threads(threads: list[dict[str, Any]], now: float) -> None:
    """结束的小课题保留 30 天，用于“我前两天查了下……”；搁置的先看是否放太久了。"""

    expire_parked(threads, now)
    threads[:] = [
        t
        for t in threads
        if t.get("status") in (*ACTIVE, "parked")
        or now - float(t.get("updated_at", 0.0)) <= DONE_KEEP_SECONDS
    ]


def protected_episode_ids(threads: list[dict[str, Any]]) -> set[str]:
    """进行中和搁置中的小课题引用的经历不过期。"""

    return {
        str(n.get("episode_id"))
        for t in threads
        if t.get("status") in (*ACTIVE, "parked")
        for n in t.get("notes", [])
        if n.get("episode_id")
    }
