"""小课题状态机：open → reviewing → done（或 dropped）。打算和完成分开，没查完只能说“在查”。

``reviewing`` 到 ``done`` 要睡前整理写收获并引用经历 ID；答完或步数到上限时先进 ``reviewing``。
"""

from __future__ import annotations

from typing import Any

from .config import LifeConfig
from .planner import THREAD_COOLDOWN_SECONDS, similar
from .stores import new_id

DONE_KEEP_SECONDS = 30 * 86400
MAX_NOTES         = 8
ACTIVE            = ("open", "reviewing")


def active_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in threads if t.get("status") in ACTIVE]


def get_thread(threads: list[dict[str, Any]], thread_id: str) -> dict[str, Any] | None:
    return next((t for t in threads if t.get("id") == thread_id), None)


def find_similar(threads: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    return next(
        (t for t in active_threads(threads) if similar(str(t.get("title", "")), title)), None
    )


def open_thread(
    threads: list[dict[str, Any]],
    cfg: LifeConfig,
    *,
    title: str,
    why: str,
    next_query: str,
    now: float,
) -> dict[str, Any] | None:
    """名额已满或已有相近的小课题时不新开。"""

    title = title.strip()[:20]
    if not title or len(active_threads(threads)) >= cfg.max_open_threads:
        return None
    if find_similar(threads, title) is not None:
        return None
    thread = {
        "id"            : new_id("t"),
        "title"         : title,
        "why"           : why.strip()[:60],
        "next_query"    : (next_query.strip() or title)[:30],
        "status"        : "open",
        "steps"         : 0,
        "empty_streak"  : 0,
        "cooldown_until": 0.0,
        "notes"         : [],
        "takeaway"      : "",
        "created_at"    : now,
        "updated_at"    : now,
    }
    threads.append(thread)
    return thread


def _add_note(thread: dict[str, Any], episode_id: str, note: str) -> None:
    if note.strip():
        thread["notes"].append({"episode_id": episode_id, "note": note.strip()[:80]})
        del thread["notes"][:-MAX_NOTES]


def record_step(
    thread: dict[str, Any],
    cfg: LifeConfig,
    *,
    episode_id: str,
    note: str,
    new_content: bool,
    now: float,
) -> None:
    """续查一步：有新内容步数 +1；连续两次没有就冷却 180 分钟；步数到上限进回看。"""

    thread["updated_at"] = now
    if not new_content:
        thread["empty_streak"] = int(thread.get("empty_streak", 0)) + 1
        if thread["empty_streak"] >= 2:
            thread["cooldown_until"] = now + THREAD_COOLDOWN_SECONDS
            thread["empty_streak"]   = 0
        return
    thread["empty_streak"] = 0
    thread["steps"]        = int(thread.get("steps", 0)) + 1
    _add_note(thread, episode_id, note)
    if thread["steps"] >= cfg.thread_max_steps:
        thread["status"] = "reviewing"


def mark_answered(thread: dict[str, Any], *, episode_id: str, note: str, now: float) -> None:
    thread["updated_at"] = now
    _add_note(thread, episode_id, note)
    thread["status"] = "reviewing"


def finish(thread: dict[str, Any], takeaway: str, episode_ids: list[str], now: float) -> bool:
    """收获必须引用至少一个她自己的经历 ID，否则不能 done。"""

    known = {n.get("episode_id") for n in thread.get("notes", [])}
    if not takeaway.strip() or not any(e in known for e in episode_ids):
        return False
    thread["takeaway"]   = takeaway.strip()[:80]
    thread["status"]     = "done"
    thread["updated_at"] = now
    return True


def drop(thread: dict[str, Any], now: float) -> None:
    thread["status"]     = "dropped"
    thread["updated_at"] = now


def prune_threads(threads: list[dict[str, Any]], now: float) -> None:
    """结束的小课题保留 30 天，用于“我前两天查了下……”。"""

    threads[:] = [
        t
        for t in threads
        if t.get("status") in ACTIVE or now - float(t.get("updated_at", 0.0)) <= DONE_KEEP_SECONDS
    ]


def protected_episode_ids(threads: list[dict[str, Any]]) -> set[str]:
    """进行中小课题引用的经历不过期。"""

    return {
        str(n.get("episode_id"))
        for t in active_threads(threads)
        for n in t.get("notes", [])
        if n.get("episode_id")
    }
