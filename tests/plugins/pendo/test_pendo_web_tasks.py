"""Pendo Web 任务概览聚合器的跨模块回归。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics import task_overview as task_overview_module
from plugins.pendo.web.analytics.task_overview import build_task_overview


def _insert(db: Database, owner_id: str, **values: Any) -> None:
    """为当前所有者写入一条测试任务。"""

    db.insert_item({"owner_id": owner_id, "type": "task", **values})


def test_build_task_overview_groups_widget_tasks_and_returns_minimal_payload(
    db: Database,
) -> None:
    """Widget 分组保持完整，页面负载不再夹带重复派生或私有字段。"""

    owner_id = "u-task-overview"
    for values in (
        {
            "id": "t1",
            "title": "今天截止",
            "category": "工作",
            "status": "open",
            "priority": 1,
            "plan_date": "2026-03-26",
            "deadline_at": "2026-03-26T18:00:00",
            "created_at": "2026-03-25T09:00:00",
        },
        {
            "id": "t2",
            "title": "已经逾期",
            "category": "工作",
            "status": "open",
            "priority": 2,
            "plan_date": "2026-03-24",
            "deadline_at": "2026-03-24T18:00:00",
            "created_at": "2026-03-20T09:00:00",
        },
        {
            "id": "t3",
            "title": "下周处理",
            "category": "生活",
            "status": "open",
            "priority": 4,
            "plan_date": "2026-03-29",
            "deadline_at": "2026-03-29T12:00:00",
            "created_at": "2026-03-22T09:00:00",
        },
        {
            "id": "t4",
            "title": "已经完成",
            "category": "工作",
            "status": "done",
            "priority": 3,
            "plan_date": "2026-03-25",
            "completed_at": "2026-03-26T08:00:00",
            "created_at": "2026-03-24T09:00:00",
        },
        {
            "id": "t5",
            "title": "已取消",
            "category": "杂项",
            "status": "cancelled",
            "priority": 5,
            "cancelled_at": "2026-03-25T08:00:00",
            "created_at": "2026-03-23T09:00:00",
        },
    ):
        _insert(db, owner_id, **values)

    result = build_task_overview(db=db, owner_id=owner_id, today="2026-03-26")
    cancelled = next(task for task in result["all_tasks"] if task["id"] == "t5")

    assert set(result) == {
        "summary",
        "focus_tasks",
        "up_next_tasks",
        "later_tasks",
        "backlog_tasks",
        "all_tasks",
    }
    assert result["summary"] == {"active_count": 3, "focus_count": 2, "overdue_count": 1}
    assert [task["id"] for task in result["focus_tasks"]] == ["t2", "t1"]
    assert [task["id"] for task in result["up_next_tasks"]] == ["t3"]
    assert len(result["all_tasks"]) == 5
    assert set(cancelled) == {
        "id",
        "title",
        "content",
        "category",
        "status",
        "priority",
        "plan_date",
        "deadline_at",
        "completed_at",
        "cancelled_at",
        "created_at",
        "updated_at",
        "version",
    }
    assert cancelled["cancelled_at"] == "2026-03-25T00:00:00+00:00"


def test_build_task_overview_returns_all_focus_tasks(db: Database) -> None:
    """今日与逾期任务不能按 Widget 卡片上限截断。"""

    owner_id = "u-task-focus"
    for index in range(8):
        _insert(
            db,
            owner_id,
            id=f"today-{index}",
            title=f"今天任务 {index}",
            category="工作",
            status="open",
            priority=3,
            plan_date="2026-03-26",
            created_at=f"2026-03-25T09:00:{index:02d}",
        )

    result = build_task_overview(db=db, owner_id=owner_id, today="2026-03-26")

    assert result["summary"]["focus_count"] == 8
    assert [task["id"] for task in result["focus_tasks"]] == [
        f"today-{index}" for index in range(8)
    ]


def test_build_task_overview_loads_more_than_500_tasks(db: Database) -> None:
    """全量任务不能在首个读取批次结束，Widget 小分组仍保持上限。"""

    owner_id = "u-task-many"
    for index in range(505):
        _insert(
            db,
            owner_id,
            id=f"t{index}",
            title=f"任务 {index}",
            category="工作",
            status="open",
            priority=3,
            created_at=f"2026-03-01T00:00:{index % 60:02d}",
        )

    result = build_task_overview(db=db, owner_id=owner_id, today="2026-03-26")

    assert result["summary"]["active_count"] == 505
    assert len(result["all_tasks"]) == 505
    assert len(result["backlog_tasks"]) == 8


def test_build_task_overview_uses_user_clock_when_today_is_omitted(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """省略 today 时，今日聚焦必须依据用户设置时区。"""

    owner_id = "u-task-user-clock"
    _insert(
        db,
        owner_id,
        id="today",
        title="用户今天",
        status="open",
        plan_date="2030-01-02",
    )

    def user_now(user_id: str, database: Database) -> datetime:
        assert user_id == owner_id
        assert database is db
        return datetime.fromisoformat("2030-01-02T00:30:00-12:00")

    monkeypatch.setattr(task_overview_module, "now_in_timezone", user_now)
    result = build_task_overview(db=db, owner_id=owner_id)

    assert result["summary"]["focus_count"] == 1
    assert result["focus_tasks"][0]["id"] == "today"


def test_build_task_overview_rejects_invalid_direct_today(db: Database) -> None:
    """显式非法 today 不能静默改用宿主机日期。"""

    with pytest.raises(ValueError, match="today must be a valid ISO date"):
        build_task_overview(db=db, owner_id="u-task-invalid", today="not-a-date")
