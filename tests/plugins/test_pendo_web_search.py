"""Regression tests for the redesigned Pendo web search behavior."""

from pathlib import Path
import shutil
import uuid

from plugins.pendo.services.db import Database


ROOT = Path(__file__).resolve().parents[2]


def test_database_search_items_matches_additional_text_fields():
    temp_dir = ROOT / ".pytest_tmp" / f"pendo_web_search_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-search"

    try:
        db.insert_item({
            "id": "ev1",
            "owner_id": owner_id,
            "type": "event",
            "title": "组会",
            "content": "",
            "location": "图书馆 402",
            "start_time": "2026-03-28T09:00:00",
            "end_time": "2026-03-28T10:00:00",
        })
        db.insert_item({
            "id": "dy1",
            "owner_id": owner_id,
            "type": "diary",
            "title": "雨夜",
            "content": "今天走得很慢。",
            "weather": "🌧️ 雨",
            "notes": "窗边的风声",
            "diary_date": "2026-03-28",
        })

        by_location = db.search_items(owner_id, "图书馆", limit=10)
        by_weather = db.search_items(owner_id, "风声", filters={"type": "diary"}, limit=10)

        assert [item.id for item in by_location] == ["ev1"]
        assert [item.id for item in by_weather] == ["dy1"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_search_source_preserves_fts_then_like_order():
    src = (ROOT / "plugins" / "pendo" / "services" / "db.py").read_text(encoding="utf-8")

    assert "return [items_by_id[item_id] for item_id in merged_ids[:limit] if item_id in items_by_id]" in src
    assert "ORDER BY created_at DESC LIMIT ?" not in src
