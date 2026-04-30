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

    _orig_fastapi = sys.modules.get("fastapi")
    _orig_responses = sys.modules.get("fastapi.responses")

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules.pop("plugins.pendo.web.api.stats", None)
    mod = importlib.import_module("plugins.pendo.web.api.stats")

    if _orig_fastapi is not None:
        sys.modules["fastapi"] = _orig_fastapi
    else:
        sys.modules.pop("fastapi", None)
    if _orig_responses is not None:
        sys.modules["fastapi.responses"] = _orig_responses
    else:
        sys.modules.pop("fastapi.responses", None)

    return mod


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
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-10",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "打车",
            "amount": 48,
            "transaction_type": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-03-11",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "房租",
            "amount": 1200,
            "transaction_type": "expense",
            "ledger_category": "居住",
            "ledger_date": "2026-03-12",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "工资",
            "amount": 5000,
            "transaction_type": "income",
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
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-02",
            },
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": "兼职",
                "amount": 300,
                "transaction_type": "income",
                "ledger_category": "副业",
                "ledger_date": "2026-03-03",
            },
            {
                "type": "ledger",
                "owner_id": owner_id,
                "title": "范围外支出",
                "amount": 66,
                "transaction_type": "expense",
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
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": included_date,
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "未来支出",
            "amount": 88,
            "transaction_type": "expense",
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


def test_event_stats_counts_event_graph_leaves_and_collection_category_fallback():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_event_graph_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-stats-graph"
    stats_module = _load_stats_module()

    try:
        db.create_event_collection({
            "id": "stat-conf",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "统计会议",
            "category": "学术",
            "start_time": "2026-03-05T09:00:00",
            "end_time": "2026-03-06T10:00:00",
        })
        for item_id, title, start_time in [
            ("stat-conf_m01", "摘要截止", "2026-03-05T09:00:00"),
            ("stat-conf_m02", "会议开始", "2026-03-06T10:00:00"),
        ]:
            db.insert_item({
                "id": item_id,
                "type": "event",
                "owner_id": owner_id,
                "title": title,
                "category": "未分类",
                "start_time": start_time,
                "event_role": "multi_node_child",
                "event_collection_id": "stat-conf",
                "event_collection_kind": "multi_node",
            })

        result = stats_module.event_stats(range="2026-03-01..2026-03-31", owner_id=owner_id, db=db)
        data = result["data"]

        assert sum(item["count"] for item in data["weekly"]) == 2
        assert data["by_category"] == [{"category": "学术", "count": 2}]
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
            "status": "open",
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
        assert result["cadence_granularity"] == "week"
        assert {item["category"] for item in result["categories"]} == {"工作", "生活"}
        assert {item["tag"] for item in result["hot_tags"]} == {"alpha", "beta"}
        assert sum(item["count"] for item in result["cadence"]) == 2
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ledger_stats_accepts_explicit_date_bounds():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_ledger_bounds_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger-bounds"
    stats_module = _load_stats_module()

    try:
        for title, amount, ledger_date in [
            ("一月支出", 88, "2026-01-05"),
            ("三月支出", 66, "2026-03-18"),
        ]:
            db.insert_item({
                "type": "ledger",
                "owner_id": owner_id,
                "title": title,
                "amount": amount,
                "transaction_type": "expense",
                "ledger_category": "测试",
                "ledger_date": ledger_date,
            })

        result = stats_module.ledger_stats(
            range="all",
            start_date="2026-03-01",
            end_date="2026-03-31",
            owner_id=owner_id,
            db=db,
        )
        data = result["data"]

        assert data["expense_by_category"] == [{"category": "测试", "total": 66}]
        assert data["daily"] == [{"date": "2026-03-18", "income": 0, "expense": 66}]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_notes_overview_uses_yearly_cadence_for_cross_year_ranges():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_stats_notes_year_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-note-stats-year"

    try:
        for item_id, created_at in [
            ("n-2024", "2024-03-03T09:00:00"),
            ("n-2025", "2025-06-18T09:00:00"),
            ("n-2026", "2026-02-01T09:00:00"),
        ]:
            db.insert_item({
                "id": item_id,
                "owner_id": owner_id,
                "type": "note",
                "title": item_id,
                "content": f"{item_id} 内容",
                "category": "工作",
                "created_at": created_at,
                "updated_at": created_at,
            })

        result = build_notes_overview(
            db=db,
            owner_id=owner_id,
            start_date="2024-01-01",
            end_date="2026-12-31",
            today="2026-12-31",
        )

        assert result["cadence_granularity"] == "year"
        assert [item["label"] for item in result["cadence"]] == ["2024", "2025", "2026"]
        assert [item["count"] for item in result["cadence"]] == [1, 1, 1]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_stats_page_source_passes_note_range_params():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "async function fetchNoteRangeBounds(fallbackEnd = todayStr())" in src
    assert "const notesRange = _range === 'all' ? await fetchNoteRangeBounds(range.end) : range;" in src
    assert "function overviewReferenceDay(range, today = todayStr())" in src
    assert "api.get('/stats/notes/overview', { start_date: notesRange.start, end_date: notesRange.end, today: overviewReferenceDay(notesRange) })" in src


def test_stats_page_source_passes_ledger_range_params_for_all():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "async function fetchLedgerRangeBounds(fallbackEnd = todayStr())" in src
    assert "const ledgerRange = _range === 'all' ? await fetchLedgerRangeBounds(range.end) : range;" in src
    assert "api.get('/stats/ledger', _range === 'all' ? { start_date: ledgerRange.start, end_date: ledgerRange.end } : { range: rangeParam })" in src


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


def test_parse_range_supports_calendar_quarter_boundaries():
    stats_module = _load_stats_module()

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 5, 18, 9, 0, 0)

    original_datetime = stats_module.datetime
    stats_module.datetime = _FrozenDateTime
    try:
        start, end = stats_module._parse_range("quarter")
    finally:
        stats_module.datetime = original_datetime

    assert start == "2026-04-01"
    assert end == "2026-05-18"


def test_stats_page_source_uses_task_palette_aligned_with_tasks_page():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "const taskToneDone = '#166534';" in src
    assert "const taskToneOpen = '#F59E0B';" in src
    assert "{ label: '未完成', count: totals.open, color: taskToneOpen }" in src
    assert "{ label: '已完成', count: totals.done, color: taskToneDone }" in src
    assert "浅橙 = 新增" in src


def test_stats_page_source_uses_dynamic_note_cadence_layout():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "function renderHeatStrip(items, valueKey, labelKey, color, formatter = (value) => `${value}`, options = {})" in src
    assert 'const style = columns > 0 ? ` style="grid-template-columns:repeat(${columns}, minmax(0, 1fr));"` : \'\';' in src
    assert "const cadenceBody = cadenceGranularity === 'day'" in src
    assert "Math.min(Math.max(cadenceItems.length, 1), 7)" in src


def test_stats_page_source_scales_summary_values_for_mid_width_layouts():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert ".stats-summary-card {" in src
    assert "font-size: clamp(24px, 1.85vw, 30px);" in src
    assert "overflow-wrap: anywhere;" in src
    assert "word-break: break-word;" in src


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

    assert "RANGE_PRESET_OPTIONS" in src
    assert "if (_range === 'all') return 'all';" in src


def test_stats_page_source_marks_note_cards_as_range_driven():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "subtitle: noteCadenceSubtitle(cadenceGranularity)," in src
    assert "if (granularity === 'year') return `按${rangeLabel()}查看每年新增笔记数量。`;" in src
    assert "subtitle: `按${rangeLabel()}统计知识沉淀主题。`" in src
    assert "subtitle: `按${rangeLabel()}统计高频标签。`" in src


def test_notes_page_source_uses_range_driven_note_cadence():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "notes.js").read_text(encoding="utf-8")

    assert "RANGE_PRESET_OPTIONS" in src
    assert "range: 'year'" in src
    assert "async function fetchNoteRangeBounds(fallbackEnd = todayKey())" in src
    assert "async function resolveActiveRange()" in src
    assert "function overviewReferenceDay(range)" in src
    assert "start_date: range?.start || ''" in src
    assert "today: overviewReferenceDay(range)," in src
    assert "date_field: 'created_at'" in src
    assert "function noteCadenceSubtitle(granularity)" in src
    assert "按${rangeLabel()}查看每月新增笔记数量。" in src


def test_date_range_utility_uses_full_natural_period_bounds():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "date_ranges.js").read_text(encoding="utf-8")

    assert "function endOfWeek(base)" in src
    assert "function endOfMonth(base)" in src
    assert "function endOfQuarter(base)" in src
    assert "function endOfYear(base)" in src
    assert "const firstMonth = Math.floor(base.getMonth() / 3) * 3;" in src
    assert "start: isoDate(new Date(base.getFullYear(), firstMonth, 1))," in src
    assert "return { start: isoDate(monday), end: isoDate(endOfWeek(base)) };" in src
    assert "end: isoDate(endOfMonth(base))," in src
    assert "end: isoDate(endOfQuarter(base))," in src
    assert "end: isoDate(endOfYear(base))," in src


def test_notes_page_source_scales_summary_values_for_mid_width_layouts():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "notes.js").read_text(encoding="utf-8")

    assert "min-width: 0;" in src
    assert "font-size: clamp(24px, 1.9vw, 30px);" in src
    assert "overflow-wrap: anywhere;" in src
    assert "word-break: break-word;" in src
    assert ".notes-spotlight-side {" in src
    assert "display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in src
    assert ".note-row-side {" in src
    assert ".note-row-footer {" in src
    assert ".note-row-preview { font-size: 12px; line-height: 1.55; -webkit-line-clamp: 1; }" in src
    assert ".note-row-order, .note-card-category, .note-tag { height: 22px; padding: 0 7px; font-size: 10px; }" in src


def test_stats_page_source_compacts_mobile_donuts():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert ".stats-donut { width: 100%; height: auto; max-width: 180px; margin: 0 auto; }" in src
    assert ".stats-donut { max-width: min(200px, 52vw); }" in src
    assert ".stats-donut-center-value { font-size: 16px; }" in src
    assert "fill: #7f1d1d;" in src


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
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2025-10-12",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "十二月支出",
            "amount": 360,
            "transaction_type": "expense",
            "ledger_category": "交通",
            "ledger_date": "2025-12-08",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "二月支出",
            "amount": 240,
            "transaction_type": "expense",
            "ledger_category": "服务",
            "ledger_date": "2026-02-18",
        })
        db.insert_item({
            "type": "ledger",
            "owner_id": owner_id,
            "title": "三月支出",
            "amount": 180,
            "transaction_type": "expense",
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
