"""Regression tests for the redesigned Pendo web tasks page."""

import shutil
import uuid
from pathlib import Path

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.task_overview import build_task_overview

ROOT = Path(__file__).resolve().parents[2]


def test_build_task_overview_groups_focus_risk_and_recent_done():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_tasks_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-task-overview"

    try:
        db.insert_item({
            "id": "t1",
            "owner_id": owner_id,
            "type": "task",
            "title": "今天截止",
            "category": "工作",
            "status": "open",
            "priority": 1,
            "plan_date": "2026-03-26",
            "deadline_at": "2026-03-26T18:00:00",
            "created_at": "2026-03-25T09:00:00",
        })
        db.insert_item({
            "id": "t2",
            "owner_id": owner_id,
            "type": "task",
            "title": "已经逾期",
            "category": "工作",
            "status": "open",
            "priority": 2,
            "plan_date": "2026-03-24",
            "deadline_at": "2026-03-24T18:00:00",
            "created_at": "2026-03-20T09:00:00",
        })
        db.insert_item({
            "id": "t3",
            "owner_id": owner_id,
            "type": "task",
            "title": "下周处理",
            "category": "生活",
            "status": "open",
            "priority": 4,
            "plan_date": "2026-03-29",
            "deadline_at": "2026-03-29T12:00:00",
            "created_at": "2026-03-22T09:00:00",
        })
        db.insert_item({
            "id": "t4",
            "owner_id": owner_id,
            "type": "task",
            "title": "已经完成",
            "category": "工作",
            "status": "done",
            "priority": 3,
            "plan_date": "2026-03-25",
            "deadline_at": "2026-03-25T10:00:00",
            "completed_at": "2026-03-26T08:00:00",
            "created_at": "2026-03-24T09:00:00",
        })
        db.insert_item({
            "id": "t5",
            "owner_id": owner_id,
            "type": "task",
            "title": "已取消",
            "category": "杂项",
            "status": "cancelled",
            "priority": 5,
            "created_at": "2026-03-23T09:00:00",
        })

        result = build_task_overview(db=db, owner_id=owner_id, today="2026-03-26")

        assert result["summary"]["active_count"] == 3
        assert result["summary"]["focus_count"] == 2
        assert result["summary"]["overdue_count"] == 1
        assert result["summary"]["done_today_count"] == 1
        assert result["summary"]["completion_rate"] == 0.25
        assert result["focus_tasks"][0]["id"] == "t2"
        assert result["up_next_tasks"][0]["id"] == "t3"
        assert result["done_recent"][0]["id"] == "t4"
        assert result["category_load"][0]["category"] == "工作"
        assert result["board_columns"]["open"][0]["id"] == "t1"
        assert result["board_columns"]["cancelled"][0]["id"] == "t5"
        assert len(result["completion_bars"]) == 7
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_task_overview_returns_all_focus_tasks():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_tasks_focus_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-task-focus"

    try:
        for index in range(8):
            db.insert_item({
                "id": f"today-{index}",
                "owner_id": owner_id,
                "type": "task",
                "title": f"今天任务 {index}",
                "category": "工作",
                "status": "open",
                "priority": 3,
                "plan_date": "2026-03-26",
                "created_at": f"2026-03-25T09:00:{index:02d}",
            })

        result = build_task_overview(db=db, owner_id=owner_id, today="2026-03-26")

        assert result["summary"]["focus_count"] == 8
        assert len(result["focus_tasks"]) == 8
        assert [task["id"] for task in result["focus_tasks"]] == [f"today-{index}" for index in range(8)]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_tasks_page_source_uses_task_overview_and_view_toggle():
    api_src = (ROOT / "plugins" / "pendo" / "web" / "api" / "stats.py").read_text(encoding="utf-8")
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "tasks.js").read_text(encoding="utf-8")

    assert '"/stats/tasks/overview"' in api_src
    assert "/stats/tasks/overview" in page_src
    assert "task-view-list" in page_src
    assert "task-view-board" in page_src
    assert "model.focus_tasks.slice(0, 6)" not in page_src


def test_tasks_page_source_normalizes_modal_payload_defaults():
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "tasks.js").read_text(encoding="utf-8")

    assert "function normalizeTaskPayload(formData)" in page_src
    assert "payload.category = String(payload.category || '').trim() || '未分类';" in page_src
    assert "payload.content = payload.content ?? '';" in page_src
    assert "payload.priority = Number.isInteger(priority) && priority >= 1 && priority <= 5 ? priority : 3;" in page_src


def test_diary_page_source_styles_mood_picker_active_state():
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "diary.js").read_text(encoding="utf-8")

    assert ".mood-selector { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }" in page_src
    assert ".mood-btn.active {" in page_src
    assert "transform: scale(1.06);" in page_src
    assert "box-shadow: inset 0 0 0 1px rgba(236,72,153,0.12);" in page_src


def test_tasks_page_source_uses_subtle_priority_picker_active_state():
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "tasks.js").read_text(encoding="utf-8")

    assert "border-radius: 12px;" in page_src
    assert ".priority-btn.active { opacity: 1; transform: scale(1.06); }" in page_src
    assert ".priority-btn.priority-3.active { border-color: rgba(234,179,8,0.38);" in page_src
    assert "box-shadow: inset 0 0 0 1px rgba(234,179,8,0.14);" in page_src


def test_tasks_page_source_scales_stat_values_for_mid_width_layouts():
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "tasks.js").read_text(encoding="utf-8")

    assert "min-width: 0;" in page_src
    assert "font-size: clamp(24px, 1.9vw, 30px);" in page_src
    assert "overflow-wrap: anywhere;" in page_src
    assert "word-break: break-word;" in page_src
    assert "white-space: nowrap;" in page_src
    assert "text-overflow: ellipsis;" in page_src


def test_build_task_overview_loads_more_than_500_tasks():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_tasks_many_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-task-many"

    try:
        for index in range(505):
            db.insert_item({
                "id": f"t{index}",
                "owner_id": owner_id,
                "type": "task",
                "title": f"任务 {index}",
                "category": "工作",
                "status": "open",
                "priority": 3,
                "created_at": f"2026-03-01T00:00:{index % 60:02d}",
            })

        result = build_task_overview(db=db, owner_id=owner_id, today="2026-03-26")

        assert result["summary"]["active_count"] == 505
        assert len(result["all_tasks"]) == 505
        assert len(result["board_columns"]["open"]) == 505
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)
