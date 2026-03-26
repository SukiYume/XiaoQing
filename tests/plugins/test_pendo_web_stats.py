"""Regression tests for Pendo web stats aggregations."""

import importlib
from pathlib import Path
import shutil
import sys
import types
import uuid

from plugins.pendo.services.db import Database


ROOT = Path(__file__).resolve().parents[2]


def _load_stats_module():
    fastapi = types.ModuleType("fastapi")

    class _Router:
        def get(self, *_args, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

    fastapi.APIRouter = _Router
    fastapi.Depends = lambda dep=None: dep
    fastapi.Query = lambda default=None, **_kwargs: default
    fastapi.Header = lambda default=None, **_kwargs: default

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = _HTTPException

    sys.modules["fastapi"] = fastapi
    sys.modules.pop("plugins.pendo.web.api.stats", None)
    return importlib.import_module("plugins.pendo.web.api.stats")


def test_ledger_stats_returns_expense_amount_histogram():
    temp_dir = ROOT / ".pytest_tmp" / f"pendo_web_stats_ledger_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-stats"
    stats_module = _load_stats_module()

    try:
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "早餐",
            "amount": 12,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-10",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "打车",
            "amount": 48,
            "direction": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-03-11",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "房租",
            "amount": 1200,
            "direction": "expense",
            "ledger_category": "居住",
            "ledger_date": "2026-03-12",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "工资",
            "amount": 5000,
            "direction": "income",
            "ledger_category": "工资",
            "ledger_date": "2026-03-15",
        })

        result = stats_module.ledger_stats(range="2026-03-01..2026-03-31", owner_id=owner_id, db=db)
        histogram = {item["bucket"]: item["count"] for item in result["data"]["expense_amount_histogram"]}

        assert histogram["0-20"] == 1
        assert histogram["20-50"] == 1
        assert histogram["1000+"] == 1
        assert sum(histogram.values()) == 3
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_event_stats_filters_range_and_builds_weekday_slot_matrix():
    temp_dir = ROOT / ".pytest_tmp" / f"pendo_web_stats_event_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-stats"
    stats_module = _load_stats_module()

    try:
        db.insert_item({
            "type": "event",
            "owner_id": owner_id,
            "title": "下午会议",
            "category": "工作",
            "start_time": "2026-03-31T14:00:00",
            "end_time": "2026-03-31T15:00:00",
        })
        db.insert_item({
            "type": "event",
            "owner_id": owner_id,
            "title": "晨间回顾",
            "category": "个人",
            "start_time": "2026-03-31T09:30:00",
            "end_time": "2026-03-31T10:00:00",
        })
        db.insert_item({
            "type": "event",
            "owner_id": owner_id,
            "title": "范围外事件",
            "category": "工作",
            "start_time": "2026-04-01T09:00:00",
            "end_time": "2026-04-01T10:00:00",
        })

        result = stats_module.event_stats(range="2026-03-31..2026-03-31", owner_id=owner_id, db=db)
        weekly_total = sum(item["count"] for item in result["data"]["weekly"])
        by_category = {item["category"]: item["count"] for item in result["data"]["by_category"]}
        weekday_slots = result["data"]["weekday_slots"]

        assert weekly_total == 2
        assert by_category == {"个人": 1, "工作": 1}
        assert sum(item["count"] for item in weekday_slots) == 2
        assert any(item["slot"] == "09-12" and item["count"] == 1 for item in weekday_slots)
        assert any(item["slot"] == "14-18" and item["count"] == 1 for item in weekday_slots)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_task_stats_separates_created_and_completed_weeks():
    temp_dir = ROOT / ".pytest_tmp" / f"pendo_web_stats_task_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-task-stats"
    stats_module = _load_stats_module()

    try:
        db.insert_item({
            "type": "task",
            "owner_id": owner_id,
            "title": "任务一",
            "status": "done",
            "category": "工作",
            "created_at": "2026-03-03T09:00:00",
            "completed_at": "2026-03-10T10:00:00",
        })
        db.insert_item({
            "type": "task",
            "owner_id": owner_id,
            "title": "任务二",
            "status": "todo",
            "category": "生活",
            "created_at": "2026-03-17T09:00:00",
        })

        result = stats_module.task_stats(range="2026-03-01..2026-03-31", owner_id=owner_id, db=db)
        weekly = {item["week"]: item for item in result["data"]["weekly"]}
        totals = result["data"]["totals"]

        assert weekly["2026-W09"]["total"] == 1
        assert weekly["2026-W09"]["done"] == 0
        assert weekly["2026-W11"]["total"] == 1
        assert weekly["2026-W11"]["done"] == 0
        assert weekly["2026-W10"]["done"] == 1
        assert weekly["2026-W10"]["total"] == 0
        assert totals["done"] == 1
        assert totals["todo"] == 1
        assert result["data"]["new_this_week"] == 2
        assert result["data"]["by_category"][0]["category"] in {"工作", "生活"}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_parse_range_supports_last_year():
    stats_module = _load_stats_module()
    start, end = stats_module._parse_range("last_year")
    current_year = __import__("datetime").datetime.now().year

    assert start == f"{current_year - 1}-01-01"
    assert end == f"{current_year - 1}-12-31"
