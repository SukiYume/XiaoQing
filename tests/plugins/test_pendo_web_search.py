"""Regression tests for the redesigned Pendo web search behavior."""

import importlib
from pathlib import Path
import shutil
import sys
import types
import uuid

from plugins.pendo.services.db import Database


ROOT = Path(__file__).resolve().parents[2]


def _load_search_module():
    fastapi = types.ModuleType("fastapi")

    class _Router:
        def _decorator(self, *_args, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

        def get(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = _Router
    fastapi.Depends = lambda dep=None: dep
    fastapi.HTTPException = _HTTPException

    sys.modules["fastapi"] = fastapi
    sys.modules.pop("plugins.pendo.web.api.search", None)
    return importlib.import_module("plugins.pendo.web.api.search")


def test_database_search_items_matches_additional_text_fields():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_search_{uuid.uuid4().hex}"
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


def test_database_search_items_supports_ledger_category_filter():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_search_ledger_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-search-ledger"

    try:
        db.insert_item({
            "id": "ld1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "餐饮消费",
            "amount": 20,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-28",
        })
        db.insert_item({
            "id": "ld2",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "交通消费",
            "amount": 8,
            "direction": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-03-28",
        })

        results = db.search_items(
            owner_id,
            "消费",
            filters={"type": "ledger", "ledger_category": "餐饮"},
            limit=10,
        )

        assert [item.id for item in results] == ["ld1"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_search_route_source_maps_dedicated_ledger_category_filter():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "search.py").read_text(encoding="utf-8")

    assert "ledger_category: Optional[str] = None" in src
    assert 'filters["ledger_category"] = ledger_category' in src


def test_search_page_source_uses_dedicated_ledger_category_param():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "search.js").read_text(encoding="utf-8")

    assert "params.ledger_category = _activeCategory;" in src
    assert "params.category = _activeCategory;" in src
