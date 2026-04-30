"""Task-focused aggregation for the Pendo web UI."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from ...services.db import Database


def _task_dict(task) -> dict[str, Any]:
    return task.to_dict() if hasattr(task, "to_dict") else {}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        if "T" in text:
            return datetime.fromisoformat(text).date()
        return datetime.fromisoformat(f"{text}T00:00:00").date()
    except ValueError:
        return None


def _priority_value(task: dict[str, Any]) -> int:
    try:
        return int(task.get("priority") or 99)
    except (TypeError, ValueError):
        return 99


def _task_status_bucket(task: dict[str, Any]) -> str:
    return "closed" if task.get("status") in {"done", "cancelled"} else "open"


def _task_text_category(task: dict[str, Any]) -> str:
    category = str(task.get("category") or "").strip()
    if category and category != "未分类":
        return category
    return ""


def _task_plan_key(task: dict[str, Any]) -> str:
    plan = str(task.get("plan_date") or "").strip()
    if plan:
        return plan
    deadline_day = _parse_date(task.get("deadline_at"))
    return deadline_day.strftime("%Y-%m-%d") if deadline_day else ""


def _task_sort_key(task: dict[str, Any]) -> tuple:
    due_key = _task_plan_key(task) or "9999-12-31"
    return (_priority_value(task), due_key, task.get("created_at") or "")


def _focus_sort_key(task: dict[str, Any], today_day: date) -> tuple:
    plan_key = _task_plan_key(task)
    today_key = today_day.strftime("%Y-%m-%d")
    is_due_today = 1 if plan_key == today_key else 0
    return (0 if plan_key and plan_key < today_key else is_due_today, *_task_sort_key(task))


def _done_sort_key(task: dict[str, Any]) -> tuple:
    completed = task.get("completed_at") or task.get("updated_at") or ""
    return (completed, task.get("updated_at") or "")


def _load_all_tasks(db: Database, owner_id: str, batch_size: int = 200) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    offset = 0

    while True:
        chunk = db.get_items(owner_id, filters={"type": "task"}, limit=batch_size, offset=offset)
        if not chunk:
            break
        tasks.extend(_task_dict(task) for task in chunk)
        if len(chunk) < batch_size:
            break
        offset += batch_size

    return tasks


def build_task_overview(db: Database, owner_id: str, today: str | None = None) -> dict[str, Any]:
    today_day = _parse_date(today) or datetime.now().date()
    today_key = today_day.strftime("%Y-%m-%d")
    next_week_key = (today_day + timedelta(days=7)).strftime("%Y-%m-%d")
    tasks = _load_all_tasks(db=db, owner_id=owner_id)

    active = [task for task in tasks if _task_status_bucket(task) == "open"]
    done = [task for task in tasks if task.get("status") == "done"]
    cancelled = [task for task in tasks if task.get("status") == "cancelled"]
    closed = done + cancelled

    overdue_tasks: list[dict[str, Any]] = []
    focus_tasks: list[dict[str, Any]] = []
    up_next_tasks: list[dict[str, Any]] = []
    later_tasks: list[dict[str, Any]] = []
    backlog_tasks: list[dict[str, Any]] = []

    for task in active:
        plan_key = _task_plan_key(task)
        if plan_key and plan_key < today_key:
            overdue_tasks.append(task)
            focus_tasks.append(task)
        elif plan_key == today_key:
            focus_tasks.append(task)
        elif plan_key and plan_key <= next_week_key:
            up_next_tasks.append(task)
        elif plan_key:
            later_tasks.append(task)
        else:
            backlog_tasks.append(task)

    overdue_tasks.sort(key=_task_sort_key)
    focus_tasks.sort(key=lambda task: _focus_sort_key(task, today_day))
    up_next_tasks.sort(key=_task_sort_key)
    later_tasks.sort(key=_task_sort_key)
    backlog_tasks.sort(key=_task_sort_key)

    done_recent = sorted(done, key=_done_sort_key, reverse=True)[:8]
    done_today_count = sum(1 for task in done if _parse_date(task.get("completed_at") or task.get("updated_at")) == today_day)

    last_days = [today_day - timedelta(days=offset) for offset in range(6, -1, -1)]
    done_counter = Counter(
        _parse_date(task.get("completed_at") or task.get("updated_at"))
        for task in done
        if _parse_date(task.get("completed_at") or task.get("updated_at")) is not None
    )
    completion_bars = [
        {
            "date": day.strftime("%Y-%m-%d"),
            "label": f"{day.month}/{day.day}",
            "count": done_counter.get(day, 0),
        }
        for day in last_days
    ]

    category_counter = Counter(_task_text_category(task) or "未分类" for task in active if _task_text_category(task))
    category_load = [
        {"category": category, "count": count, "share": count / len(active) if active else 0}
        for category, count in category_counter.most_common(6)
    ]

    plan_counter = Counter(_task_plan_key(task) for task in active if _task_plan_key(task))
    plan_load = [
        {
            "plan": plan,
            "count": count,
            "share": count / len(active) if active else 0,
            "state": "overdue" if plan < today_key else ("today" if plan == today_key else "upcoming"),
        }
        for plan, count in sorted(plan_counter.items(), key=lambda item: item[0])[:6]
    ]

    completion_denominator = len(active) + len(done)
    completion_rate = (len(done) / completion_denominator) if completion_denominator else 0

    board_columns = {
        "open": sorted(active, key=_task_sort_key),
        "done": sorted(done, key=_done_sort_key, reverse=True),
        "cancelled": sorted(cancelled, key=_done_sort_key, reverse=True),
    }
    board_columns["closed"] = sorted(closed, key=_done_sort_key, reverse=True)

    return {
        "summary": {
            "active_count": len(active),
            "focus_count": len(focus_tasks),
            "overdue_count": len(overdue_tasks),
            "done_today_count": done_today_count,
            "done_count": len(done),
            "cancelled_count": len(cancelled),
            "closed_count": len(closed),
            "completion_rate": round(completion_rate, 4),
        },
        "focus_tasks": focus_tasks[:6],
        "up_next_tasks": up_next_tasks[:8],
        "later_tasks": later_tasks[:8],
        "backlog_tasks": backlog_tasks[:8],
        "overdue_tasks": overdue_tasks[:6],
        "done_recent": done_recent,
        "plan_load": plan_load,
        "category_load": category_load,
        "completion_bars": completion_bars,
        "board_columns": board_columns,
        "all_tasks": tasks,
    }
