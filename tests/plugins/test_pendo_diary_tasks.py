"""日记、任务和跨类型命令。"""

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


class TestCrossTypeCommandRegression:
    def test_note_view_with_event_id_returns_hint(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_wrong_type.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="晨会",
                start_time="2026-03-29T09:00:00",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(event, "evt12345")

            handler = NoteHandler(db=db)
            result = asyncio.run(handler.view_note("u1", "evt12345", SimpleNamespace()))

            assert result["status"] == "success"
            assert "不是笔记ID" in result["message"]
            assert "/pendo event view evt12345" in result["message"]
        finally:
            db.cleanup()

    def test_note_delete_with_event_id_returns_hint_without_deleting_event(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.models.item import EventItem, ItemType
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_delete_guard.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="晨会",
                start_time="2026-03-29T09:00:00",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(event, "evt12345")

            handler = NoteHandler(db=db)
            result = asyncio.run(handler.delete_note("u1", "evt12345", SimpleNamespace()))
            preserved = db.get_item("evt12345", "u1")

            assert result["status"] == "success"
            assert "不是笔记ID" in result["message"]
            assert preserved is not None
            assert preserved.deleted is False
            assert preserved.type == ItemType.EVENT
        finally:
            db.cleanup()

    def test_note_add_multiline_title_parses_category_and_tags(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_multiline_title.db"))

        try:
            handler = NoteHandler(db=db)
            result = asyncio.run(
                handler.create_note(
                    "u1",
                    "title:xxx\n1. xxx\n2. xxx\n3. xxx\ncat:其他 #xx",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "success"
            item = db.get_item(result["item_id"], "u1")
            assert item is not None
            assert item.title == "xxx"
            assert item.content == "1. xxx\n2. xxx\n3. xxx"
            assert item.category == "其他"
            assert item.tags == ["xx"]
        finally:
            db.cleanup()

    def test_note_add_explicit_content_syntax_still_works(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_explicit_content.db"))

        try:
            handler = NoteHandler(db=db)
            result = asyncio.run(
                handler.create_note(
                    "u1",
                    "title:我的标题 content 这里是详细正文 cat:工作 #学习",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "success"
            item = db.get_item(result["item_id"], "u1")
            assert item is not None
            assert item.title == "我的标题"
            assert item.content == "这里是详细正文"
            assert item.category == "工作"
            assert item.tags == ["学习"]
        finally:
            db.cleanup()

    def test_note_add_title_token_keeps_body_separate(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_title_token_prefix.db"))

        try:
            handler = NoteHandler(db=db)
            result = asyncio.run(
                handler.create_note(
                    "u1",
                    "title:测试 xxx cat:其他 #11 #22 #33",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "success"
            item = db.get_item(result["item_id"], "u1")
            assert item is not None
            assert item.title == "测试"
            assert item.content == "xxx"
            assert item.category == "其他"
            assert item.tags == ["11", "22", "33"]
        finally:
            db.cleanup()

    def test_note_add_title_token_can_be_extracted_from_middle(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_title_token_middle.db"))

        try:
            handler = NoteHandler(db=db)
            result = asyncio.run(
                handler.create_note(
                    "u1",
                    "xxx title:测试 cat:其他 #11 #22 #33",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "success"
            item = db.get_item(result["item_id"], "u1")
            assert item is not None
            assert item.title == "测试"
            assert item.content == "xxx"
            assert item.category == "其他"
            assert item.tags == ["11", "22", "33"]
        finally:
            db.cleanup()

    def test_note_add_preserves_markdown_heading_in_body(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_markdown_heading.db"))

        try:
            handler = NoteHandler(db=db)
            result = asyncio.run(
                handler.create_note(
                    "u1",
                    "title:我的 笔记标题\n# 一级标题\n正文内容\ncat:工作 #归档",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "success"
            item = db.get_item(result["item_id"], "u1")
            assert item is not None
            assert item.title == "我的 笔记标题"
            assert item.content == "# 一级标题\n正文内容"
            assert item.category == "工作"
            assert item.tags == ["归档"]
        finally:
            db.cleanup()

    def test_note_add_parses_references_and_view_shows_linked_item(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_reference.db"))

        try:
            db.insert_item(
                EventItem(
                    owner_id="u1",
                    title="晨会",
                    start_time="2026-04-20T09:00:00",
                    created_at="2026-04-19T21:00:00",
                    updated_at="2026-04-19T21:00:00",
                ),
                "evt12345",
            )

            handler = NoteHandler(db=db)
            result = asyncio.run(
                handler.create_note(
                    "u1",
                    "title:会议纪要\n今天确认两个事项。\nref:evt12345 cat:工作 #会议",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "success"
            item = db.get_item(result["item_id"], "u1")
            assert item.references == [
                {"kind": "item", "id": "evt12345", "type": "event", "title": "晨会"}
            ]
            assert item.related_items == ["evt12345"]
            assert item.content == "今天确认两个事项。"

            view = asyncio.run(handler.view_note("u1", result["item_id"], SimpleNamespace()))
            assert "关联条目" in view["message"]
            assert "日程: 晨会 `evt12345`" in view["message"]
        finally:
            db.cleanup()

    def test_note_append_tag_untag_link_and_backlink_flow(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.models.item import NoteItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_command_flow.db"))

        try:
            db.insert_item(
                NoteItem(
                    owner_id="u1",
                    title="主笔记",
                    content="第一段",
                    tags=["原始"],
                    category="工作",
                    created_at="2026-04-20T09:00:00",
                    updated_at="2026-04-20T09:00:00",
                ),
                "note_main",
            )
            db.insert_item(
                NoteItem(
                    owner_id="u1",
                    title="引用笔记",
                    content="引用正文",
                    category="工作",
                    created_at="2026-04-20T10:00:00",
                    updated_at="2026-04-20T10:00:00",
                ),
                "note_ref",
            )

            handler = NoteHandler(db=db)
            append = asyncio.run(handler.append_note("u1", "note_main 第二段", SimpleNamespace()))
            tag = asyncio.run(handler.tag_note("u1", "note_main #新增 #原始", SimpleNamespace()))
            untag = asyncio.run(handler.untag_note("u1", "note_main #原始", SimpleNamespace()))
            link = asyncio.run(handler.link_note("u1", "note_ref note_main", SimpleNamespace()))

            updated = db.get_item("note_main", "u1")
            linked = db.get_item("note_ref", "u1")
            view = asyncio.run(handler.view_note("u1", "note_main", SimpleNamespace()))

            assert append["status"] == "success"
            assert tag["status"] == "success"
            assert untag["status"] == "success"
            assert link["status"] == "success"
            assert updated.content == "第一段\n\n第二段"
            assert updated.tags == ["新增"]
            assert linked.related_items == ["note_main"]
            assert "被这些笔记引用" in view["message"]
            assert "引用笔记 `note_ref`" in view["message"]
        finally:
            db.cleanup()

    def test_ledger_view_with_diary_id_returns_hint(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.ledger import LedgerHandler
        from plugins.pendo.models.item import DiaryItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_ledger_wrong_type.db"))

        try:
            diary = DiaryItem(
                owner_id="u1",
                title="2026-03-28 日记",
                content="今天周六，十点多醒来。",
                diary_date="2026-03-28",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(diary, "dia12345")

            handler = LedgerHandler(db=db)
            result = asyncio.run(handler.handle("u1", "view dia12345", SimpleNamespace()))

            assert result["status"] == "success"
            assert "不是账目ID" in result["message"]
            assert "/pendo diary view dia12345" in result["message"]
        finally:
            db.cleanup()

    def test_ledger_edit_with_note_id_returns_hint_without_mutation(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.ledger import LedgerHandler
        from plugins.pendo.models.item import ItemType, NoteItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_ledger_edit_guard.db"))

        try:
            note = NoteItem(
                owner_id="u1",
                title="采购清单",
                content="牛奶 面包",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(note, "not12345")

            handler = LedgerHandler(db=db)
            result = asyncio.run(
                handler.edit_ledger("u1", "not12345 amount:50 cat:交通", SimpleNamespace())
            )
            preserved = db.get_item("not12345", "u1")

            assert result["status"] == "success"
            assert "不是账目ID" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.NOTE
            assert preserved.title == "采购清单"
        finally:
            db.cleanup()

    def test_ledger_add_session_starts_with_amount_then_numeric_options(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.ledger import LedgerHandler

        create_calls = []

        class _Context:
            async def create_session(self, initial_data=None, timeout=300.0):
                create_calls.append((initial_data, timeout))

        handler = LedgerHandler(db=MagicMock())
        result = asyncio.run(handler.start_add_session("u1", _Context()))

        assert result["status"] == "success"
        assert "请先输入金额" in result["message"]
        assert "类型、账户和分类可直接选数字" in result["message"]
        assert create_calls[0][0]["step"] == "amount"
        assert "owner_id" not in create_calls[0][0]

    def test_ledger_add_session_collects_typed_front_fields_then_numeric_options(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.ledger import LedgerHandler

        class _Session(dict):
            def set(self, key, value):
                self[key] = value

        class _Context:
            def __init__(self):
                self.end_calls = 0

            async def end_session(self):
                self.end_calls += 1
                return True

        handler = LedgerHandler(db=MagicMock())
        context = _Context()
        session = _Session({"step": "amount", "data": {}, "group_id": 123})
        captured = {}

        async def _fake_save(user_id, data, group_id=None):
            captured["user_id"] = user_id
            captured["data"] = dict(data)
            captured["group_id"] = group_id
            return {"status": "success", "message": "saved"}

        handler._save_ledger_item = _fake_save

        result = asyncio.run(handler.handle_session_step("u1", "88.5", session, context))
        assert result["status"] == "success"
        assert "请输入描述" in result["message"]
        assert session["step"] == "description"
        assert session["data"]["amount"] == 88.5

        result = asyncio.run(handler.handle_session_step("u1", "午饭", session, context))
        assert result["status"] == "success"
        assert "请选择收支类型" in result["message"]
        assert session["step"] == "transaction_type"
        assert session["data"]["title"] == "午饭"

        result = asyncio.run(handler.handle_session_step("u1", "1", session, context))
        assert result["status"] == "success"
        assert "请选择账户" in result["message"]
        assert session["step"] == "account"
        assert session["data"]["transaction_type"] == "expense"

        result = asyncio.run(handler.handle_session_step("u1", "2", session, context))
        assert result["status"] == "success"
        assert "请选择分类" in result["message"]
        assert session["step"] == "category"
        assert session["data"]["account_name"] == "微信"

        result = asyncio.run(handler.handle_session_step("u1", "1", session, context))
        assert result["status"] == "success"
        assert "请输入商户" in result["message"]
        assert session["step"] == "merchant"

        result = asyncio.run(handler.handle_session_step("u1", "0", session, context))
        assert result == {"status": "success", "message": "saved"}
        assert context.end_calls == 1
        assert captured["user_id"] == "u1"
        assert captured["group_id"] == 123
        assert captured["data"]["amount"] == 88.5
        assert captured["data"]["title"] == "午饭"
        assert captured["data"]["transaction_type"] == "expense"
        assert captured["data"]["account_name"] == "微信"
        assert captured["data"]["ledger_category"] == "餐饮"
        assert "merchant" not in captured["data"]

        context = _Context()
        session = _Session(
            {
                "step": "transaction_type",
                "data": {"amount": 1000, "title": "还款", "owner_id": "u-spoofed"},
                "group_id": None,
            }
        )
        captured.clear()
        result = asyncio.run(handler.handle_session_step("u1", "3", session, context))
        assert result["status"] == "success"
        assert session["step"] == "account"
        assert session["data"]["transaction_type"] == "transfer"

        result = asyncio.run(handler.handle_session_step("u1", "2", session, context))
        assert result["status"] == "success"
        assert session["step"] == "counter_account"
        assert session["data"]["account_name"] == "微信"

        result = asyncio.run(handler.handle_session_step("u1", "4", session, context))
        assert result["status"] == "success"
        assert session["step"] == "merchant"
        assert session["data"]["ledger_category"] == "转账"

        result = asyncio.run(handler.handle_session_step("u1", "0", session, context))
        assert result == {"status": "success", "message": "saved"}
        assert context.end_calls == 1
        assert captured["data"]["transaction_type"] == "transfer"
        assert captured["data"]["account_name"] == "微信"
        assert captured["data"]["counter_account_name"] == "银行卡"
        assert "owner_id" not in captured["data"]

    def test_ledger_add_inline_uses_compact_entry_parser(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.ledger import LedgerHandler

        class _Context:
            async def create_session(self, initial_data=None, timeout=300.0):
                raise AssertionError("inline add should not create a session")

        handler = LedgerHandler(db=MagicMock())
        captured = {}

        async def _fake_save(user_id, data, group_id=None):
            captured["user_id"] = user_id
            captured["data"] = dict(data)
            captured["group_id"] = group_id
            return {"status": "success", "message": "saved"}

        handler._save_ledger_item = _fake_save

        result = asyncio.run(
            handler.handle("u1", "add 28 午饭 cat:餐饮 account:微信", _Context(), group_id=456)
        )

        assert result == {"status": "success", "message": "saved"}
        assert captured["user_id"] == "u1"
        assert captured["group_id"] == 456
        assert captured["data"]["amount"] == 28
        assert captured["data"]["title"] == "午饭"
        assert captured["data"]["ledger_category"] == "餐饮"
        assert captured["data"]["account_name"] == "微信"

    def test_ledger_add_inline_uses_defaults_without_empty_messages(self, tmp_path):
        import sys
        from types import SimpleNamespace

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.ledger import LedgerHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_ledger_add_defaults.db"))

        try:
            handler = LedgerHandler(db=db)
            result = asyncio.run(handler.handle("u1", "add 28 午饭", SimpleNamespace()))
            item = db.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert item is not None
            assert item.amount == 28
            assert item.title == "午饭"
            assert item.transaction_type == "expense"
            assert item.ledger_category == "其他"
            assert item.account_name == "现金"
            assert item.ledger_date
        finally:
            db.cleanup()

    def test_todo_add_without_args_starts_interactive_session(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.config import PendoConfig
        from plugins.pendo.handlers.task import TaskHandler

        create_calls = []

        class _Context:
            async def create_session(self, initial_data=None, timeout=300.0):
                create_calls.append((initial_data, timeout))

        handler = TaskHandler(db=MagicMock())
        result = asyncio.run(handler.handle("u1", "add", _Context(), group_id=42))

        assert result["status"] == "success"
        assert "开始添加待办" in result["message"]
        assert "下一步只需要填写计划日期" in result["message"]
        assert "一条命令或 edit" in result["message"]
        assert create_calls[0][0]["type"] == PendoConfig.SESSION_TYPE_TASK_ADD
        assert create_calls[0][0]["step"] == "title"
        assert create_calls[0][0]["group_id"] == 42

    def test_todo_add_inline_still_uses_quick_parser(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.services.db import Database

        class _Context:
            async def create_session(self, initial_data=None, timeout=300.0):
                raise AssertionError("inline todo add should not create a session")

        db = Database(str(tmp_path / "pendo_todo_inline_add.db"))
        try:
            handler = TaskHandler(db=db)
            result = asyncio.run(
                handler.handle(
                    "u1",
                    "add 写项目周报 cat:工作 p:2 plan:2026-05-01 "
                    "deadline:2026-05-01T18:00 remind:2026-04-30T09:00 #周报",
                    _Context(),
                )
            )
            item = db.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert item is not None
            assert item.title == "写项目周报"
            assert item.category == "工作"
            assert item.priority == 2
            assert item.plan_date == "2026-05-01"
            assert item.deadline_at == "2026-05-01T10:00:00+00:00"
            assert item.remind_times == ["2026-04-30T01:00:00+00:00"]
            assert item.tags == ["周报"]
            assert "分类: 工作" in result["message"]
            assert "优先级" in result["message"]
            assert "提醒: 1 个" in result["message"]
            assert "标签: #周报" in result["message"]
        finally:
            db.cleanup()

    def test_todo_add_session_collects_content_then_finishes_after_plan_date(
        self, tmp_path, monkeypatch
    ):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.services.db import Database

        class _Session(dict):
            def set(self, key, value):
                self[key] = value

        class _Context:
            def __init__(self):
                self.end_calls = 0

            async def end_session(self):
                self.end_calls += 1
                return True

        db = Database(str(tmp_path / "pendo_todo_session_defaults.db"))
        try:
            handler = TaskHandler(db=db)
            monkeypatch.setattr(
                handler, "_user_local_now", lambda _user_id: datetime(2026, 5, 1, 10, 0)
            )

            context = _Context()
            session = _Session({"step": "title", "data": {}, "group_id": 88})

            result = asyncio.run(handler.handle_session_step("u1", "写周报", session, context))
            assert result["status"] == "success"
            assert session["step"] == "plan_date"
            assert session["data"]["title"] == "写周报"

            result = asyncio.run(handler.handle_session_step("u1", "0", session, context))
            item = db.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert context.end_calls == 1
            assert item is not None
            assert item.title == "写周报"
            assert item.plan_date == "2026-05-01"
            assert item.deadline_at is None
            assert item.remind_times == []
            assert item.category == "未分类"
            assert item.priority == 3
            assert item.tags == []
            assert item.context == {"group_id": 88}
            assert "分类: 未分类" not in result["message"]
            assert "优先级" not in result["message"]
        finally:
            db.cleanup()

    def test_todo_add_session_reprompts_invalid_plan_date(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler

        class _Session(dict):
            def set(self, key, value):
                self[key] = value

        handler = TaskHandler(db=MagicMock())
        session = _Session({"step": "plan_date", "data": {"title": "写周报"}})
        result = asyncio.run(
            handler.handle_session_step("u1", "不是日期", session, SimpleNamespace())
        )

        assert result["status"] == "info"
        assert "无法解析计划日期" in result["message"]
        assert session["step"] == "plan_date"

    def test_todo_view_returns_detail(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.models.item import TaskItem, TaskStatus
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_todo_view.db"))

        try:
            task = TaskItem(
                owner_id="u1",
                title="提交报销",
                content="整理发票并提交系统",
                category="工作",
                priority=2,
                status=TaskStatus.OPEN,
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(task, "tsk12345")

            handler = TaskHandler(db=db)
            result = asyncio.run(handler.handle("u1", "view tsk12345", SimpleNamespace()))

            assert result["status"] == "success"
            assert "提交报销" in result["message"]
            assert "分类: 工作" in result["message"]
            assert "/pendo todo done tsk12345" in result["message"]
        finally:
            db.cleanup()

    def test_todo_done_with_note_id_returns_hint_without_mutating_note(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.models.item import ItemType, NoteItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_todo_wrong_type.db"))

        try:
            note = NoteItem(
                owner_id="u1",
                title="采购清单",
                content="牛奶 面包",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(note, "not12345")

            handler = TaskHandler(db=db)
            result = asyncio.run(handler.mark_done("u1", "not12345", SimpleNamespace()))
            preserved = db.get_item("not12345", "u1")

            assert result["status"] == "success"
            assert "不是待办ID" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.NOTE
        finally:
            db.cleanup()

    def test_todo_unknown_command_returns_error_instead_of_silent_list(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_todo_unknown_cmd.db"))

        try:
            handler = TaskHandler(db=db)
            result = asyncio.run(handler.handle("u1", "viwe abc12345", SimpleNamespace()))

            assert result["status"] == "error"
            assert "未知待办命令: viwe" in result["message"]
        finally:
            db.cleanup()

    def test_todo_top_level_undo_returns_canonical_usage(self):
        from plugins.pendo.handlers.task import TaskHandler

        result = asyncio.run(TaskHandler(SimpleNamespace()).handle("u1", "undo", SimpleNamespace()))

        assert result == {"status": "error", "message": "❌ 正确用法:\n\n/pendo undo"}

    def test_todo_category_shortcut_still_routes_to_list(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.models.item import TaskItem, TaskStatus
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_todo_shortcut.db"))

        try:
            task = TaskItem(
                owner_id="u1",
                title="提交报销",
                category="工作",
                priority=2,
                status=TaskStatus.OPEN,
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(task, "tsk12345")

            handler = TaskHandler(db=db)
            result = asyncio.run(handler.handle("u1", "工作 done", SimpleNamespace()))

            assert result["status"] == "success"
            assert "工作" in result["message"]
        finally:
            db.cleanup()

    def test_event_edit_with_diary_id_returns_hint_without_mutation(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import DiaryItem, ItemType
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_event_edit_wrong_type.db"))

        try:
            diary = DiaryItem(
                owner_id="u1",
                title="2026-03-28 日记",
                content="今天周六。",
                diary_date="2026-03-28",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(diary, "dia12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(
                handler.edit_event("u1", "dia12345 改到明天下午两点", SimpleNamespace())
            )
            preserved = db.get_item("dia12345", "u1")

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert "/pendo diary view dia12345" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.DIARY
            assert preserved.diary_date == "2026-03-28"
        finally:
            db.cleanup()

    def test_event_reminders_view_with_diary_id_returns_hint(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import DiaryItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_event_reminders_wrong_type.db"))

        try:
            diary = DiaryItem(
                owner_id="u1",
                title="2026-03-28 日记",
                content="今天周六。",
                diary_date="2026-03-28",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(diary, "8bec805e")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.list_reminders("u1", "8bec805e", SimpleNamespace()))

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert "/pendo diary view 8bec805e" in result["message"]
        finally:
            db.cleanup()

    def test_event_reminders_set_with_note_id_returns_hint_without_mutation(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import ItemType, NoteItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_event_reminders_set_guard.db"))

        try:
            note = NoteItem(
                owner_id="u1",
                title="采购清单",
                content="牛奶 面包",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(note, "not12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(
                handler.set_reminders("u1", "not12345 提前1天提醒", SimpleNamespace())
            )
            preserved = db.get_item("not12345", "u1")

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.NOTE
            assert preserved.title == "采购清单"
        finally:
            db.cleanup()

    def test_event_delete_with_note_id_returns_hint_without_mutation(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import ItemType, NoteItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_event_delete_wrong_type.db"))

        try:
            note = NoteItem(
                owner_id="u1",
                title="采购清单",
                content="牛奶 面包",
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.insert_item(note, "not12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.delete_event("u1", "not12345", SimpleNamespace()))
            preserved = db.get_item("not12345", "u1")

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert "/pendo note view not12345" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.NOTE
            assert preserved.title == "采购清单"
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
