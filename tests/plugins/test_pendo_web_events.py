"""Regression tests for the redesigned Pendo web events page."""

from pathlib import Path
import importlib
import shutil
import sys
import types
import uuid

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.event_schedule import ensure_datetime
from plugins.pendo.web.analytics.events_overview import build_event_detail, build_events_overview


ROOT = Path(__file__).resolve().parents[2]


def _load_events_module():
    fastapi = types.ModuleType("fastapi")

    class _Router:
        def _decorator(self, *_args, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

        def get(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def post(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def put(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def delete(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = _Router
    fastapi.Depends = lambda dep=None: dep
    fastapi.HTTPException = _HTTPException
    fastapi.Header = lambda default=None, **_kwargs: default
    fastapi.Query = lambda default=None, **_kwargs: default
    fastapi.Request = type("Request", (), {})

    responses = types.ModuleType("fastapi.responses")
    responses.Response = type("Response", (), {})

    _orig_fastapi = sys.modules.get("fastapi")
    _orig_responses = sys.modules.get("fastapi.responses")
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules.pop("plugins.pendo.web.api.events", None)
    mod = importlib.import_module("plugins.pendo.web.api.events")

    if _orig_fastapi is not None:
        sys.modules["fastapi"] = _orig_fastapi
    else:
        sys.modules.pop("fastapi", None)
    if _orig_responses is not None:
        sys.modules["fastapi.responses"] = _orig_responses
    else:
        sys.modules.pop("fastapi.responses", None)
    return mod


def test_build_events_overview_supports_multi_node_recurring_and_reminder_filters():
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
        db.create_event_collection({
            "id": "ev2col",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "发布准备",
            "content": "",
            "category": "项目",
            "location": "",
            "notes": "跨三天推进",
            "start_time": "2026-03-12T09:00:00",
            "end_time": "2026-03-14T20:00:00",
        })
        db.insert_item({
            "id": "ev2col_m01",
            "owner_id": owner_id,
            "type": "event",
            "title": "提审",
            "category": "项目",
            "start_time": "2026-03-13T18:00:00",
            "remind_times": ["2026-03-13T17:00:00"],
            "notes": "节点备注",
            "event_role": "multi_node_child",
            "event_collection_id": "ev2col",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        })
        db.create_event_collection({
            "id": "series",
            "owner_id": owner_id,
            "kind": "recurring",
            "title": "周会",
            "content": "",
            "category": "会议",
            "rrule": "FREQ=WEEKLY",
            "start_time": "2026-03-18T10:00:00",
            "end_time": "2026-03-18T11:00:00",
        })
        db.insert_item({
            "id": "series_20260318",
            "owner_id": owner_id,
            "type": "event",
            "title": "周会",
            "category": "会议",
            "start_time": "2026-03-18T10:00:00",
            "end_time": "2026-03-18T11:00:00",
            "remind_times": ["2026-03-18T09:30:00"],
            "event_role": "recurring_occurrence",
            "event_collection_id": "series",
            "event_collection_kind": "recurring",
            "event_index": 1,
            "event_node_key": "20260318",
        })

        db.log_reminder("ev1", "2026-03-10T08:00:00", sent=True)
        db.confirm_reminder("ev1", remind_time="2026-03-10T08:00:00")
        db.log_reminder("ev2col_m01", "2026-03-13T17:00:00", sent=True)

        result = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-03-01",
            end_date="2026-03-31",
        )

        assert result["summary"]["event_count"] == 3
        assert result["summary"]["multi_node_count"] == 1
        assert result["summary"]["recurring_count"] == 1
        assert result["summary"]["reminder_count"] == 4
        assert result["calendar_days"]["2026-03-11"]["has_events"] is False
        assert result["calendar_days"]["2026-03-12"]["has_events"] is False
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
        assert sent_only["events"][0]["id"] == "ev2col_m01"

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


def test_build_events_overview_includes_each_day_for_multi_day_events():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_events_multiday_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-events-multiday"

    try:
        db.insert_item({
            "id": "ev_multiday",
            "owner_id": owner_id,
            "type": "event",
            "title": "跨天出差",
            "category": "工作",
            "start_time": "2026-03-10T22:00:00",
            "end_time": "2026-03-12T03:00:00",
        })

        result = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-03-10",
            end_date="2026-03-12",
        )

        assert result["events"][0]["display_days"] == ["2026-03-10", "2026-03-11", "2026-03-12"]
        assert result["calendar_days"]["2026-03-11"]["has_events"] is True
        assert [row["date"] for row in result["timeline_days"]] == [
            "2026-03-10",
            "2026-03-11",
            "2026-03-12",
        ]
        assert result["timeline_days"][1]["items"][0]["time_label"] == "跨天"
        assert result["timeline_days"][2]["items"][0]["time_label"] == "至 03:00"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_event_schedule_converts_offset_aware_datetimes_to_default_local_time():
    parsed = ensure_datetime("2026-04-29T00:30:00+00:00")

    assert parsed is not None
    assert parsed.isoformat(timespec="seconds") == "2026-04-29T08:30:00"


def test_build_event_detail_includes_reminder_logs_and_related_instances():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_detail_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-detail"

    try:
        db.create_event_collection({
            "id": "series",
            "owner_id": owner_id,
            "kind": "recurring",
            "title": "周会",
            "content": "",
            "category": "会议",
            "rrule": "FREQ=WEEKLY",
            "start_time": "2026-03-18T10:00:00",
            "end_time": "2026-03-25T11:00:00",
        })
        db.insert_item({
            "id": "series_20260318",
            "owner_id": owner_id,
            "type": "event",
            "title": "周会",
            "category": "会议",
            "start_time": "2026-03-18T10:00:00",
            "end_time": "2026-03-18T11:00:00",
            "remind_times": ["2026-03-18T09:30:00"],
            "event_role": "recurring_occurrence",
            "event_collection_id": "series",
            "event_collection_kind": "recurring",
            "event_index": 1,
            "event_node_key": "20260318",
        })
        db.insert_item({
            "id": "series_20260325",
            "owner_id": owner_id,
            "type": "event",
            "title": "周会",
            "category": "会议",
            "start_time": "2026-03-25T10:00:00",
            "end_time": "2026-03-25T11:00:00",
            "remind_times": ["2026-03-25T09:30:00"],
            "event_role": "recurring_occurrence",
            "event_collection_id": "series",
            "event_collection_kind": "recurring",
            "event_index": 2,
            "event_node_key": "20260325",
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


def test_build_event_detail_preserves_multi_node_leaf_notes():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_milestone_notes_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-milestone-notes"

    try:
        db.create_event_collection({
            "id": "milestone-detail",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "线下会议",
            "content": "",
            "category": "会议",
            "notes": "全局备注",
            "start_time": "2026-04-22T12:43:00",
            "end_time": "2026-04-26T12:00:00",
        })
        db.insert_item({
            "id": "milestone-detail_m01",
            "owner_id": owner_id,
            "type": "event",
            "title": "会议开始",
            "category": "会议",
            "start_time": "2026-04-22T12:43:00",
            "notes": "北京南 G823，7车5F 坐",
            "remind_times": ["2026-04-21T12:43:00", "2026-04-25T12:00:00"],
            "event_role": "multi_node_child",
            "event_collection_id": "milestone-detail",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        })

        detail = build_event_detail(db=db, owner_id=owner_id, event_id="milestone-detail_m01")

        assert detail is not None
        assert detail["event"]["notes"] == "北京南 G823，7车5F 坐"
        assert detail["event"]["collection"]["notes"] == "全局备注"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_events_overview_and_detail_return_collection_context_for_leaf_events():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_graph_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-web-event-graph"

    try:
        collection_id = db.create_event_collection(
            {
                "id": "colgraph",
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": "发布项目",
                "category": "项目",
                "start_time": "2030-05-01T10:00:00",
                "end_time": "2030-05-02T18:00:00",
            }
        )
        db.insert_item(
            {
                "id": "colgraph_m01",
                "owner_id": owner_id,
                "type": "event",
                "title": "提审",
                "category": "项目",
                "start_time": "2030-05-01T10:00:00",
                "reminder_rules": [{"offset_seconds": 0}],
                "remind_times": ["2030-05-01T10:00:00"],
                "event_role": "multi_node_child",
                "event_collection_id": collection_id,
                "event_collection_kind": "multi_node",
                "event_index": 1,
            }
        )
        db.insert_item(
            {
                "id": "colgraph_m02",
                "owner_id": owner_id,
                "type": "event",
                "title": "上线",
                "category": "项目",
                "start_time": "2030-05-02T18:00:00",
                "reminder_rules": [{"offset_seconds": 0}],
                "remind_times": ["2030-05-02T18:00:00"],
                "event_role": "multi_node_child",
                "event_collection_id": collection_id,
                "event_collection_kind": "multi_node",
                "event_index": 2,
            }
        )

        overview = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2030-05-01",
            end_date="2030-05-31",
        )

        assert overview["summary"]["event_count"] == 2
        assert overview["summary"]["multi_node_count"] == 2
        assert overview["events"][0]["collection"]["title"] == "发布项目"
        assert overview["events"][0]["kind"] == "multi_node"
        assert overview["timeline_days"][0]["items"][0]["event_id"] == "colgraph_m01"

        detail = build_event_detail(db=db, owner_id=owner_id, event_id="colgraph_m01")
        assert detail is not None
        assert detail["event"]["collection"]["id"] == collection_id
        assert detail["related_instances"][0]["id"] == "colgraph_m02"

        collection_detail = build_event_detail(db=db, owner_id=owner_id, event_id=collection_id)
        assert collection_detail is not None
        assert collection_detail["collection"]["id"] == collection_id
        assert [child["id"] for child in collection_detail["children"]] == [
            "colgraph_m01",
            "colgraph_m02",
        ]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_events_collection_api_creates_updates_and_deletes_graph():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_graph_api_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-web-event-graph-api"
    events_api = _load_events_module()

    try:
        created = events_api.create_event_collection(
            body=events_api.EventCollectionCreate(
                title="发布项目",
                category="项目",
                location="线上",
                reminder_rules=[{"offset_seconds": 3600}, {"offset_seconds": 0}],
                children=[
                    events_api.EventCollectionChildCreate(
                        title="提审",
                        start_time="2030-05-01T10:00:00",
                    ),
                    events_api.EventCollectionChildCreate(
                        title="上线",
                        start_time="2030-05-02T18:00:00",
                    ),
                ],
            ),
            owner_id=owner_id,
            db=db,
        )

        collection_id = created["data"]["id"]
        child_ids = created["data"]["child_ids"]
        assert child_ids == [f"{collection_id}_m01", f"{collection_id}_m02"]
        assert db.get_item(child_ids[0], owner_id).remind_times == [
            "2030-05-01T09:00:00",
            "2030-05-01T10:00:00",
        ]

        detail = events_api.get_collection_detail(collection_id, owner_id=owner_id, db=db)
        assert detail["data"]["collection"]["title"] == "发布项目"
        assert [child["id"] for child in detail["data"]["children"]] == child_ids

        updated = events_api.update_collection(
            collection_id,
            body=events_api.EventCollectionUpdate(title="发布项目 v2"),
            owner_id=owner_id,
            db=db,
        )
        assert updated["ok"] is True
        assert db.get_event_collection(collection_id, owner_id)["title"] == "发布项目 v2"

        deleted = events_api.delete_collection(collection_id, owner_id=owner_id, db=db)
        assert deleted["ok"] is True
        assert db.get_event_collection(collection_id, owner_id) is None
        assert db.get_item(child_ids[0], owner_id) is None
        assert db.get_item(child_ids[1], owner_id) is None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_events_collection_api_rejects_invalid_child_without_partial_writes():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_graph_invalid_child_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-web-event-graph-invalid-child"
    events_api = _load_events_module()

    try:
        with pytest.raises(events_api.HTTPException) as exc_info:
            events_api.create_event_collection(
                body=events_api.EventCollectionCreate(
                    title="发布项目",
                    children=[
                        events_api.EventCollectionChildCreate(
                            title="提审",
                            start_time="2030-05-01T10:00:00",
                        ),
                        events_api.EventCollectionChildCreate(
                            title="上线",
                            start_time="not-a-date",
                        ),
                    ],
                ),
                owner_id=owner_id,
                db=db,
            )

        assert exc_info.value.status_code == 422
        assert "Invalid start_time" in exc_info.value.detail
        assert db.get_connection().execute(
            "SELECT COUNT(*) FROM event_collections WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()[0] == 0
        assert db.get_connection().execute(
            "SELECT COUNT(*) FROM items WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()[0] == 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_events_collection_update_rejects_invalid_reminder_rules():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_event_graph_invalid_rules_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-web-event-graph-invalid-rules"
    events_api = _load_events_module()

    try:
        created = events_api.create_event_collection(
            body=events_api.EventCollectionCreate(
                title="发布项目",
                children=[
                    events_api.EventCollectionChildCreate(
                        title="提审",
                        start_time="2030-05-01T10:00:00",
                    ),
                    events_api.EventCollectionChildCreate(
                        title="上线",
                        start_time="2030-05-02T18:00:00",
                    ),
                ],
            ),
            owner_id=owner_id,
            db=db,
        )

        with pytest.raises(events_api.HTTPException) as exc_info:
            events_api.update_collection(
                created["data"]["id"],
                body=events_api.EventCollectionUpdate(
                    reminder_rules=[{"offset_seconds": -120}],
                ),
                owner_id=owner_id,
                db=db,
            )

        assert exc_info.value.status_code == 422
        assert "offset_seconds" in exc_info.value.detail
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
        db.create_event_collection({
            "id": "visible_nodes",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "五月节点",
            "content": "",
            "category": "项目",
            "start_time": "2026-04-29T09:00:00",
            "end_time": "2026-05-20T10:00:00",
        })
        db.insert_item({
            "id": "visible_nodes_m01",
            "owner_id": owner_id,
            "type": "event",
            "title": "前置",
            "category": "项目",
            "start_time": "2026-04-29T09:00:00",
            "remind_times": ["2026-05-01T08:00:00"],
            "event_role": "multi_node_child",
            "event_collection_id": "visible_nodes",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        })
        db.insert_item({
            "id": "visible_nodes_m02",
            "owner_id": owner_id,
            "type": "event",
            "title": "发布",
            "category": "项目",
            "start_time": "2026-05-20T10:00:00",
            "remind_times": [
                "2026-04-29T08:00:00",
                "2026-05-20T09:00:00",
            ],
            "event_role": "multi_node_child",
            "event_collection_id": "visible_nodes",
            "event_collection_kind": "multi_node",
            "event_index": 2,
            "event_node_key": "m02",
        })

        result = build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-05-01",
            end_date="2026-05-31",
        )

        assert result["summary"]["event_count"] == 1
        assert result["summary"]["multi_node_count"] == 1
        assert result["summary"]["reminder_count"] == 1
        assert result["calendar_days"]["2026-05-20"]["count"] == 1
        assert result["events"][0]["id"] == "visible_nodes_m02"
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
    assert '"/events/collections"' in api_src
    assert '"/events/collections/{collection_id}/detail"' in api_src
    assert "milestones: Optional[list[dict]] = None" not in items_src
    assert "/events/overview" in page_src
    assert "/events/${eventId}/detail" in page_src
    assert "/events/collections" in page_src
    assert "reminder_rules" in page_src
    assert "openCollectionDetail" in page_src
    assert "deleteCollection" in page_src
    assert "多节点事件" in page_src
    assert "timeline_days" in page_src
    assert "function detailActionNoun(event)" in page_src
    assert "event?.kind === 'multi_node'" in page_src
    assert "event?.kind === 'recurring'" in page_src
    assert "编辑节点</button>" not in page_src
    assert "删除节点</button>" not in page_src


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
