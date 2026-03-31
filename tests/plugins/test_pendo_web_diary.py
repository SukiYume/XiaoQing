"""Regression tests for the redesigned Pendo web diary page analytics."""

from pathlib import Path
import shutil
import uuid

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.diary_overview import build_diary_overview


ROOT = Path(__file__).resolve().parents[2]


def test_build_diary_overview_tracks_fill_rate_streaks_and_moods():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_diary_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-diary-overview"

    try:
        db.insert_item({
            "id": "d1",
            "owner_id": owner_id,
            "type": "diary",
            "title": "春天",
            "content": "今天写了很多很多字。",
            "diary_date": "2026-03-20",
            "mood": "😊",
            "weather": "☀️ 晴",
            "created_at": "2026-03-20T22:00:00",
            "updated_at": "2026-03-20T22:00:00",
        })
        db.insert_item({
            "id": "d2",
            "owner_id": owner_id,
            "type": "diary",
            "title": "散步",
            "content": "今天继续写日记。",
            "diary_date": "2026-03-21",
            "mood": "😊",
            "created_at": "2026-03-21T22:00:00",
            "updated_at": "2026-03-21T22:00:00",
        })
        db.insert_item({
            "id": "d3",
            "owner_id": owner_id,
            "type": "diary",
            "title": "雨夜",
            "content": "这一天有些疲惫。",
            "diary_date": "2026-03-23",
            "mood": "😴",
            "template_id": "night_review",
            "created_at": "2026-03-23T22:00:00",
            "updated_at": "2026-03-23T22:00:00",
        })
        db.insert_item({
            "id": "d4",
            "owner_id": owner_id,
            "type": "diary",
            "title": "四月第一天",
            "content": "下一月的记录不应算进三月。",
            "diary_date": "2026-04-01",
            "mood": "🌤️",
            "created_at": "2026-04-01T22:00:00",
            "updated_at": "2026-04-01T22:00:00",
        })

        result = build_diary_overview(db=db, owner_id=owner_id, year=2026, month=3, today="2026-03-23")

        assert result["summary"]["entry_count"] == 3
        assert result["summary"]["range_start"] == "2026-03-01"
        assert result["summary"]["range_end"] == "2026-03-31"
        assert result["summary"]["active_days"] == 3
        assert result["summary"]["fill_rate"] == 3 / 31
        assert result["summary"]["current_streak"] == 1
        assert result["summary"]["longest_streak"] == 2
        assert result["summary"]["period_longest_streak"] == 2
        assert result["summary"]["month_longest_streak"] == 2
        assert result["summary"]["busiest_day"]["date"] == "2026-03-20"
        assert result["summary"]["busiest_day"]["words"] == len("今天写了很多很多字。")
        assert result["cadence_granularity"] == "day"
        assert result["mood_breakdown"][0]["mood"] == "😊"
        assert result["mood_breakdown"][0]["count"] == 2
        assert result["template_usage"][0]["template_id"] == "night_review"
        assert result["cadence"][19]["count"] == 1
        assert result["cadence"][19]["words"] == len("今天写了很多很多字。")
        assert result["cadence"][20]["count"] == 1
        assert result["cadence"][20]["words"] == len("今天继续写日记。")
        assert result["cadence"][22]["count"] == 1
        assert result["cadence"][22]["words"] == len("这一天有些疲惫。")
        assert result["recent_entries"][0]["id"] == "d3"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_diary_overview_supports_range_based_weekly_cadence():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_diary_range_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-diary-range"

    try:
        for item_id, diary_date, mood, template_id, content in [
            ("d1", "2026-01-05", "calm", "night_review", "第一周的记录。"),
            ("d2", "2026-01-12", "calm", "", "第二周的记录更长一点。"),
            ("d3", "2026-01-27", "happy", "free_write", "第三周没有连写。"),
            ("d4", "2026-02-08", "happy", "", "二月开始继续补记。"),
        ]:
            db.insert_item({
                "id": item_id,
                "owner_id": owner_id,
                "type": "diary",
                "title": item_id,
                "content": content,
                "diary_date": diary_date,
                "mood": mood,
                "template_id": template_id,
                "created_at": f"{diary_date}T21:00:00",
                "updated_at": f"{diary_date}T21:00:00",
            })

        result = build_diary_overview(
            db=db,
            owner_id=owner_id,
            start_date="2026-01-01",
            end_date="2026-02-15",
            today="2026-02-15",
            cadence_granularity="auto",
        )

        assert result["summary"]["entry_count"] == 4
        assert result["summary"]["range_days"] == 46
        assert result["summary"]["period_longest_streak"] == 1
        assert result["cadence_granularity"] == "week"
        assert result["cadence"][0]["label"] == "2026-W01"
        assert result["cadence"][-1]["label"] == "2026-W07"
        assert sum(item["count"] for item in result["cadence"]) == 4
        assert result["mood_breakdown"][0]["mood"] == "calm"
        assert result["template_usage"][0]["template_id"] in {"free_write", "night_review"}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_diary_overview_supports_cross_year_yearly_cadence():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_diary_year_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-diary-year"

    try:
        for item_id, diary_date, content in [
            ("d1", "2024-03-08", "2024 年记录"),
            ("d2", "2025-06-10", "2025 年记录更长一点"),
            ("d3", "2026-02-15", "2026 年记录"),
        ]:
            db.insert_item({
                "id": item_id,
                "owner_id": owner_id,
                "type": "diary",
                "title": item_id,
                "content": content,
                "diary_date": diary_date,
                "created_at": f"{diary_date}T21:00:00",
                "updated_at": f"{diary_date}T21:00:00",
            })

        result = build_diary_overview(
            db=db,
            owner_id=owner_id,
            start_date="2024-01-01",
            end_date="2026-12-31",
            today="2026-12-31",
            cadence_granularity="auto",
        )

        assert result["cadence_granularity"] == "year"
        assert [item["label"] for item in result["cadence"]] == ["2024", "2025", "2026"]
        assert [item["count"] for item in result["cadence"]] == [1, 1, 1]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_diary_page_source_uses_word_based_cadence_copy_and_values():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "diary.js").read_text(encoding="utf-8")

    assert "function formatWordMetric(value)" in src
    assert "class=\"diary-month-label\"" in src
    assert "id=\"diary-prev-month\"" in src
    assert "id=\"diary-next-month\"" in src
    assert "查看这个月每天写了多少字。" in src
    assert 'title="${item.date} · ${item.words} 字"' in src
    assert "const maxValue = Math.max(1, ...cadence.map((item) => item.words || 0));" in src


def test_diary_page_source_scales_summary_values_for_mid_width_layouts():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "diary.js").read_text(encoding="utf-8")

    assert ".diary-summary-card { padding: 18px; min-width: 0; }" in src
    assert "font-size: clamp(24px, 1.9vw, 30px);" in src
    assert "overflow-wrap: anywhere;" in src
    assert "word-break: break-word;" in src
    assert ".diary-summary-card { padding: 14px 16px; border-radius: 20px; }" in src
    assert ".diary-summary-value { margin-top: 6px; font-size: 22px; }" in src


def test_diary_page_source_uses_calendar_metrics_instead_of_preview_snippets():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "diary.js").read_text(encoding="utf-8")

    assert "function compactDiaryCellLabel(entry, maxChars = 8)" in src
    assert ".replace(/^[\\s\\u3000]+/, '')" in src
    assert ".replace(/\\s+/g, ' ')" in src
    assert "const totalWords = list.length ? list.reduce((sum, item) => sum + diaryWordCount(item), 0) : 0;" in src
    assert "const metric = list.length > 1 ? `${list.length} 篇 ${totalWords} 字` : `${totalWords}字`;" in src
    assert "const copy = meta ? `${metric} ${meta}` : metric;" in src
    assert '<span class=\"diary-day-copy\">${escapeHtml(copy)}</span>' in src
    assert "diary-day-count" not in src
    assert ".diary-day-body { display: flex; flex-direction: column; gap: 4px; min-height: 0; margin-top: 0; align-items: flex-start; text-align: left; }" in src
    assert ".diary-day-copy {" in src
    assert "BREAKPOINTS.XL" in src
    assert "compactBreakpoint: BREAKPOINTS.MOBILE" in src
    assert "BREAKPOINTS.COMPACT" not in src
    assert "BREAKPOINTS.NARROW" not in src
    assert ".diary-month-nav { align-self: stretch; grid-template-columns: 32px minmax(0, 1fr) 32px; width: 100%; }" in src
    assert "还没写" not in src


def test_stats_page_source_uses_word_based_diary_density_and_month_streak():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "function formatWordCompact(value)" in src
    assert "function diaryCadenceSubtitle(granularity)" in src
    assert "async function fetchDiaryRangeBounds(fallbackEnd = todayStr())" in src
    assert "const diaryRange = _range === 'all' ? await fetchDiaryRangeBounds(range.end) : range;" in src
    assert "api.get('/stats/diary/overview', { start_date: diaryRange.start, end_date: diaryRange.end, today: diaryRange.end, cadence_granularity: 'auto' })" in src
    assert "api.get('/config/diary/moods').catch(() => null)" in src
    assert "const densityBody = cadenceGranularity === 'day'" in src
    assert "if (granularity === 'year') return `${diaryRangeSentence()}每年写了多少字。`;" in src
    assert "formatMoodLabel(item.mood)" in src
    assert "{ label: '区间最长连续', value: formatCount(summary.period_longest_streak || summary.month_longest_streak || 0) }" in src
