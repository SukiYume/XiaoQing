"""日记查看、删除、情绪分析和 AI 建议。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    asyncio,
)


class TestDiaryViewRegression:
    def test_view_diary_by_id_returns_full_entry(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.models.item import DiaryItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_view.db"))

        try:
            diary = DiaryItem(
                owner_id="u1",
                title="2026-03-28 日记",
                content="今天周六，十点多醒来，去华贸天地吃饭。",
                diary_date="2026-03-28",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(diary, "82d34407")

            handler = DiaryHandler(db=db)
            result = asyncio.run(handler.view_diary("u1", "82d34407", SimpleNamespace()))

            assert result["status"] == "success"
            assert "2026-03-28 的日记条目" in result["message"]
            assert "`82d34407`" in result["message"]
            assert "今天周六，十点多醒来" in result["message"]
        finally:
            db.cleanup()

    def test_view_event_with_diary_id_returns_hint_instead_of_crashing(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import DiaryItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_event_wrong_type.db"))

        try:
            diary = DiaryItem(
                owner_id="u1",
                title="2026-03-28 日记",
                content="今天周六，十点多醒来，去华贸天地吃饭。",
                diary_date="2026-03-28",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(diary, "82d34407")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.handle("u1", "view 82d34407", SimpleNamespace()))

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert "/pendo diary view 82d34407" in result["message"]
            assert "2026-03-28" in result["message"]
        finally:
            db.cleanup()

    def test_view_diary_with_event_id_returns_hint(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_wrong_type.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="晨会",
                start_time="2026-03-29T09:00:00",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(event, "evt12345")

            handler = DiaryHandler(db=db)
            result = asyncio.run(handler.view_diary("u1", "evt12345", SimpleNamespace()))

            assert result["status"] == "success"
            assert "不是日记ID" in result["message"]
            assert "/pendo event view evt12345" in result["message"]
        finally:
            db.cleanup()

    def test_delete_diary_by_id_deletes_entry(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.models.item import DiaryItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_delete_by_id.db"))

        try:
            diary = DiaryItem(
                owner_id="u1",
                title="2026-03-28 日记",
                content="今天周六，十点多醒来。",
                diary_date="2026-03-28",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(diary, "82d34407")

            handler = DiaryHandler(db=db)
            result = asyncio.run(handler.delete_diary("u1", "82d34407", SimpleNamespace()))

            assert result["status"] == "success"
            assert "已删除 2026-03-28 的日记" in result["message"]
            assert db.get_item("82d34407", "u1") is None
        finally:
            db.cleanup()

    def test_delete_diary_with_event_id_returns_hint(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_delete_wrong_type.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="晨会",
                start_time="2026-03-29T09:00:00",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(event, "evt12345")

            handler = DiaryHandler(db=db)
            result = asyncio.run(handler.delete_diary("u1", "evt12345", SimpleNamespace()))

            assert result["status"] == "success"
            assert "不是日记ID" in result["message"]
            assert "/pendo event view evt12345" in result["message"]
        finally:
            db.cleanup()


class TestDiaryMoodAIRegression:
    def test_create_diary_uses_ai_mood_analysis(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_ai_mood.db"))

        class _FakeAiParser:
            async def analyze_diary_mood(self, text, user_id):
                assert "今天周六" in text
                assert user_id == "u1"
                return "calm", 6

        try:
            handler = DiaryHandler(db=db, ai_parser=_FakeAiParser())
            result = asyncio.run(
                handler.create_diary(
                    "u1",
                    "2026-03-28",
                    {"content": "今天周六，十点多醒来，去华贸天地吃饭。"},
                    SimpleNamespace(),
                )
            )

            saved = db.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert "情绪: calm" in result["message"]
            assert saved is not None
            assert saved.mood == "calm"
            assert saved.mood_score == 6
        finally:
            db.cleanup()

    def test_add_diary_creates_separate_entry_with_ai_mood(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.models.item import DiaryItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_ai_append.db"))

        class _FakeAiParser:
            def __init__(self):
                self.calls = []

            async def analyze_diary_mood(self, text, user_id):
                self.calls.append(text)
                return "happy", 8

        ai_parser = _FakeAiParser()

        try:
            diary = DiaryItem(
                owner_id="u1",
                title="2026-03-28 日记",
                content="早上出门。",
                diary_date="2026-03-28",
                mood="calm",
                mood_score=5,
                category="日记",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(diary, "dia12345")

            handler = DiaryHandler(db=db, ai_parser=ai_parser)
            result = asyncio.run(
                handler.add_diary("u1", "2026-03-28 晚上玩得很开心", SimpleNamespace(), None)
            )

            original = db.get_item("dia12345", "u1")
            created = db.get_item(result["item_id"], "u1")
            entries = db.query_items_by_date_range(
                "u1",
                "diary",
                "diary_date",
                "2026-03-28",
                "2026-03-28",
            )

            assert result["status"] == "success"
            assert len(ai_parser.calls) == 1
            assert "早上出门。" not in ai_parser.calls[0]
            assert "晚上玩得很开心" in ai_parser.calls[0]
            assert original is not None
            assert original.mood == "calm"
            assert created is not None
            assert created.mood == "happy"
            assert created.mood_score == 8
            assert len(entries) == 2
        finally:
            db.cleanup()

    def test_add_diary_backfill_date_keeps_entry_time_on_diary_date(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_backfill_entry_time.db"))

        try:
            handler = DiaryHandler(db=db, ai_parser=None)
            result = asyncio.run(
                handler.add_diary(
                    "u1",
                    "2026-01-31 mood:happy score:8 favorite:false 补写这一天。",
                    SimpleNamespace(),
                    None,
                )
            )

            saved = db.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert saved is not None
            assert saved.diary_date == "2026-01-31"
            assert (
                datetime.fromisoformat(saved.entry_time)
                .astimezone(ZoneInfo("Asia/Shanghai"))
                .date()
                .isoformat()
                == "2026-01-31"
            )
            assert saved.mood == "happy"
            assert saved.mood_score == 8
            assert saved.is_favorite is False
        finally:
            db.cleanup()

    def test_add_diary_rejects_score_without_mood_when_out_of_range(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_score_without_mood.db"))

        try:
            handler = DiaryHandler(db=db, ai_parser=None)
            result = asyncio.run(
                handler.add_diary(
                    "u1",
                    "2026-01-31 score:99 补写这一天。",
                    SimpleNamespace(),
                    None,
                )
            )

            assert result["status"] == "error"
            assert "mood_score" in result["message"]
            assert (
                db.query_items_by_date_range(
                    "u1", "diary", "diary_date", "2026-01-31", "2026-01-31"
                )
                == []
            )
        finally:
            db.cleanup()

    def test_create_diary_rejects_invalid_new_structure_fields(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_diary_invalid_fields.db"))

        try:
            handler = DiaryHandler(db=db, ai_parser=None)
            result = asyncio.run(
                handler.create_diary(
                    "u1",
                    "2026-03-28",
                    {
                        "content": "今天记录一下。",
                        "mood": "happy",
                        "mood_score": 99,
                        "template_answers": "legacy text",
                    },
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "error"
            assert "mood_score" in result["message"]

            template_result = asyncio.run(
                handler.create_diary(
                    "u1",
                    "2026-03-28",
                    {
                        "content": "今天记录一下。",
                        "mood": "happy",
                        "mood_score": 8,
                        "template_answers": "legacy text",
                    },
                    SimpleNamespace(),
                )
            )

            assert template_result["status"] == "error"
            assert "template_answers" in template_result["message"]
            assert (
                db.query_items_by_date_range(
                    "u1", "diary", "diary_date", "2026-03-28", "2026-03-28"
                )
                == []
            )
        finally:
            db.cleanup()
