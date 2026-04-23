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


def test_build_event_detail_preserves_milestone_notes():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_milestone_notes_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-milestone-notes"

    try:
        db.insert_item({
            "id": "milestone-detail",
            "owner_id": owner_id,
            "type": "event",
            "title": "线下会议",
            "category": "会议",
            "start_time": "2026-04-22T12:43:00",
            "end_time": "2026-04-26T12:00:00",
            "notes": "全局备注",
            "milestones": [
                {
                    "name": "会议开始",
                    "time": "2026-04-22T12:43:00",
                    "notes": "北京南 G823，7车5F 坐",
                },
                {
                    "name": "会议结束",
                    "time": "2026-04-26T12:00:00",
                },
            ],
            "remind_times": ["2026-04-21T12:43:00", "2026-04-25T12:00:00"],
        })

        detail = build_event_detail(db=db, owner_id=owner_id, event_id="milestone-detail")

        assert detail is not None
        assert detail["event"]["milestones"][0]["notes"] == "北京南 G823，7车5F 坐"
        assert "notes" not in detail["event"]["milestones"][1]
        assert detail["event"]["notes"] == "全局备注"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_events_overview_batches_reminder_log_reads(monkeypatch):
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_events_batch_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-events-batch"

    try:
        for event_id, start_time in [
            ("batch_ev1", "2026-03-10T09:00:00"),
            ("batch_ev2", "2026-03-11T09:00:00"),
            ("batch_ev3", "2026-03-12T09:00:00"),
        ]:
            end_time = start_time.replace("09:00:00", "10:00:00")
            db.insert_item({
                "id": event_id,
                "owner_id": owner_id,
                "type": "event",
                "title": event_id,
                "category": "会议",
                "start_time": start_time,
                "end_time": end_time,
                "remind_times": [start_time.replace("09:00:00", "08:00:00")],
            })

        db.log_reminder("batch_ev1", "2026-03-10T08:00:00", sent=True)
        db.log_reminder("batch_ev2", "2026-03-11T08:00:00", sent=True)

        from plugins.pendo.web.analytics import events_overview as events_overview_module

        original_fetch = events_overview_module._fetch_reminder_logs_by_event_ids
        call_info = {"count": 0, "event_ids": []}

        def wrapped_fetch(db_obj, event_ids):
            call_info["count"] += 1
            call_info["event_ids"] = list(event_ids)
            return original_fetch(db_obj, event_ids)

        monkeypatch.setattr(
            events_overview_module,
            "_fetch_reminder_logs_by_event_ids",
            wrapped_fetch,
        )

        def fail_get_reminder_logs(*args, **kwargs):
            raise AssertionError("build_events_overview should batch reminder log reads")

        monkeypatch.setattr(Database, "get_reminder_logs", fail_get_reminder_logs)

        result = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-03-01",
            end_date="2026-03-31",
        )

        assert call_info["count"] == 1
        assert sorted(call_info["event_ids"]) == ["batch_ev1", "batch_ev2", "batch_ev3"]
        assert result["summary"]["event_count"] == 3
        assert result["summary"]["reminder_count"] == 3
        assert result["events"][0]["reminders"][0]["status"] == "sent"
        assert result["events"][1]["reminders"][0]["status"] == "sent"
        assert result["events"][2]["reminders"][0]["status"] == "pending"
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


def test_build_events_overview_accepts_offset_aware_imported_events():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_events_offset_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-events-offset"

    try:
        db.insert_item({
            "id": "aware_event",
            "owner_id": owner_id,
            "type": "event",
            "title": "带时区的导入日程",
            "category": "导入",
            "start_time": "2026-01-21T22:27:00+08:00",
            "end_time": "2026-01-21T23:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "remind_times": ["2026-01-21T22:27:00+08:00"],
            "created_at": "2026-01-21T22:27:00+08:00",
            "updated_at": "2026-01-21T22:27:00+08:00",
        })

        result = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-01-01",
            end_date="2026-01-31",
        )

        assert result["summary"]["event_count"] == 1
        assert result["events"][0]["id"] == "aware_event"
        assert result["calendar_days"]["2026-01-21"]["has_events"] is True
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


def test_events_page_source_uses_unified_time_presets():
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "events.js").read_text(encoding="utf-8")

    assert "RANGE_PRESET_OPTIONS" in page_src
    assert "option.key" in page_src
    assert "async function fetchAllRangeBounds()" in page_src
    assert "listRange: 'month'" in page_src


def test_events_page_source_uses_compact_calendar_summary_layout():
    page_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "events.js").read_text(encoding="utf-8")

    assert "function calendarVisibleItemLimit()" in page_src
    assert "const visibleLimit = calendarVisibleItemLimit();" in page_src
    assert "const visibleItems = items.slice(0, visibleLimit);" in page_src
    assert ".events-calendar-chip-text {" in page_src
    assert 'class="events-calendar-overflow">+${count - visibleItems.length}<span class="events-calendar-overflow-suffix"> 更多</span>' in page_src
    assert "BREAKPOINTS.XL" in page_src
    assert "BREAKPOINTS.EVENTS" not in page_src
    assert "events-calendar-summary" not in page_src
    assert "共 ${count} 条安排" not in page_src
    assert "aspect-ratio: 1 / 1;" in page_src
    assert "justify-content: flex-start;" in page_src
    assert ".events-hero-actions .events-summary-chip { width: auto; flex: 0 0 auto; }" in page_src
    assert ".events-calendar-items { gap: 4px; margin-top: 0; }" in page_src
    assert ".events-calendar-chip::before { width: calc(100% - 12px); height: 6px; border-radius: 999px; margin-left: 2px; }" in page_src
    assert ".events-calendar-chip-text { display: none; }" in page_src
    assert ".events-calendar-cell { min-height: 62px; padding: 4px; border-radius: 14px; }" in page_src
    assert ".events-calendar-overflow-suffix { display: none; }" in page_src
    assert ".events-summary-chips .events-summary-chip { width: 100%; justify-content: center; padding: 0 8px; font-size: 10px; }" in page_src
