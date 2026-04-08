"""Regression tests for the redesigned Pendo web notes page analytics."""

import importlib
from pathlib import Path
import shutil
import uuid

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics import notes_overview as notes_overview_module
from plugins.pendo.web.analytics.notes_overview import build_notes_overview


ROOT = Path(__file__).resolve().parents[2]


def test_build_notes_overview_tracks_categories_tags_and_cadence():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_notes_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-note-overview"

    try:
        db.insert_item({
            "id": "n1",
            "owner_id": owner_id,
            "type": "note",
            "title": "项目复盘",
            "content": "这是第一条笔记",
            "category": "工作",
            "tags": ["复盘", "工作"],
            "created_at": "2026-03-24T10:00:00",
            "updated_at": "2026-03-25T08:00:00",
        })
        db.insert_item({
            "id": "n2",
            "owner_id": owner_id,
            "type": "note",
            "title": "阅读摘录",
            "content": "第二条笔记更长一些",
            "category": "学习",
            "tags": ["阅读"],
            "created_at": "2026-03-26T09:00:00",
            "updated_at": "2026-03-26T09:30:00",
        })

        result = build_notes_overview(db=db, owner_id=owner_id, today="2026-03-26")

        assert result["summary"]["total_count"] == 2
        assert result["summary"]["week_new_count"] == 2
        assert result["categories"][0]["category"] in {"工作", "学习"}
        assert result["hot_tags"][0]["count"] == 1
        assert result["recent_notes"][0]["id"] == "n2"
        assert len(result["cadence"]) == 14
        assert "工作" in result["all_categories"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_notes_overview_clips_current_period_cadence_to_today():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_notes_current_period_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-note-current-period"

    class _FrozenDateTime(importlib.import_module("datetime").datetime):
        @classmethod
        def now(cls):
            return cls(2026, 4, 8, 10, 0, 0)

    original_datetime = notes_overview_module.datetime
    notes_overview_module.datetime = _FrozenDateTime

    try:
        db.insert_item({
            "id": "n1",
            "owner_id": owner_id,
            "type": "note",
            "title": "本月笔记",
            "content": "本月内容",
            "category": "工作",
            "created_at": "2026-04-06T09:00:00",
            "updated_at": "2026-04-06T09:00:00",
        })

        result = build_notes_overview(
            db=db,
            owner_id=owner_id,
            today="2026-04-08",
            start_date="2026-04-01",
            end_date="2026-04-30",
        )

        assert result["summary"]["range_start"] == "2026-04-01"
        assert result["summary"]["range_end"] == "2026-04-30"
        assert result["cadence_granularity"] == "week"
        assert [item["date"] for item in result["cadence"]] == ["2026-03-30", "2026-04-06"]
        assert [item["count"] for item in result["cadence"]] == [0, 1]
    finally:
        notes_overview_module.datetime = original_datetime
        shutil.rmtree(temp_dir, ignore_errors=True)
