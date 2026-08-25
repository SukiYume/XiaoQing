"""为 Pendo Web 任务页和 Widget 生成最小任务概览。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from ...models.item import TaskItem, TaskStatus
from ...services.db import Database
from ...utils.identifiers import public_id
from ...utils.time_utils import TimezoneHelper, now_in_timezone
from ..utils import parse_iso_date

JsonObject = dict[str, Any]

_TASK_PLAN_KEY_SQL = """
CASE
  WHEN TRIM(COALESCE(plan_date, '')) != '' THEN TRIM(plan_date)
  WHEN pendo_local_date(deadline_at, :timezone) != ''
    THEN pendo_local_date(deadline_at, :timezone)
  ELSE ''
END
""".strip()
_TASK_PRIORITY_SQL = "CASE WHEN priority BETWEEN 1 AND 5 THEN priority ELSE 3 END"


@dataclass(frozen=True, slots=True)
class _TaskRecord:
    """页面和 Widget 实际需要的任务字段及缓存计划日。"""

    id: str
    title: str
    content: str
    category: str
    status: str
    priority: int
    plan_date: str | None
    deadline_at: str | None
    completed_at: str | None
    cancelled_at: str | None
    created_at: str
    updated_at: str
    version: int
    plan_key: str
    deadline_sort: datetime | None
    created_sort: datetime | None

    def to_payload(self) -> JsonObject:
        """只公开任务页规范化器消费的字段。"""

        return {
            "id": self.id,
            "display_id": public_id(self.id),
            "title": self.title,
            "content": self.content,
            "category": self.category,
            "status": self.status,
            "priority": self.priority,
            "plan_date": self.plan_date,
            "deadline_at": self.deadline_at,
            "completed_at": self.completed_at,
            "cancelled_at": self.cancelled_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }


def _normalize_task(item: TaskItem, user_timezone: tzinfo) -> _TaskRecord:
    """清理任务字段，并按计划日优先、截止日回退计算分桶日期。"""

    plan_day = parse_iso_date(item.plan_date)
    deadline_sort: datetime | None = None
    created_sort: datetime | None = None
    if item.deadline_at:
        try:
            deadline_sort = TimezoneHelper.parse(item.deadline_at, user_timezone)
        except (TypeError, ValueError):
            pass
    try:
        created_sort = TimezoneHelper.parse(item.created_at, user_timezone)
    except (TypeError, ValueError):
        pass
    deadline_day = deadline_sort.date() if deadline_sort is not None else None
    plan_date = plan_day.isoformat() if plan_day else None
    plan_key = plan_date or (deadline_day.isoformat() if deadline_day else "")
    raw_priority: object = item.priority
    priority = (
        raw_priority
        if isinstance(raw_priority, int)
        and not isinstance(raw_priority, bool)
        and 1 <= raw_priority <= 5
        else 3
    )
    raw_status: object = item.status
    status = raw_status.value if isinstance(raw_status, TaskStatus) else str(raw_status or "open")
    if status not in {"open", "done", "cancelled"}:
        status = "open"
    raw_version: object = item.version
    version = (
        raw_version
        if isinstance(raw_version, int) and not isinstance(raw_version, bool) and raw_version >= 0
        else 0
    )
    return _TaskRecord(
        id=item.id,
        title=item.title,
        content=item.content,
        category=(item.category or "").strip() or "未分类",
        status=status,
        priority=priority,
        plan_date=plan_date,
        deadline_at=item.deadline_at,
        completed_at=item.completed_at,
        cancelled_at=item.cancelled_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
        version=version,
        plan_key=plan_key,
        deadline_sort=deadline_sort,
        created_sort=created_sort,
    )


def _load_all_tasks(db: Database, owner_id: str) -> list[_TaskRecord]:
    """读取任务页契约要求的全量任务，不把分页页块写入共享缓存。"""

    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)
    return [
        _normalize_task(item, user_timezone)
        for item in db.get_all_items(owner_id, filters={"type": "task"}, page_size=500)
        if isinstance(item, TaskItem)
    ]


def _task_sort_key(task: _TaskRecord) -> tuple[int, str, datetime, datetime]:
    """按优先级、计划日、截止时间和创建时间稳定排序。"""

    return (
        task.priority,
        task.plan_key or "9999-12-31",
        task.deadline_sort or datetime.max.replace(tzinfo=timezone.utc),
        task.created_sort or datetime.max.replace(tzinfo=timezone.utc),
    )


def _focus_sort_key(
    task: _TaskRecord,
    today_key: str,
) -> tuple[int, int, str, datetime, datetime]:
    """聚焦列表先展示逾期，再按普通任务顺序展示今天事项。"""

    return (0 if task.plan_key < today_key else 1, *_task_sort_key(task))


def _resolve_today(db: Database, owner_id: str, today: str | None) -> date:
    """校验显式自然日，缺省时使用用户自己的时区。"""

    today_day = parse_iso_date(today)
    if today is not None and today.strip() and today_day is None:
        raise ValueError("today must be a valid ISO date")
    return today_day or now_in_timezone(owner_id, db).date()


def build_task_widget_overview(
    db: Database,
    owner_id: str,
    today: str | None = None,
    *,
    limit: int = 5,
) -> JsonObject:
    """用有界 SQL 聚合生成 Widget 所需的待办计数和预览。"""

    if limit <= 0:
        raise ValueError("limit must be positive")
    today_day = _resolve_today(db, owner_id, today)
    today_key = today_day.isoformat()
    next_week_key = (today_day + timedelta(days=7)).isoformat()
    timezone_name = TimezoneHelper.get_user_timezone(owner_id, db).key
    active_cte = f"""
        WITH active AS (
          SELECT id, title, status, plan_date, deadline_at, created_at,
                 {_TASK_PLAN_KEY_SQL} AS plan_key,
                 {_TASK_PRIORITY_SQL} AS normalized_priority
          FROM items
          WHERE owner_id = :owner_id AND type = 'task' AND deleted = 0
            AND COALESCE(status, 'open') = 'open'
        )
    """
    conn = db.get_connection()
    summary_row = conn.execute(
        active_cte
        + """
        SELECT COUNT(*) AS active_count,
               COALESCE(SUM(CASE WHEN plan_key != '' AND plan_key <= :today THEN 1 ELSE 0 END), 0)
                  AS focus_count,
               COALESCE(SUM(CASE WHEN plan_key != '' AND plan_key < :today THEN 1 ELSE 0 END), 0)
                  AS overdue_count
        FROM active
        """,
        {"owner_id": owner_id, "timezone": timezone_name, "today": today_key},
    ).fetchone()
    rows = conn.execute(
        active_cte
        + """
        SELECT id, title, status, plan_date, deadline_at, created_at
        FROM active
        ORDER BY
          CASE
            WHEN plan_key != '' AND plan_key <= :today THEN 0
            WHEN plan_key != '' AND plan_key <= :next_week THEN 1
            WHEN plan_key = '' THEN 2
            ELSE 3
          END,
          CASE WHEN plan_key != '' AND plan_key < :today THEN 0 ELSE 1 END,
          normalized_priority,
          COALESCE(NULLIF(plan_key, ''), '9999-12-31'),
          COALESCE(pendo_utc_epoch(deadline_at, :timezone), 1e999),
          COALESCE(pendo_utc_epoch(created_at, :timezone), 1e999),
          id
        LIMIT :limit
        """,
        {
            "owner_id": owner_id,
            "timezone": timezone_name,
            "today": today_key,
            "next_week": next_week_key,
            "limit": limit,
        },
    ).fetchall()
    return {
        "summary": {
            "active_count": int(summary_row["active_count"] or 0),
            "focus_count": int(summary_row["focus_count"] or 0),
            "overdue_count": int(summary_row["overdue_count"] or 0),
        },
        "items": [dict(row) for row in rows],
    }


def build_task_overview(
    db: Database,
    owner_id: str,
    today: str | None = None,
) -> JsonObject:
    """生成任务页所需全量任务和 Widget 所需活动分组。"""

    today_day = _resolve_today(db, owner_id, today)
    today_key = today_day.isoformat()
    next_week_key = (today_day + timedelta(days=7)).isoformat()
    tasks = _load_all_tasks(db, owner_id)
    active = [task for task in tasks if task.status == "open"]

    overdue_tasks: list[_TaskRecord] = []
    focus_tasks: list[_TaskRecord] = []
    up_next_tasks: list[_TaskRecord] = []
    later_tasks: list[_TaskRecord] = []
    backlog_tasks: list[_TaskRecord] = []
    for task in active:
        if task.plan_key and task.plan_key < today_key:
            overdue_tasks.append(task)
            focus_tasks.append(task)
        elif task.plan_key == today_key:
            focus_tasks.append(task)
        elif task.plan_key and task.plan_key <= next_week_key:
            up_next_tasks.append(task)
        elif task.plan_key:
            later_tasks.append(task)
        else:
            backlog_tasks.append(task)

    focus_tasks.sort(key=lambda task: _focus_sort_key(task, today_key))
    up_next_tasks.sort(key=_task_sort_key)
    later_tasks.sort(key=_task_sort_key)
    backlog_tasks.sort(key=_task_sort_key)
    return {
        "summary": {
            "active_count": len(active),
            "focus_count": len(focus_tasks),
            "overdue_count": len(overdue_tasks),
        },
        "focus_tasks": [task.to_payload() for task in focus_tasks],
        "up_next_tasks": [task.to_payload() for task in up_next_tasks[:8]],
        "later_tasks": [task.to_payload() for task in later_tasks[:8]],
        "backlog_tasks": [task.to_payload() for task in backlog_tasks[:8]],
        "all_tasks": [task.to_payload() for task in tasks],
    }
