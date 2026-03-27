"""Regression tests for the redesigned Pendo web events page."""

from pathlib import Path
import shutil
import uuid

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.events_overview import build_event_detail, build_events_overview


ROOT = Path(__file__).resolve().parents[2]


def test_build_events_overview_supports_milestones_recurring_and_reminder_filters():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_events_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-events"

    try:
        db.insert_item({
            "id": "ev1",
            "owner_id": owner_id,
            "type": "event",
            "title": "产品评审",
            "category": "会议",
            "start_time": "2026-03-10T09:00:00",
            "end_time": "2026-03-10T10:00:00",
            "location": "A1",
            "remind_times": ["2026-03-10T08:00:00", "2026-03-10T09:00:00"],
        })
        db.insert_item({
            "id": "ev2",
            "owner_id": owner_id,
            "type": "event",
            "title": "发布准备",
            "category": "项目",
            "start_time": "2026-03-12T09:00:00",
            "end_time": "2026-03-14T20:00:00",
            "milestones": [
                {"name": "素材冻结", "time": "2026-03-12T09:00:00"},
                {"name": "提审", "time": "2026-03-13T18:00:00"},
                {"name": "上线", "time": "2026-03-14T10:00:00"},
            ],
            "remind_times": ["2026-03-13T17:00:00"],
            "notes": "跨三天推进",
        })
        db.insert_item({
            "id": "series_20260318",
            "owner_id": owner_id,
            "type": "event",
            "title": "周会",
            "category": "会议",
            "start_time": "2026-03-18T10:00:00",
            "end_time": "2026-03-18T11:00:00",
            "parent_id": "series",
            "rrule": "FREQ=WEEKLY",
            "remind_times": ["2026-03-18T09:30:00"],
        })

        db.log_reminder("ev1", "2026-03-10T08:00:00", sent=True)
        db.confirm_reminder("ev1", remind_time="2026-03-10T08:00:00")
        db.log_reminder("ev2", "2026-03-13T17:00:00", sent=True)

        result = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-03-01",
            end_date="2026-03-31",
        )

        assert result["summary"]["event_count"] == 3
        assert result["summary"]["milestone_count"] == 1
        assert result["summary"]["recurring_count"] == 1
        assert result["summary"]["reminder_count"] == 4
        assert result["calendar_days"]["2026-03-11"]["has_events"] is False
        assert result["calendar_days"]["2026-03-12"]["has_events"] is True
        assert result["calendar_days"]["2026-03-13"]["has_events"] is True
        assert result["calendar_days"]["2026-03-13"]["items"][0]["label"] == "提审"
        assert any(day["date"] == "2026-03-10" for day in result["timeline_days"])

        sent_only = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-03-01",
            end_date="2026-03-31",
            reminder="sent",
        )
        assert sent_only["summary"]["event_count"] == 1
        assert sent_only["events"][0]["id"] == "ev2"

        meeting_only = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-03-01",
            end_date="2026-03-31",
            category="会议",
            kind="recurring",
        )
        assert meeting_only["summary"]["event_count"] == 1
        assert meeting_only["events"][0]["id"] == "series_20260318"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_event_detail_includes_reminder_logs_and_related_instances():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_detail_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-detail"

    try:
        db.insert_item({
            "id": "series_20260318",
            "owner_id": owner_id,
            "type": "event",
            "title": "周会",
            "category": "会议",
            "start_time": "2026-03-18T10:00:00",
            "end_time": "2026-03-18T11:00:00",
            "parent_id": "series",
            "rrule": "FREQ=WEEKLY",
            "remind_times": ["2026-03-18T09:30:00"],
        })
        db.insert_item({
            "id": "series_20260325",
            "owner_id": owner_id,
            "type": "event",
            "title": "周会",
            "category": "会议",
            "start_time": "2026-03-25T10:00:00",
            "end_time": "2026-03-25T11:00:00",
            "parent_id": "series",
            "rrule": "FREQ=WEEKLY",
            "remind_times": ["2026-03-25T09:30:00"],
        })
        db.log_reminder("series_20260318", "2026-03-18T09:30:00", sent=True)

        detail = build_event_detail(db=db, owner_id=owner_id, event_id="series_20260318")

        assert detail is not None
        assert detail["event"]["kind"] == "recurring"
        assert detail["event"]["reminders"][0]["status"] == "sent"
        assert len(detail["related_instances"]) == 1
        assert detail["related_instances"][0]["id"] == "series_20260325"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_events_overview_counts_only_visible_nodes_and_in_range_reminders():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_events_visible_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-events-visible"

    try:
        db.insert_item({
            "id": "visible_milestone",
            "owner_id": owner_id,
            "type": "event",
            "title": "五月节点",
            "category": "项目",
            "start_time": "2026-04-28T09:00:00",
            "end_time": "2026-05-20T18:00:00",
            "milestones": [
                {"name": "前置", "time": "2026-04-29T09:00:00"},
                {"name": "发布", "time": "2026-05-20T10:00:00"},
            ],
            "remind_times": [
                "2026-04-29T08:00:00",
                "2026-05-20T09:00:00",
            ],
        })
        db.insert_item({
            "id": "hidden_milestone",
            "owner_id": owner_id,
            "type": "event",
            "title": "不应出现在五月摘要",
            "category": "项目",
            "start_time": "2026-04-28T09:00:00",
            "end_time": "2026-05-08T18:00:00",
            "milestones": [
                {"name": "四月节点", "time": "2026-04-30T09:00:00"},
            ],
            "remind_times": [
                "2026-04-30T08:00:00",
                "2026-05-01T08:00:00",
                "2026-05-02T08:00:00",
            ],
        })

        result = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-05-01",
            end_date="2026-05-31",
        )

        assert result["summary"]["event_count"] == 1
        assert result["summary"]["milestone_count"] == 1
        assert result["summary"]["reminder_count"] == 1
        assert result["calendar_days"]["2026-05-20"]["count"] == 1
        assert result["events"][0]["id"] == "visible_milestone"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_events_page_source_uses_event_overview_routes():
    api_src = (ROOT / "plugins" / "pendo" / "web" / "api" / "events.py").read_text(encoding="utf-8")
    items_src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "events.js").read_text(encoding="utf-8")

    assert '"/events/overview"' in api_src
    assert '"/events/{event_id}/detail"' in api_src
    assert "milestones: Optional[list[dict]] = None" in items_src
    assert "/events/overview" in page_src
    assert "/events/${eventId}/detail" in page_src
    assert "多节点事件" in page_src
    assert "timeline_days" in page_src
