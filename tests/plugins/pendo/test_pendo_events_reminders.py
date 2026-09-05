"""日程、里程碑和提醒。"""

from __future__ import annotations

import pytest

from tests.helpers.pendo_test_support import (
    ROOT,
    Any,
    asyncio,
)


class TestEventLeafModel:
    """测试新版日程 leaf 数据模型"""

    def test_event_item_has_event_graph_fields_without_legacy_milestones(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.models.item import EventItem

        item = EventItem(owner_id="u1", title="会议")
        assert not hasattr(item, "milestones")
        assert not hasattr(item, "parent_id")
        assert not hasattr(item, "rrule")
        assert item.event_role == "single"
        assert item.event_collection_id is None

    def test_event_item_has_notes_field(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.models.item import EventItem

        item = EventItem(owner_id="u1", title="会议", notes="备注内容")
        assert item.notes == "备注内容"

    def test_event_item_to_dict_includes_event_graph_fields(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.models.item import EventItem

        item = EventItem(
            owner_id              = "u1",
            title                 = "摘要截止",
            event_role            = "multi_node_child",
            event_collection_id   = "conf2026",
            event_collection_kind = "multi_node",
            event_index           = 1,
            event_node_key        = "m01",
            notes                 = "备注",
        )
        d = item.to_dict()
        assert "milestones" not in d
        assert d["event_collection_id"] == "conf2026"
        assert d["event_collection_kind"] == "multi_node"
        assert d["notes"] == "备注"


class TestAIParserMilestones:
    """测试 AI parser 处理多时间节点事件"""

    def _make_parser(self):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.services.ai_parser import AIParser

        return AIParser(
            context=None,
            now_factory=lambda tz=None: datetime(2026, 1, 1, 9, 0, 0, tzinfo=tz),
        )

    @pytest.mark.parametrize("text", ["²", "٣", "１２"])
    def test_unicode_digits_do_not_escape_local_number_parser(self, text: str):
        assert self._make_parser()._parse_chinese_number(text) is None

    def test_build_remind_times_for_milestones(self):
        """多节点时 remind_times 是所有里程碑各自提醒的并集"""
        parser     = self._make_parser()
        milestones = [
            {"name": "注册截止", "time": "2030-04-06T00:00:00"},
            {"name": "会议开始", "time": "2030-04-22T10:30:00"},
        ]
        remind_offsets = ["提前1天", "提前1小时"]
        times          = parser.build_remind_times_for_milestones(milestones, remind_offsets)
        # 2 milestones × 2 offsets = 4 remind times
        assert len(times) == 4

    def test_parse_event_with_ai_handles_milestones(self):
        """模拟 LLM 返回 milestones 时正确解析"""
        import asyncio
        import json
        from unittest.mock import AsyncMock, patch

        parser = self._make_parser()

        mock_response = json.dumps(
            {
                "title": "星团会议",
                "start_time": None,
                "end_time": None,
                "location": "江苏溧水",
                "category": "学习",
                "remind_offsets": ["提前1天", "提前1小时"],
                "rrule": None,
                "milestones": [
                    {"name": "注册截止", "time": "2030-04-06T00:00:00Z"},
                    {"name": "会议开始", "time": "2030-04-22T10:30:00+00:00"},
                    {"name": "会议结束", "time": "2030-04-26T12:00:00+08:00"},
                ],
                "notes": "https://example.com",
            }
        )

        async def run():
            with patch.object(parser, "_call_llm", new=AsyncMock(return_value=mock_response)):
                return await parser.parse_event_with_ai("...", "user1")

        result = asyncio.run(run())
        assert result["milestones"][0]["name"] == "注册截止"
        assert result["start_time"] == "2030-04-06T00:00:00"
        assert result["milestones"][1]["time"] == "2030-04-22T10:30:00"
        assert result["end_time"] == "2030-04-26T12:00:00"
        assert result["notes"] == "https://example.com"
        assert len(result["remind_times"]) == 6  # 3 milestones × 2 offsets

    def test_parse_event_with_ai_treats_timezone_suffix_as_local_wall_time(self):
        """模型擅自添加的 UTC 后缀不能改变用户说出的本地钟点。"""
        import asyncio
        import json
        from unittest.mock import AsyncMock, patch

        parser        = self._make_parser()
        mock_response = json.dumps(
            {
                "parse_source": "rule",
                "title": "心理咨询",
                "start_time": "2030-08-19T18:00:00+00:00",
                "end_time": "2030-08-19T19:00:00Z",
                "location": None,
                "category": "健康",
                "remind_offsets": [],
                "rrule": None,
                "milestones": [],
                "notes": None,
            }
        )

        async def run():
            with patch.object(parser, "_call_llm", new=AsyncMock(return_value=mock_response)):
                return await parser.parse_event_with_ai("下周三晚上六点心理咨询", "user1")

        result = asyncio.run(run())
        assert result["parse_source"] == "ai"
        assert result["start_time"] == "2030-08-19T18:00:00"
        assert result["end_time"] == "2030-08-19T19:00:00"

    def test_parse_event_with_ai_recovers_single_milestone_as_start_time(self):
        """LLM 把单次日程误放进一个 milestone 时，应按单次日程整理。"""
        import asyncio
        import json
        from unittest.mock import AsyncMock, patch

        parser = self._make_parser()

        mock_response = json.dumps(
            {
                "title": "悉尼大学博后申请",
                "start_time": None,
                "end_time": None,
                "location": "悉尼大学",
                "category": "工作",
                "remind_offsets": ["提前1周", "提前1天"],
                "rrule": None,
                "milestones": [
                    {"name": "申请截止", "time": "2030-06-14T14:00:00"},
                ],
                "notes": "https://example.com/job",
            }
        )

        async def run():
            with patch.object(parser, "_call_llm", new=AsyncMock(return_value=mock_response)):
                return await parser.parse_event_with_ai("...", "user1")

        result = asyncio.run(run())
        assert result["title"] == "悉尼大学博后申请"
        assert result["start_time"] == "2030-06-14T14:00:00"
        assert "milestones" not in result
        assert result["notes"] == "https://example.com/job"
        assert len(result["remind_times"]) == 2


class TestMilestoneEventHandler:
    """测试多时间节点事件创建"""

    def _make_handler(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from unittest.mock import MagicMock

        from plugins.pendo.handlers.event import EventHandler

        db = MagicMock()
        db.insert_item = MagicMock(return_value="abc12345")
        db.log_operation = MagicMock()

        ai_parser        = MagicMock()
        reminder_service = MagicMock()
        reminder_service.detect_conflict = MagicMock(return_value=[])

        handler = EventHandler(db=db, ai_parser=ai_parser, reminder_service=reminder_service)
        return handler

    def test_create_milestone_event_success(self):
        import asyncio

        handler                     = self._make_handler()
        parsed_data: dict[str, Any] = {
            "title": "星团会议",
            "milestones": [
                {"name": "注册截止", "time": "2030-04-06T00:00:00"},
                {"name": "会议开始", "time": "2030-04-22T10:30:00"},
                {"name": "会议结束", "time": "2030-04-26T12:00:00"},
            ],
            "start_time": "2030-04-06T00:00:00",
            "end_time": "2030-04-26T12:00:00",
            "location": "江苏溧水",
            "notes": "https://example.com",
            "remind_times": ["2030-04-05T00:00:00", "2030-04-05T23:00:00"],
        }
        from plugins.pendo.models.item import ItemType

        parsed_data["type"] = ItemType.EVENT

        async def run():
            return await handler.create_event("user1", parsed_data, {})

        result = asyncio.run(run())
        assert result["status"] == "success"
        assert "多时间节点" in result["message"]
        assert "3个节点" in result["message"]
        assert "注册截止" in result["message"]
        assert "江苏溧水" in result["message"]
        assert "https://example.com" in result["message"]

    def test_create_single_event_with_notes(self):
        import asyncio

        handler                     = self._make_handler()
        parsed_data: dict[str, Any] = {
            "title": "普通会议",
            "milestones": [],
            "start_time": "2030-04-06T09:00:00",
            "notes": "会议室在3楼",
            "remind_times": ["2030-04-05T09:00:00"],
        }
        from plugins.pendo.models.item import ItemType

        parsed_data["type"] = ItemType.EVENT

        async def run():
            return await handler.create_event("user1", parsed_data, {})

        result = asyncio.run(run())
        assert result["status"] == "success"
        assert "会议室在3楼" in result["message"]


class TestMilestoneReminderMessage:
    """测试里程碑事件的提醒消息"""

    def _make_service(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from unittest.mock import MagicMock

        from plugins.pendo.services.reminder import ReminderService

        db = MagicMock()
        db.get_user_settings = MagicMock(return_value={})
        return ReminderService(db=db)

    def test_reminder_message_uses_collection_and_leaf_notes(self):
        """多节点 leaf 提醒显示集合标题、节点标题和节点备注"""
        from types import SimpleNamespace

        service                                      = self._make_service()
        service.db.get_event_collection.return_value = {
            "id": "abc12345",
            "kind": "multi_node",
            "title": "星团会议",
            "notes": "这是整场会议的全局说明",
        }

        item = SimpleNamespace(
            id                    = "abc12345_m01",
            title                 = "注册截止",
            start_time            = "2030-04-06T00:00:00",
            end_time              = None,
            location              = "江苏溧水",
            notes                 = "报名材料今晚前发给秘书",
            remind_times          = ["2030-04-05T00:00:00", "2030-04-05T23:00:00"],
            context               = {},
            owner_id              = "user1",
            event_collection_id   = "abc12345",
            event_collection_kind = "multi_node",
        )

        msg = service._build_reminder_message(item, "2030-04-05T00:00:00")
        assert "注册截止" in msg
        assert "星团会议" in msg
        assert "节点时间: 04月06日 00:00" in msg
        assert "对应提醒点: 提前1天（04月05日 00:00）" in msg
        assert "报名材料今晚前发给秘书" in msg
        assert "这是整场会议的全局说明" not in msg

    def test_reminder_message_shows_notes(self):
        """普通事件的提醒消息应附上 notes"""
        from types import SimpleNamespace

        service = self._make_service()

        item = SimpleNamespace(
            id           = "abc12345",
            title        = "普通会议",
            start_time   = "2030-04-06T09:00:00",
            end_time     = None,
            location     = "",
            notes        = "会议链接: https://meet.example.com",
            remind_times = ["2030-04-05T09:00:00"],
            context      = {},
            owner_id     = "user1",
        )

        msg = service._build_reminder_message(item, "2030-04-05T09:00:00")
        assert "会议链接" in msg
        assert "事件时间: 04月06日 09:00" in msg
        assert "对应提醒点: 提前1天（04月05日 09:00）" in msg


class TestRecurringEventRegression:
    def test_create_recurring_event_preserves_duration_per_instance(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler

        inserted_items = []

        db = MagicMock()
        db.create_event_collection.side_effect = lambda _collection, children, *, operation_action: (
            inserted_items.extend(item for _item_id, item in children) or _collection["id"]
        )

        handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

        parsed_data = {
            "title": "晨会",
            "content": "每日站会",
            "start_time": "2030-01-01T09:00:00",
            "end_time": "2030-01-01T10:30:00",
            "location": "A会议室",
            "tags": ["团队"],
            "category": "工作",
            "context": {},
            "notes": "带上周报",
            "rrule": "FREQ=DAILY;COUNT=2",
        }
        remind_times = ["2030-01-01T08:00:00"]

        result = asyncio.run(handler._create_recurring_event("u1", parsed_data, remind_times))

        assert result["status"] == "success"
        assert len(inserted_items) == 2
        assert inserted_items[0].end_time == "2030-01-01T10:30:00"
        assert inserted_items[1].end_time == "2030-01-02T10:30:00"

    def test_edit_recurring_collection_updates_header_without_shifting_occurrences(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db            = Database(str(tmp_path / "pendo.db"))
        collection_id = "series123"

        try:
            db.create_event_collection(
                {
                    "id": collection_id,
                    "owner_id": "u1",
                    "kind": "recurring",
                    "title": "重复会议",
                    "category": "工作",
                    "rrule": "FREQ=DAILY;COUNT=2",
                    "start_time": "2030-01-01T09:00:00",
                    "end_time": "2030-01-02T10:00:00",
                }
            )
            first = EventItem(
                owner_id              = "u1",
                title                 = "重复会议",
                start_time            = "2030-01-01T09:00:00",
                end_time              = "2030-01-01T10:00:00",
                remind_times          = ["2030-01-01T08:00:00"],
                event_role            = "recurring_occurrence",
                event_collection_id   = collection_id,
                event_collection_kind = "recurring",
                event_index           = 1,
                event_node_key        = "20300101",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id              = "u1",
                title                 = "重复会议",
                start_time            = "2030-01-02T09:00:00",
                end_time              = "2030-01-02T10:00:00",
                remind_times          = ["2030-01-02T08:00:00"],
                event_role            = "recurring_occurrence",
                event_collection_id   = collection_id,
                event_collection_kind = "recurring",
                event_index           = 2,
                event_node_key        = "20300102",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            db.insert_item(first, "series123_20300101")
            db.insert_item(second, "series123_20300102")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"title": "新版重复会议"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler.edit_event("u1", f"{collection_id} 改名为新版重复会议", MagicMock())
            )

            assert result["status"] == "success"

            updated_first  = db.get_item("series123_20300101", "u1")
            updated_second = db.get_item("series123_20300102", "u1")

            assert updated_first is not None
            assert updated_second is not None
            assert db.get_event_collection(collection_id, "u1")["title"] == "新版重复会议"
            assert updated_first.start_time == "2030-01-01T01:00:00+00:00"
            assert updated_second.start_time == "2030-01-02T01:00:00+00:00"
        finally:
            db.cleanup()


class TestReminderRegression:
    def test_parse_updates_uses_partial_ai_parse_for_edits(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem

        changes = "4月7日下午两点，提前一天和提前一小时提醒"

        class _FakeAiParser:
            async def parse_event_with_ai(self, text, user_id, **kwargs):
                assert kwargs["partial"] is True
                assert kwargs["fallback_text"] == changes
                assert "[编辑现有日程]" in text
                return {
                    "type": "event",
                    "parse_source": "ai",
                    "title": "[编辑现有日程] 原标题：FAST2026观测申请截止",
                    "content": text,
                    "category": "未分类",
                    "start_time": "2026-04-07T14:00:00",
                    "remind_times": ["2026-04-06T14:00:00", "2026-04-07T13:00:00"],
                }

            def parse_natural_language(self, text, user_id):
                raise AssertionError("unexpected fallback")

        handler = EventHandler(
            db=MagicMock(), ai_parser=_FakeAiParser(), reminder_service=MagicMock()
        )
        current_event = EventItem(
            owner_id   = "u1",
            title      = "FAST2026观测申请截止",
            category   = "工作",
            start_time = "2026-03-31T14:00:00",
        )

        updates = asyncio.run(handler._parse_updates(changes, current_event))

        assert updates == {
            "start_time": "2026-04-07T14:00:00",
            "remind_times": ["2026-04-06T14:00:00", "2026-04-07T13:00:00"],
        }

    def test_ai_edit_stores_model_utc_suffix_as_shanghai_wall_time(self, tmp_path):
        """回归：模型返回 18:00+00:00 时，日程仍应显示为北京时间 18:00。"""
        import json
        import sys
        from unittest.mock import AsyncMock, MagicMock, patch
        from zoneinfo import ZoneInfo

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.ai_parser import AIParser
        from plugins.pendo.services.db import Database
        from plugins.pendo.utils.formatters import ItemFormatter

        owner_id = "u-ai-offset-edit"
        db       = Database(str(tmp_path / "pendo.db"))

        try:
            assert db.update_user_settings(owner_id, {"timezone": "Asia/Shanghai"})
            event = EventItem(
                owner_id       = owner_id,
                title          = "心理咨询",
                category       = "健康",
                start_time     = "2026-08-12T18:00:00",
                timezone       = "Asia/Shanghai",
                reminder_rules = [
                    {"offset_seconds": 86400},
                    {"offset_seconds": 3600},
                    {"offset_seconds": 0},
                ],
                remind_times=[
                    "2026-08-11T18:00:00",
                    "2026-08-12T17:00:00",
                    "2026-08-12T18:00:00",
                ],
                created_at = "2026-08-06T14:03:09",
                updated_at = "2026-08-06T14:03:09",
            )
            db.insert_item(event, "06f123e5")

            parser = AIParser(context=None, db=db)
            handler = EventHandler(db=db, ai_parser=parser, reminder_service=MagicMock())
            mock_response = json.dumps(
                {
                    "start_time": "2026-08-19T18:00:00+00:00",
                    "end_time": None,
                    "remind_offsets": [],
                    "milestones": [],
                }
            )

            async def run_edit():
                with patch.object(parser, "_call_llm", new=AsyncMock(return_value=mock_response)):
                    return await handler._edit_single_instance(
                        owner_id,
                        "06f123e5",
                        "改到下周三晚上六点",
                    )

            result = asyncio.run(run_edit())
            assert result["status"] == "success"

            updated = db.get_item("06f123e5", owner_id)
            assert updated is not None
            assert updated.start_time == "2026-08-19T10:00:00+00:00"
            assert updated.remind_times == [
                "2026-08-18T10:00:00+00:00",
                "2026-08-19T09:00:00+00:00",
                "2026-08-19T10:00:00+00:00",
            ]
            assert (
                ItemFormatter.format_datetime(
                    updated.start_time,
                    tz=ZoneInfo("Asia/Shanghai"),
                )
                == "2026-08-19 18:00"
            )
        finally:
            db.cleanup()

    def test_parse_updates_does_not_take_location_from_note_text(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem

        class _FakeAiParser:
            async def parse_event_with_ai(self, text, user_id, **kwargs):
                return {
                    "type": "event",
                    "location": "北京南",
                    "notes": "从北京南坐G123去会场",
                }

            def parse_natural_language(self, text, user_id):
                raise AssertionError("unexpected fallback")

        handler = EventHandler(
            db=MagicMock(), ai_parser=_FakeAiParser(), reminder_service=MagicMock()
        )
        current_event = EventItem(
            owner_id   = "u1",
            title      = "会议开始",
            location   = "杭州",
            notes      = "",
            start_time = "2030-01-22T10:30:00",
        )

        updates = asyncio.run(handler._parse_updates("备注从北京南坐G123去会场", current_event))

        assert updates == {"notes": "从北京南坐G123去会场"}

    def test_edit_single_instance_keeps_category_and_explicit_reminders(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))

        try:
            event = EventItem(
                owner_id     = "u1",
                title        = "FAST2026观测申请截止",
                category     = "工作",
                start_time   = "2026-03-31T14:00:00",
                remind_times = [
                    "2026-03-30T14:00:00",
                    "2026-03-31T13:00:00",
                    "2026-03-31T14:00:00",
                ],
                created_at = "2026-03-20T00:00:00",
                updated_at = "2026-03-20T00:00:00",
            )
            db.insert_item(event, "evt12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {
                    "start_time": "2026-04-07T14:00:00",
                    "remind_times": ["2026-04-06T14:00:00", "2026-04-07T13:00:00"],
                }

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler._edit_single_instance(
                    "u1",
                    "evt12345",
                    "4月7日下午两点，提前一天和提前一小时提醒",
                )
            )

            assert result["status"] == "success"

            updated = db.get_item("evt12345", "u1")
            assert updated is not None
            assert updated.title == "FAST2026观测申请截止"
            assert updated.category == "工作"
            assert updated.start_time == "2026-04-07T06:00:00+00:00"
            assert updated.remind_times == [
                "2026-04-06T06:00:00+00:00",
                "2026-04-07T05:00:00+00:00",
                "2026-04-07T06:00:00+00:00",
            ]
        finally:
            db.cleanup()

    def test_collection_reminders_apply_explicit_offsets_to_children(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))

        try:
            collection_id = "series123"
            db.create_event_collection(
                {
                    "id": collection_id,
                    "owner_id": "u1",
                    "kind": "recurring",
                    "title": "重复会议",
                    "category": "工作",
                    "rrule": "FREQ=DAILY;COUNT=2",
                    "start_time": "2030-01-01T10:00:00",
                    "end_time": "2030-01-02T11:00:00",
                }
            )
            first = EventItem(
                owner_id              = "u1",
                title                 = "重复会议",
                category              = "工作",
                start_time            = "2030-01-01T10:00:00",
                end_time              = "2030-01-01T11:00:00",
                remind_times          = ["2030-01-01T09:00:00"],
                event_role            = "recurring_occurrence",
                event_collection_id   = collection_id,
                event_collection_kind = "recurring",
                event_index           = 1,
                event_node_key        = "20300101",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id              = "u1",
                title                 = "重复会议",
                category              = "工作",
                start_time            = "2030-01-02T10:00:00",
                end_time              = "2030-01-02T11:00:00",
                remind_times          = ["2030-01-02T09:00:00"],
                event_role            = "recurring_occurrence",
                event_collection_id   = collection_id,
                event_collection_kind = "recurring",
                event_index           = 2,
                event_node_key        = "20300102",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            db.insert_item(first, "series123_20300101")
            db.insert_item(second, "series123_20300102")

            ai_parser                                                    = MagicMock()
            ai_parser.build_reminder_rules_from_description.return_value = [
                {"offset_seconds": 86400},
                {"offset_seconds": 3600},
                {"offset_seconds": 0},
            ]
            handler = EventHandler(db=db, ai_parser=ai_parser, reminder_service=MagicMock())

            result = asyncio.run(
                handler.set_reminders("u1", f"{collection_id} 提前1天和1小时提醒", MagicMock())
            )

            assert result["status"] == "success"

            updated_first  = db.get_item("series123_20300101", "u1")
            updated_second = db.get_item("series123_20300102", "u1")

            assert updated_first is not None
            assert updated_second is not None
            assert updated_first.remind_times == [
                "2029-12-31T02:00:00+00:00",
                "2030-01-01T01:00:00+00:00",
                "2030-01-01T02:00:00+00:00",
            ]
            assert updated_second.remind_times == [
                "2030-01-01T02:00:00+00:00",
                "2030-01-02T01:00:00+00:00",
                "2030-01-02T02:00:00+00:00",
            ]
        finally:
            db.cleanup()

    def test_list_events_exact_date_query_returns_matching_day(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_exact_date.db"))

        try:
            event = EventItem(
                owner_id     = "u1",
                title        = "元旦会议",
                start_time   = "2030-01-01T10:00:00",
                end_time     = "2030-01-01T11:00:00",
                remind_times = ["2030-01-01T09:00:00"],
                created_at   = "2029-12-01T00:00:00",
                updated_at   = "2029-12-01T00:00:00",
            )
            db.insert_item(event, "evtday01")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.list_events("u1", "2030-01-01", MagicMock()))

            assert result["status"] == "success"
            assert "元旦会议" in result["message"]
            assert "01月01日" in result["message"]
        finally:
            db.cleanup()

    def test_leaf_edit_preserves_sent_history_and_prunes_stale_unsent_logs(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_batch_logs.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="会议",
                start_time="2030-01-01T10:00:00",
                end_time="2030-01-01T11:00:00",
                remind_times=["2030-01-01T08:30:00", "2030-01-01T09:00:00", "2030-01-01T10:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.insert_item(event, "evtlogs1")
            db.get_connection().execute(
                """
                UPDATE reminder_logs SET repeat_count = 1
                WHERE item_id = ? AND remind_time = ?
                """,
                ("evtlogs1", "2030-01-01T00:30:00+00:00"),
            )
            db.get_connection().commit()
            db.log_reminder("evtlogs1", "2030-01-01T01:00:00+00:00", sent=True)
            db.log_reminder("evtlogs1", "2030-01-01T02:00:00+00:00", sent=True)

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"start_time": "2030-01-10T10:00:00"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler.edit_event("u1", "evtlogs1 改到2030-01-10 10:00", MagicMock())
            )

            assert result["status"] == "success"
            logs = db.get_reminder_logs("evtlogs1")
            assert sorted(log["remind_time"] for log in logs if log["sent_at"]) == [
                "2030-01-01T01:00:00+00:00",
                "2030-01-01T02:00:00+00:00",
            ]
            assert sorted(log["remind_time"] for log in logs if not log["sent_at"]) == [
                "2030-01-10T00:30:00+00:00",
                "2030-01-10T01:00:00+00:00",
                "2030-01-10T02:00:00+00:00",
            ]
            assert db.get_unconfirmed_sent_reminders() == []
        finally:
            db.cleanup()

    def test_edit_multi_node_leaf_shifts_only_that_leaf(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_milestone_edit.db"))

        try:
            db.create_event_collection(
                {
                    "id": "mile1234",
                    "owner_id": "u1",
                    "kind": "multi_node",
                    "title": "报名流程",
                    "category": "未分类",
                    "start_time": "2030-01-01T10:00:00",
                    "end_time": "2030-01-03T10:00:00",
                }
            )
            first = EventItem(
                owner_id              = "u1",
                title                 = "开始",
                start_time            = "2030-01-01T10:00:00",
                event_role            = "multi_node_child",
                event_collection_id   = "mile1234",
                event_collection_kind = "multi_node",
                event_index           = 1,
                event_node_key        = "m01",
                remind_times          = ["2029-12-31T10:00:00", "2030-01-01T10:00:00"],
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id              = "u1",
                title                 = "截止",
                start_time            = "2030-01-03T10:00:00",
                event_role            = "multi_node_child",
                event_collection_id   = "mile1234",
                event_collection_kind = "multi_node",
                event_index           = 2,
                event_node_key        = "m02",
                remind_times          = ["2030-01-03T10:00:00"],
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            db.insert_item(first, "mile1234_m01")
            db.insert_item(second, "mile1234_m02")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"start_time": "2030-01-05T10:00:00"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler.edit_event("u1", "mile1234_m01 改到1月5日10点", MagicMock())
            )

            assert result["status"] == "success"
            assert "已更新日程" in result["message"]

            updated_first  = db.get_item("mile1234_m01", "u1")
            updated_second = db.get_item("mile1234_m02", "u1")
            assert updated_first is not None
            assert updated_second is not None
            assert updated_first.start_time == "2030-01-05T02:00:00+00:00"
            assert updated_second.start_time == "2030-01-03T02:00:00+00:00"
        finally:
            db.cleanup()

    def test_edit_multi_node_leaf_notes_does_not_mutate_collection_notes(self, tmp_path):
        import sys
        from unittest.mock import AsyncMock, MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_milestone_targeted_edit.db"))

        try:
            db.create_event_collection(
                {
                    "id": "mile5678",
                    "owner_id": "u1",
                    "kind": "multi_node",
                    "title": "学术会议",
                    "category": "工作",
                    "notes": "旧备注",
                    "start_time": "2030-01-06T00:00:00",
                    "end_time": "2030-01-26T12:00:00",
                }
            )
            event = EventItem(
                owner_id     = "u1",
                title        = "会议开始",
                start_time   = "2030-01-22T10:30:00",
                remind_times = [
                    "2030-01-21T10:30:00",
                    "2030-01-22T09:30:00",
                    "2030-01-22T10:30:00",
                ],
                notes                 = "",
                event_role            = "multi_node_child",
                event_collection_id   = "mile5678",
                event_collection_kind = "multi_node",
                event_index           = 3,
                event_node_key        = "m03",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            db.insert_item(event, "mile5678_m03")

            ai_parser = MagicMock()
            ai_parser.parse_event_with_ai = AsyncMock(side_effect=RuntimeError("boom"))
            ai_parser.parse_natural_language.return_value = {
                "type": "event",
                "title": "会议开始改成1月22日中午12:43，备注从北京南坐G123去会场",
                "content": "会议开始改成1月22日中午12:43，备注从北京南坐G123去会场",
                "category": "工作",
                "owner_id": "u1",
                "needs_confirmation": [],
            }
            handler = EventHandler(db=db, ai_parser=ai_parser, reminder_service=MagicMock())

            result = asyncio.run(
                handler.edit_event(
                    "u1",
                    "mile5678_m03 备注从北京南坐G123去会场",
                    MagicMock(),
                )
            )

            assert result["status"] == "success"

            updated = db.get_item("mile5678_m03", "u1")
            assert updated is not None
            assert updated.notes == "从北京南坐G123去会场"
            assert db.get_event_collection("mile5678", "u1")["notes"] == "旧备注"
        finally:
            db.cleanup()

    def test_collection_reminder_view_returns_aggregate_series_reminders(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_parent_reminders.db"))

        try:
            db.create_event_collection(
                {
                    "id": "abcd1234",
                    "owner_id": "u1",
                    "kind": "recurring",
                    "title": "重复会议",
                    "category": "未分类",
                    "rrule": "FREQ=DAILY;COUNT=2",
                    "start_time": "2030-01-01T10:00:00",
                    "end_time": "2030-01-02T10:00:00",
                }
            )
            first = EventItem(
                owner_id              = "u1",
                title                 = "重复会议",
                start_time            = "2030-01-01T10:00:00",
                remind_times          = ["2030-01-01T09:00:00"],
                event_role            = "recurring_occurrence",
                event_collection_id   = "abcd1234",
                event_collection_kind = "recurring",
                event_index           = 1,
                event_node_key        = "20300101",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id              = "u1",
                title                 = "重复会议",
                start_time            = "2030-01-02T10:00:00",
                remind_times          = ["2030-01-02T09:00:00"],
                event_role            = "recurring_occurrence",
                event_collection_id   = "abcd1234",
                event_collection_kind = "recurring",
                event_index           = 2,
                event_node_key        = "20300102",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            db.insert_item(first, "abcd1234_20300101")
            db.insert_item(second, "abcd1234_20300102")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.list_reminders("u1", "abcd1234", MagicMock()))

            assert result["status"] == "success"
            assert "共 2 个节点" in result["message"]
            assert "01月01日 10:00" in result["message"]
            assert "01月02日 10:00" in result["message"]
        finally:
            db.cleanup()

    def test_list_reminders_week_supports_multi_node_leaf_events(self, tmp_path, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers import event as event_module
        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_milestone_reminders_week.db"))

        try:
            db.create_event_collection(
                {
                    "id": "mileweek",
                    "owner_id": "u1",
                    "kind": "multi_node",
                    "title": "学术会议",
                    "category": "未分类",
                    "start_time": "2030-01-22T10:30:00",
                    "end_time": "2030-01-26T12:00:00",
                }
            )
            start_event = EventItem(
                owner_id     = "u1",
                title        = "会议开始",
                start_time   = "2030-01-22T10:30:00",
                remind_times = [
                    "2030-01-21T10:30:00",
                    "2030-01-22T09:30:00",
                    "2030-01-22T10:30:00",
                ],
                event_role            = "multi_node_child",
                event_collection_id   = "mileweek",
                event_collection_kind = "multi_node",
                event_index           = 1,
                event_node_key        = "m01",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            end_event = EventItem(
                owner_id     = "u1",
                title        = "会议结束",
                start_time   = "2030-01-26T12:00:00",
                remind_times = [
                    "2030-01-25T12:00:00",
                    "2030-01-26T12:00:00",
                ],
                event_role            = "multi_node_child",
                event_collection_id   = "mileweek",
                event_collection_kind = "multi_node",
                event_index           = 2,
                event_node_key        = "m02",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
            )
            db.insert_item(start_event, "mileweek_m01")
            db.insert_item(end_event, "mileweek_m02")
            db.log_reminder(
                "mileweek_m01",
                db.get_item("mileweek_m01", "u1").remind_times[0],
                sent=True,
            )

            monkeypatch.setattr(
                event_module,
                "parse_event_time_range",
                lambda _query, **_kwargs: (
                    "2030-01-20T00:00:00",
                    "2030-01-26T23:59:59",
                ),
            )

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.handle_reminders("u1", "list week", MagicMock()))

            assert result["status"] == "success"
            assert "学术会议" in result["message"]
            assert "学术会议 · 会议开始" in result["message"]
            assert "学术会议 · 会议结束" in result["message"]
            assert "⏰ 01-21 10:30" in result["message"]
            assert "⏰ 01-26 12:00" in result["message"]
        finally:
            db.cleanup()
