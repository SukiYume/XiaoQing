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


def _task_sort_key(task: dict[str, Any]) -> tuple:
    due_time = task.get("due_time") or "9999-12-31T23:59:59"
    return (_priority_value(task), due_time, task.get("created_at") or "")


def _focus_sort_key(task: dict[str, Any], today_day: date) -> tuple:
    due_day = _parse_date(task.get("due_time"))
    is_due_today = 1 if due_day == today_day else 0
    return (0 if due_day and due_day < today_day else is_due_today, *_task_sort_key(task))


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
    tasks = _load_all_tasks(db=db, owner_id=owner_id)

    active = [task for task in tasks if task.get("status") in {"todo", "in_progress"}]
    done = [task for task in tasks if task.get("status") == "done"]
    cancelled = [task for task in tasks if task.get("status") == "cancelled"]

    overdue_tasks: list[dict[str, Any]] = []
    focus_tasks: list[dict[str, Any]] = []
    up_next_tasks: list[dict[str, Any]] = []
    later_tasks: list[dict[str, Any]] = []
    backlog_tasks: list[dict[str, Any]] = []

    for task in active:
        due_day = _parse_date(task.get("due_time"))
        if due_day and due_day < today_day:
            overdue_tasks.append(task)
            focus_tasks.append(task)
        elif due_day == today_day:
            focus_tasks.append(task)
        elif due_day and due_day <= today_day + timedelta(days=7):
            up_next_tasks.append(task)
        elif due_day:
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

    category_counter = Counter(task.get("category") or "未分类" for task in active)
    category_load = [
        {"category": category, "count": count, "share": count / len(active) if active else 0}
        for category, count in category_counter.most_common(6)
    ]

    completion_denominator = len(active) + len(done)
    completion_rate = (len(done) / completion_denominator) if completion_denominator else 0

    board_columns = {}
    for status in ("todo", "in_progress", "done", "cancelled"):
        column_tasks = [task for task in tasks if task.get("status") == status]
        sort_key = _done_sort_key if status == "done" else _task_sort_key
        board_columns[status] = sorted(column_tasks, key=sort_key, reverse=(status == "done"))

    return {
        "summary": {
            "active_count": len(active),
            "focus_count": len(focus_tasks),
            "overdue_count": len(overdue_tasks),
            "done_today_count": done_today_count,
            "done_count": len(done),
            "cancelled_count": len(cancelled),
            "completion_rate": round(completion_rate, 4),
        },
        "focus_tasks": focus_tasks[:6],
        "up_next_tasks": up_next_tasks[:8],
        "later_tasks": later_tasks[:8],
        "backlog_tasks": backlog_tasks[:8],
        "overdue_tasks": overdue_tasks[:6],
        "done_recent": done_recent,
        "category_load": category_load,
        "completion_bars": completion_bars,
        "board_columns": board_columns,
        "all_tasks": tasks,
    }
