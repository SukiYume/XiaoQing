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
        assert result["summary"]["active_days"] == 3
        assert result["summary"]["fill_rate"] == 3 / 31
        assert result["summary"]["current_streak"] == 1
        assert result["summary"]["longest_streak"] == 2
        assert result["summary"]["month_longest_streak"] == 2
        assert result["summary"]["busiest_day"]["date"] == "2026-03-20"
        assert result["summary"]["busiest_day"]["words"] == len("今天写了很多很多字。")
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


def test_diary_page_source_uses_word_based_cadence_copy_and_values():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "diary.js").read_text(encoding="utf-8")

    assert "function formatWordMetric(value)" in src
    assert "class=\"diary-month-label\"" in src
    assert "id=\"diary-prev-month\"" in src
    assert "id=\"diary-next-month\"" in src
    assert "查看这个月每天写了多少字。" in src
    assert 'title="${item.date} · ${item.words} 字"' in src
    assert "const maxValue = Math.max(1, ...cadence.map((item) => item.words || 0));" in src


def test_stats_page_source_uses_word_based_diary_density_and_month_streak():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "stats.js").read_text(encoding="utf-8")

    assert "function formatWordCompact(value)" in src
    assert "subtitle: '这个月每天写了多少字。'" in src
    assert "map((item) => ({ label: item.label, words: item.words }))" in src
    assert "{ label: '本月最长连续', value: formatCount(summary.month_longest_streak || 0) }" in src
