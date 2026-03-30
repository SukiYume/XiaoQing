"""Regression tests for Pendo web stats aggregations."""

import importlib
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys
import types
import uuid

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.notes_overview import build_notes_overview


ROOT = Path(__file__).resolve().parents[2]


def _load_stats_module():
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

    fastapi.APIRouter = _Router
    fastapi.Depends = lambda dep=None: dep
    fastapi.Query = lambda default=None, **_kwargs: default
    fastapi.Header = lambda default=None, **_kwargs: default
    fastapi.Request = type("Request", (), {})

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = _HTTPException
    responses = types.ModuleType("fastapi.responses")
    responses.Response = type("Response", (), {})

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules.pop("plugins.pendo.web.api.stats", None)
    return importlib.import_module("plugins.pendo.web.api.stats")


def test_ledger_stats_returns_expense_amount_histogram():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_ledger_{uuid.uuid4().hex}"
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


def test_ledger_stats_respects_range_for_totals_categories_and_trend_data():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_ledger_range_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-range"
    stats_module = _load_stats_module()

    try:
        for item in [
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": "早餐",
                "amount": 18,
                "direction": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-02",
            },
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": "兼职",
                "amount": 300,
                "direction": "income",
                "ledger_category": "副业",
                "ledger_date": "2026-03-03",
            },
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": "范围外支出",
                "amount": 66,
                "direction": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-04-03",
            },
        ]:
            db.insert_item(item)

        result = stats_module.ledger_stats(range="2026-03-01..2026-03-31", owner_id=owner_id, db=db)
        data = result["data"]

        assert data["monthly"] == [{"month": "2026-03", "income": 300, "expense": 18}]
        assert data["daily"] == [
            {"date": "2026-03-02", "income": 0, "expense": 18},
            {"date": "2026-03-03", "income": 300, "expense": 0},
        ]
        assert data["expense_by_category"] == [{"category": "餐饮", "total": 18}]
        assert data["income_by_category"] == [{"category": "副业", "total": 300}]
        histogram = {item["bucket"]: item["count"] for item in data["expense_amount_histogram"]}
        assert histogram["0-20"] == 1
        assert sum(histogram.values()) == 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ledger_stats_all_range_stops_at_today_and_excludes_future_entries():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_ledger_all_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-all"
    stats_module = _load_stats_module()
    now = datetime.now()
    included_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    future_date = (now + timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "已发生支出",
            "amount": 28,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": included_date,
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "未来支出",
            "amount": 88,
            "direction": "expense",
            "ledger_category": "未来",
            "ledger_date": future_date,
        })

        result = stats_module.ledger_stats(range="all", owner_id=owner_id, db=db)
        data = result["data"]

        assert data["expense_by_category"] == [{"category": "餐饮", "total": 28}]
        assert all(item["date"] != future_date for item in data["daily"])
        assert sum(item["count"] for item in data["expense_amount_histogram"]) == 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_event_stats_filters_range_and_builds_weekday_slot_matrix():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_event_{uuid.uuid4().hex}"
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
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_task_{uuid.uuid4().hex}"
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

        assert weekly["2026-W09"]["created"] == 1
        assert weekly["2026-W09"]["done"] == 0
        assert weekly["2026-W11"]["created"] == 1
        assert weekly["2026-W11"]["done"] == 0
        assert weekly["2026-W10"]["done"] == 1
        assert weekly["2026-W10"]["created"] == 0
        assert totals["done"] == 1
        assert totals["open"] == 1
        assert result["data"]["new_this_week"] == 2
        assert result["data"]["by_category"] == [{"category": "生活", "count": 1}]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_diary_overview_accepts_explicit_range_and_auto_cadence():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_diary_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-diary-stats"
    stats_module = _load_stats_module()

    try:
        for item_id, diary_date, mood, content in [
            ("d1", "2026-01-06", "calm", "第一条记录"),
            ("d2", "2026-01-15", "happy", "第二条记录更长一些"),
            ("d3", "2026-02-03", "happy", "第三条记录"),
        ]:
            db.insert_item({
                "id": item_id,
                "type": "diary",
                "owner_id": owner_id,
                "title": item_id,
                "content": content,
                "diary_date": diary_date,
                "mood": mood,
                "created_at": f"{diary_date}T20:00:00",
                "updated_at": f"{diary_date}T20:00:00",
            })

        result = stats_module.diary_overview(
            start_date="2026-01-01",
            end_date="2026-02-15",
            cadence_granularity="auto",
            today="2026-02-15",
            owner_id=owner_id,
            db=db,
        )

        data = result["data"]
        assert data["summary"]["entry_count"] == 3
        assert data["summary"]["range_start"] == "2026-01-01"
        assert data["summary"]["range_end"] == "2026-02-15"
        assert data["cadence_granularity"] == "week"
        assert sum(item["count"] for item in data["cadence"]) == 3
        assert data["mood_breakdown"][0]["mood"] == "happy"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_notes_overview_accepts_explicit_range():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_notes_range_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-note-stats"

    try:
        for item_id, title, category, created_at in [
            ("n-in-1", "范围内一", "工作", "2026-03-03T09:00:00"),
            ("n-in-2", "范围内二", "生活", "2026-03-15T09:00:00"),
            ("n-out", "范围外", "归档", "2026-02-20T09:00:00"),
        ]:
            db.insert_item({
                "id": item_id,
                "owner_id": owner_id,
                "type": "note",
                "title": title,
                "content": f"{title} 内容",
                "category": category,
                "tags": ["alpha", "beta"] if item_id != "n-out" else ["legacy"],
                "created_at": created_at,
                "updated_at": created_at,
            })

        result = build_notes_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-03-01",
            end_date="2026-03-31",
            today="2026-03-31",
        )

        assert result["summary"]["total_count"] == 2
        assert {item["category"] for item in result["categories"]} == {"工作", "生活"}
        assert {item["tag"] for item in result["hot_tags"]} == {"alpha", "beta"}
        assert all(item["date"].startswith("2026-03") for item in result["cadence"])
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_stats_page_source_passes_note_range_params():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "api.get('/stats/notes/overview', { start_date: range.start, end_date: range.end, today: range.end })" in src


def test_parse_range_supports_last_year():
    stats_module = _load_stats_module()
    start, end = stats_module._parse_range("last_year")
    current_year = __import__("datetime").datetime.now().year

    assert start == f"{current_year - 1}-01-01"
    assert end == f"{current_year - 1}-12-31"


def test_parse_range_supports_all():
    stats_module = _load_stats_module()
    start, end = stats_module._parse_range("all")

    assert start == "1970-01-01"
    assert end == __import__("datetime").datetime.now().strftime("%Y-%m-%d")


def test_stats_page_source_uses_consistent_task_green_palette():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "const taskToneDone = '#166534';" in src
    assert "const taskToneOpen = '#16a34a';" in src
    assert "{ label: '未完成', count: totals.open, color: taskToneOpen }" in src
    assert "{ label: '已完成', count: totals.done, color: taskToneDone }" in src
    assert "浅绿 = 新增" in src


def test_stats_page_source_uses_dynamic_note_cadence_layout():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "function renderHeatStrip(items, valueKey, labelKey, color, formatter = (value) => `${value}`, options = {})" in src
    assert 'const style = columns > 0 ? ` style="grid-template-columns:repeat(${columns}, minmax(0, 1fr));"` : \'\';' in src
    assert "const cadenceBody = cadenceGranularity === 'day'" in src
    assert "Math.min(Math.max(cadenceItems.length, 1), 7)" in src


def test_stats_page_source_uses_neutral_zero_cells_for_activity_heatmap():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "if (!count) return 0;" in src
    assert "? `rgba(16,185,129,${op})`" in src
    assert ": 'rgba(226,232,240,0.52)';" in src
    assert '<div class="stats-heatmap-legend-cell" style="background:rgba(226,232,240,0.52)"></div>' in src


def test_stats_page_source_uses_even_axis_tick_sampling_without_forced_last_label():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "const step = (all.length - 1) / (maxTicks - 1);" in src
    assert "picked.add(Math.round(index * step));" in src
    assert "return Array.from(picked).sort((a, b) => a - b).map((index) => all[index]);" in src


def test_stats_page_source_uses_unified_time_presets():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "const RANGE_OPTIONS = [" in src
    assert "{ key: 'week', label: '本周' }" in src
    assert "{ key: 'month', label: '本月' }" in src
    assert "{ key: 'quarter', label: '本季' }" in src
    assert "{ key: 'year', label: '今年' }" in src
    assert "{ key: 'last_year', label: '去年' }" in src
    assert "{ key: 'custom', label: '自定义' }" in src
    assert "{ key: 'all', label: '全部' }" in src
    assert "if (_range === 'all') return 'all';" in src


def test_stats_page_source_marks_note_cards_as_range_driven():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "subtitle: noteCadenceSubtitle(cadenceGranularity)," in src
    assert "subtitle: `按${rangeLabel()}统计知识沉淀主题。`" in src
    assert "subtitle: `按${rangeLabel()}统计高频标签。`" in src


def test_ledger_comparison_fills_missing_months_and_keeps_prev_month_baseline():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_compare_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-compare"
    stats_module = _load_stats_module()

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 3, 29, 10, 0, 0)

    original_datetime = stats_module.datetime
    stats_module.datetime = _FrozenDateTime

    try:
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "十月支出",
            "amount": 120,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2025-10-12",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "十二月支出",
            "amount": 360,
            "direction": "expense",
            "ledger_category": "交通",
            "ledger_date": "2025-12-08",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "二月支出",
            "amount": 240,
            "direction": "expense",
            "ledger_category": "服务",
            "ledger_date": "2026-02-18",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "三月支出",
            "amount": 180,
            "direction": "expense",
            "ledger_category": "娱乐",
            "ledger_date": "2026-03-05",
        })

        result = stats_module.ledger_comparison(months=6, owner_id=owner_id, db=db)
        months = result["data"]["months"]

        assert [item["month"] for item in months] == [
            "2025-10",
            "2025-11",
            "2025-12",
            "2026-01",
            "2026-02",
            "2026-03",
        ]
        assert months[1]["expense"] == 0
        assert months[3]["expense"] == 0
        assert months[2]["prev_expense"] == 0
        assert months[4]["prev_expense"] == 0
        assert months[5]["prev_expense"] == 240
    finally:
        stats_module.datetime = original_datetime
        shutil.rmtree(temp_dir, ignore_errors=True)
