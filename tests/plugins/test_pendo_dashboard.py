"""Regression tests for the refreshed Pendo dashboard overview."""

from datetime import datetime
from pathlib import Path
import shutil
import uuid

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.dashboard_overview import build_dashboard_overview


ROOT = Path(__file__).resolve().parents[2]


def test_build_dashboard_overview_uses_month_events_and_mixed_task_buckets():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_dashboard_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-dashboard"

    try:
        db.insert_item({
            "id": "ev1",
            "owner_id": owner_id,
            "type": "event",
            "title": "月内会议",
            "start_time": "2026-03-10T09:00:00",
            "end_time": "2026-03-10T10:00:00",
        })
        db.insert_item({
            "id": "ev2",
            "owner_id": owner_id,
            "type": "event",
            "title": "月末复盘",
            "start_time": "2026-03-28T18:00:00",
        })
        db.insert_item({
            "id": "ev3",
            "owner_id": owner_id,
            "type": "event",
            "title": "下月活动",
            "start_time": "2026-04-02T18:00:00",
        })
        db.create_event_collection({
            "id": "ev4col",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "FRB2026会议",
            "content": "",
            "category": "学术",
            "location": "",
            "start_time": "2026-02-20T00:00:00",
            "end_time": "2026-04-02T12:00:00",
        })
        db.insert_item({
            "id": "ev4col_m01",
            "owner_id": owner_id,
            "type": "event",
            "title": "摘要截止",
            "category": "学术",
            "start_time": "2026-03-05T09:00:00",
            "event_role": "multi_node_child",
            "event_collection_id": "ev4col",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        })
        db.insert_item({
            "id": "ev4col_m02",
            "owner_id": owner_id,
            "type": "event",
            "title": "会议开始",
            "category": "学术",
            "start_time": "2026-04-01T10:00:00",
            "event_role": "multi_node_child",
            "event_collection_id": "ev4col",
            "event_collection_kind": "multi_node",
            "event_index": 2,
            "event_node_key": "m02",
        })

        db.insert_item({
            "id": "task1",
            "owner_id": owner_id,
            "type": "task",
            "title": "未完成任务",
            "status": "open",
            "priority": 2,
            "plan_date": "2026-03-26",
            "deadline_at": "2026-03-26T10:00:00",
        })
        db.insert_item({
            "id": "task2",
            "owner_id": owner_id,
            "type": "task",
            "title": "今日任务",
            "status": "open",
            "priority": 1,
            "plan_date": "2026-03-25",
            "deadline_at": "2026-03-25T18:00:00",
        })
        db.insert_item({
            "id": "task3",
            "owner_id": owner_id,
            "type": "task",
            "title": "已完成任务",
            "status": "done",
            "priority": 3,
            "completed_at": "2026-03-24T21:00:00",
            "updated_at": "2026-03-24T21:00:00",
        })

        db.insert_item({
            "id": "ledger1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "午饭",
            "amount": 25.5,
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-20",
        })
        db.insert_item({
            "id": "ledger2",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "工资",
            "amount": 3000,
            "transaction_type": "income",
            "ledger_category": "工资",
            "ledger_date": "2026-03-21",
        })

        db.insert_item({
            "id": "diary1",
            "owner_id": owner_id,
            "type": "diary",
            "title": "三月日记",
            "content": "记录一下",
            "diary_date": "2026-03-05",
        })

        result = build_dashboard_overview(
            db=db,
            owner_id=owner_id,
            now=datetime(2026, 3, 25, 9, 30, 0),
        )

        assert result["summary"]["events_month"] == 3
        assert result["summary"]["tasks_pending"] == 2
        assert result["summary"]["tasks_done_recent"] == 1
        assert result["summary"]["ledger_month_expense"] == 25.5
        assert result["summary"]["diary_month"] == 1
        assert len(result["events_month"]) == 3
        assert result["events_month"][0]["title"] == "FRB2026会议"
        assert result["events_month"][0]["display_subtitle"] == "摘要截止"
        assert result["events_month"][0]["start_time"] == "2026-03-05T09:00:00"
        assert all(event["start_time"][:10] <= "2026-03-31" for event in result["events_month"])
        assert any(
            event["title"] == "FRB2026会议"
            and event["display_subtitle"] == "会议开始"
            and event["start_time"] == "2026-04-01T10:00:00"
            for event in result["events_agenda"]
        )
        assert len(result["tasks"]["active"]) == 2
        assert result["tasks"]["active"][0]["title"] == "今日任务"
        assert len(result["tasks"]["completed"]) == 1
        assert result["tasks"]["completed"][0]["title"] == "已完成任务"
        assert result["month_summary"]["income"] == 3000
        assert result["month_summary"]["expense"] == 25.5
        assert len(result["recent_ledger"]) == 2
        assert result["spending_trend"][0] == {"date": "2026-03-01", "amount": 0.0}
        assert result["spending_trend"][-1] == {"date": "2026-03-25", "amount": 0.0}
        assert any(point == {"date": "2026-03-20", "amount": 25.5} for point in result["spending_trend"])
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_dashboard_page_source_uses_month_events_and_completed_tasks():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")

    assert "本月日程" in src
    assert "最近完成" in src
    assert "events_month" in src
    assert "events_agenda" in src
    assert "display_subtitle" in src
    assert "tasks?.active" in src
    assert "tasks?.completed" in src
    assert "recent_ledger" in src


def test_dashboard_page_source_scales_summary_numbers_for_mid_width_layouts():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")

    assert "min-width: 0;" in src
    assert "font-size: clamp(22px, 1.8vw, 28px);" in src
    assert "overflow-wrap: anywhere;" in src
    assert "word-break: break-word;" in src
    assert ".dashboard-summary-card { padding: 14px 14px 12px; border-radius: 18px; }" in src
    assert ".dashboard-summary-value { font-size: 24px; }" in src


def test_dashboard_page_source_uses_sparse_spending_axis_ticks():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")

    assert "function buildSpendingAxisTicks(values)" in src
    assert "const ticks = [0, 0.33, 0.66, 1].map" in src
    assert "afterBuildTicks: (axis) => {" in src
    assert "axis.ticks = axisTicks.map(value => ({ value }));" in src
    assert "max: axisMax," in src
    assert "border: { display: false }," in src
    assert "borderDash: [4, 6]," in src
    assert "drawTicks: false," in src
