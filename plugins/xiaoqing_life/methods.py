"""方法复用：她自己摸索出的查法（做完一个小课题后提炼），下次相近的小课题照着做。

方法是模型写的文字，**不是**词条、表达、兴趣的来源；提炼必须对得上真实的行动记录。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .safety import clean_text, is_blocked, overlap, similar
from .stores import done_today, mark_done, new_id

MAX_METHODS     = 30
MAX_SELECTED    = 2
TEXT_CHARS      = 40
APPLIES_CHARS   = 20
RETIRE_MIN_USES = 3
RETIRE_RATIO    = 1 / 3


def active(methods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in methods if m.get("status", "active") == "active"]


def select(methods: list[dict[str, Any]], thread: dict[str, Any]) -> list[dict[str, Any]]:
    """按（小课题标题 + 话题）与（适用范围 + 做法）的重合排序取 ≤2 条；重合 ≥1 或 ``wins ≥ 1`` 才给。"""

    context = f"{thread.get('title', '')} {thread.get('topic', '')}"
    ranked  = []
    for method in active(methods):
        score = overlap(context, f"{method.get('applies_to', '')} {method.get('text', '')}")
        if score >= 1 or int(method.get("wins", 0)) >= 1:
            ranked.append((score, int(method.get("wins", 0)), method))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [m for _, _, m in ranked[:MAX_SELECTED]]


def record_use(picked: Iterable[dict[str, Any]], thread: dict[str, Any], now: float) -> None:
    """第一次给某个小课题用时：记进小课题 ``methods_used``，方法 ``uses += 1``、``last_used``。"""

    used: list[str] = thread.setdefault("methods_used", [])
    for method in picked:
        if method["id"] in used:
            continue
        used.append(method["id"])
        method["uses"]      = int(method.get("uses", 0)) + 1
        method["last_used"] = now


def on_thread_done(methods: list[dict[str, Any]], thread: dict[str, Any]) -> None:
    """用过方法的小课题 done：每条 ``wins += 1``；``uses ≥ 3`` 且胜率 < 1/3 的退役。"""

    used = set(thread.get("methods_used", []))
    for method in methods:
        if method.get("id") not in used:
            continue
        method["wins"] = int(method.get("wins", 0)) + 1
        uses           = int(method.get("uses", 0))
        if uses >= RETIRE_MIN_USES and method["wins"] / uses < RETIRE_RATIO:
            method["status"] = "retired"


def action_rows(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """睡前整理输入里的行动记录：下标、行动、参数、是否有用。"""

    return [
        {
            "index" : n,
            "action": row.get("action", ""),
            "arg"   : row.get("arg", ""),
            "useful": bool(row.get("useful")),
        }
        for n, row in enumerate(thread.get("actions", []))
    ]


def apply_extracted(
    methods: list[dict[str, Any]],
    raw: Any,
    finished: list[dict[str, Any]],
    blocked: tuple[str, ...],
    day: str,
    state: dict[str, Any],
    now: float,
) -> bool:
    """当天 done 的小课题可以写一条查法（全天 ≤1 条）。

    核对：小课题这次确实 done；下标都存在且至少一个 ``useful``；文字不命中屏蔽词；与已有 active 方法
    相似时不新建，只给那条 ``wins += 1``。
    """

    if not isinstance(raw, list) or done_today(state, "method", day):
        return False
    for row in raw:
        if not isinstance(row, dict):
            continue
        thread  = next((t for t in finished if t.get("id") == row.get("thread_id")), None)
        indices = row.get("actions")
        if thread is None or not isinstance(indices, list) or not indices:
            continue
        log = thread.get("actions", [])
        if not all(type(i) is int and 0 <= i < len(log) for i in indices):
            continue
        if not any(log[i].get("useful") for i in indices):
            continue
        text    = clean_text(row.get("text"), TEXT_CHARS)
        applies = clean_text(row.get("applies_to"), APPLIES_CHARS)
        if not text or is_blocked(f"{text} {applies}", blocked):
            continue
        mark_done(state, "method", day)
        twin = next((m for m in active(methods) if similar(text, str(m.get("text", "")))), None)
        if twin is not None:
            twin["wins"] = int(twin.get("wins", 0)) + 1
            return True
        methods.append(
            {
                "id"         : new_id("m"),
                "text"       : text,
                "applies_to" : applies or str(thread.get("topic", ""))[:APPLIES_CHARS],
                "from_thread": thread["id"],
                "created_at" : now,
                "uses"       : 0,
                "wins"       : 0,
                "last_used"  : 0.0,
                "status"     : "active",
            }
        )
        _trim(methods)
        return True
    return False


def _trim(methods: list[dict[str, Any]]) -> None:
    """总量 ≤30：先丢最旧的退役方法，再丢最旧的。"""

    while len(methods) > MAX_METHODS:
        pool = [m for m in methods if m.get("status") == "retired"] or methods
        methods.remove(min(pool, key=lambda m: float(m.get("created_at", 0.0))))


def chat_line(methods: list[dict[str, Any]]) -> str:
    """被问“怎么查的 / 你怎么知道”：给 1 条 ``wins ≥ 1`` 的方法。"""

    wins = [m for m in active(methods) if int(m.get("wins", 0)) >= 1]
    if not wins:
        return ""
    best = max(wins, key=lambda m: (int(m.get("wins", 0)), float(m.get("created_at", 0.0))))
    return f"你自己摸索出来的查法：{best['text']}"
