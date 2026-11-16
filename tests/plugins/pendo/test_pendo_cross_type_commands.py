"""日记、任务和跨类型命令。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest


class TestCrossTypeCommands:
    @pytest.mark.parametrize(
        ("handler_name", "method", "args", "item_kind", "item_id", "expected"),
        [
            pytest.param(
                "note",
                "view_note",
                "evt12345",
                "event",
                "evt12345",
                ("不是笔记ID", "/pendo event view evt12345"),
                id="note-view-event-id",
            ),
            pytest.param(
                "note",
                "delete_note",
                "evt12345",
                "event",
                "evt12345",
                ("不是笔记ID",),
                id="note-delete-event-id",
            ),
            pytest.param(
                "ledger",
                "handle",
                "view dia12345",
                "diary",
                "dia12345",
                ("不是账目ID", "/pendo diary view dia12345"),
                id="ledger-view-diary-id",
            ),
            pytest.param(
                "ledger",
                "edit_ledger",
                "not12345 amount:50 cat:交通",
                "note",
                "not12345",
                ("不是账目ID",),
                id="ledger-edit-note-id",
            ),
            pytest.param(
                "todo",
                "mark_done",
                "not12345",
                "note",
                "not12345",
                ("不是待办ID",),
                id="todo-done-note-id",
            ),
            pytest.param(
                "event",
                "handle",
                "view 82d34407",
                "diary",
                "82d34407",
                ("不是日程ID", "/pendo diary view 82d34407", "2026-03-28"),
                id="event-view-diary-id",
            ),
            pytest.param(
                "event",
                "edit_event",
                "dia12345 改到明天下午两点",
                "diary",
                "dia12345",
                ("不是日程ID", "/pendo diary view dia12345"),
                id="event-edit-diary-id",
            ),
            pytest.param(
                "event",
                "list_reminders",
                "8bec805e",
                "diary",
                "8bec805e",
                ("不是日程ID", "/pendo diary view 8bec805e"),
                id="event-reminders-view-diary-id",
            ),
            pytest.param(
                "event",
                "set_reminders",
                "not12345 提前1天提醒",
                "note",
                "not12345",
                ("不是日程ID",),
                id="event-reminders-set-note-id",
            ),
            pytest.param(
                "event",
                "delete_event",
                "not12345",
                "note",
                "not12345",
                ("不是日程ID", "/pendo note view not12345"),
                id="event-delete-note-id",
            ),
            pytest.param(
                "diary",
                "view_diary",
                "evt12345",
                "event",
                "evt12345",
                ("不是日记ID", "/pendo event view evt12345"),
                id="diary-view-event-id",
            ),
            pytest.param(
                "diary",
                "delete_diary",
                "evt12345",
                "event",
                "evt12345",
                ("不是日记ID", "/pendo event view evt12345"),
                id="diary-delete-event-id",
            ),
        ],
    )
    def test_command_on_other_type_id_returns_hint_without_mutation(
        self, db, handler_name, method, args, item_kind, item_id, expected
    ):
        from unittest.mock import MagicMock

        from plugins.pendo.handlers.diary import DiaryHandler
        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.handlers.ledger import LedgerHandler
        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.models.item import DiaryItem, EventItem, NoteItem

        timestamps = {"created_at": "2026-03-28T21:41:10", "updated_at": "2026-03-28T21:41:10"}
        items      = {
            "event": EventItem(
                owner_id="u1", title="晨会", start_time="2026-03-29T09:00:00", **timestamps
            ),
            "diary": DiaryItem(
                owner_id   = "u1",
                title      = "2026-03-28 日记",
                content    = "今天周六。",
                diary_date = "2026-03-28",
                **timestamps,
            ),
            "note": NoteItem(owner_id="u1", title="采购清单", content="牛奶 面包", **timestamps),
        }
        handlers = {
            "note"  : NoteHandler(db=db),
            "ledger": LedgerHandler(db=db),
            "todo"  : TaskHandler(db=db),
            "event" : EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock()),
            "diary" : DiaryHandler(db=db),
        }
        db.insert_item(items[item_kind], item_id)
        before = db.get_item(item_id, "u1").to_dict()

        result = asyncio.run(getattr(handlers[handler_name], method)("u1", args, SimpleNamespace()))

        assert result["status"] == "success"
        for fragment in expected:
            assert fragment in result["message"]
        assert db.get_item(item_id, "u1").to_dict() == before

    @pytest.mark.parametrize(
        ("raw", "title", "content", "category", "tags"),
        [
            pytest.param(
                "title:xxx\n1. xxx\n2. xxx\n3. xxx\ncat:其他 #xx",
                "xxx",
                "1. xxx\n2. xxx\n3. xxx",
                "其他",
                ["xx"],
                id="multiline-body",
            ),
            pytest.param(
                "title:我的标题 content 这里是详细正文 cat:工作 #学习",
                "我的标题",
                "这里是详细正文",
                "工作",
                ["学习"],
                id="explicit-content-keyword",
            ),
            pytest.param(
                "title:测试 xxx cat:其他 #11 #22 #33",
                "测试",
                "xxx",
                "其他",
                ["11", "22", "33"],
                id="title-token-first-body-after",
            ),
            pytest.param(
                "xxx title:测试 cat:其他 #11 #22 #33",
                "测试",
                "xxx",
                "其他",
                ["11", "22", "33"],
                id="title-token-in-middle",
            ),
            pytest.param(
                "title:我的 笔记标题\n# 一级标题\n正文内容\ncat:工作 #归档",
                "我的 笔记标题",
                "# 一级标题\n正文内容",
                "工作",
                ["归档"],
                id="markdown-heading-stays-in-body",
            ),
        ],
    )
    def test_note_add_parses_title_content_category_and_tags(
        self, db, raw, title, content, category, tags
    ):
        from plugins.pendo.handlers.note import NoteHandler

        result = asyncio.run(NoteHandler(db=db).create_note("u1", raw, SimpleNamespace()))

        assert result["status"] == "success"
        item = db.get_item(result["item_id"], "u1")
        assert item is not None
        assert item.title == title
        assert item.content == content
        assert item.category == category
        assert item.tags == tags

    def test_note_add_parses_references_and_view_shows_linked_item(self, tmp_path):

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_reference.db"))

        try:
            db.insert_item(
                EventItem(
                    owner_id   = "u1",
                    title      = "晨会",
                    start_time = "2026-04-20T09:00:00",
                    created_at = "2026-04-19T21:00:00",
                    updated_at = "2026-04-19T21:00:00",
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
            assert "日程: 晨会 evt12345" in view["message"]
        finally:
            db.cleanup()

    def test_note_append_tag_untag_link_and_backlink_flow(self, tmp_path):

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.models.item import NoteItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_command_flow.db"))

        try:
            db.insert_item(
                NoteItem(
                    owner_id   = "u1",
                    title      = "主笔记",
                    content    = "第一段",
                    tags       = ["原始"],
                    category   = "工作",
                    created_at = "2026-04-20T09:00:00",
                    updated_at = "2026-04-20T09:00:00",
                ),
                "note_main",
            )
            db.insert_item(
                NoteItem(
                    owner_id   = "u1",
                    title      = "引用笔记",
                    content    = "引用正文",
                    category   = "工作",
                    created_at = "2026-04-20T10:00:00",
                    updated_at = "2026-04-20T10:00:00",
                ),
                "note_ref",
            )

            handler = NoteHandler(db=db)
            append = asyncio.run(handler.append_note("u1", "note_main 第二段", SimpleNamespace()))
            tag    = asyncio.run(handler.tag_note("u1", "note_main #新增 #原始", SimpleNamespace()))
            untag  = asyncio.run(handler.untag_note("u1", "note_main #原始", SimpleNamespace()))
            link   = asyncio.run(handler.link_note("u1", "note_ref note_main", SimpleNamespace()))

            updated = db.get_item("note_main", "u1")
            linked  = db.get_item("note_ref", "u1")
            view    = asyncio.run(handler.view_note("u1", "note_main", SimpleNamespace()))

            assert append["status"] == "success"
            assert tag["status"] == "success"
            assert untag["status"] == "success"
            assert link["status"] == "success"
            assert updated.content == "第一段\n\n第二段"
            assert updated.tags == ["新增"]
            assert linked.related_items == ["note_main"]
            assert "被这些笔记引用" in view["message"]
            assert "引用笔记 note_ref" in view["message"]
        finally:
            db.cleanup()

    def test_ledger_add_session_starts_with_amount_then_numeric_options(self):
        from unittest.mock import MagicMock

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

    def test_ledger_add_session_collects_typed_front_fields_then_numeric_options(self, db):
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

        handler = LedgerHandler(db=db)
        context = _Context()
        session = _Session({"step": "amount", "data": {}, "group_id": 123})

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
        assert result["status"] == "success"
        assert context.end_calls == 1
        saved = db.get_item(result["item_id"], "u1")
        assert saved is not None
        assert saved.amount == 88.5
        assert saved.title == "午饭"
        assert saved.transaction_type == "expense"
        assert saved.account_name == "微信"
        assert saved.ledger_category == "餐饮"
        assert not saved.merchant
        assert saved.context == {"group_id": 123}

        context = _Context()
        session = _Session(
            {
                "step"    : "transaction_type",
                "data"    : {"amount": 1000, "title": "还款", "owner_id": "u-spoofed"},
                "group_id": None,
            }
        )
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
        assert result["status"] == "success"
        assert context.end_calls == 1
        saved = db.get_item(result["item_id"], "u1")
        assert saved is not None
        assert saved.transaction_type == "transfer"
        assert saved.account_name == "微信"
        assert saved.counter_account_name == "银行卡"
        assert db.get_items("u-spoofed", filters={"type": "ledger"}) == []

    def test_ledger_add_inline_uses_compact_entry_parser(self, db):
        from plugins.pendo.handlers.ledger import LedgerHandler

        class _Context:
            async def create_session(self, initial_data=None, timeout=300.0):
                raise AssertionError("inline add should not create a session")

        handler = LedgerHandler(db=db)

        result = asyncio.run(
            handler.handle("u1", "add 28 午饭 cat:餐饮 account:微信", _Context(), group_id=456)
        )

        assert result["status"] == "success"
        saved = db.get_item(result["item_id"], "u1")
        assert saved is not None
        assert saved.context == {"group_id": 456}
        assert saved.amount == 28
        assert saved.title == "午饭"
        assert saved.ledger_category == "餐饮"
        assert saved.account_name == "微信"

    def test_ledger_add_inline_uses_defaults_without_empty_messages(self, tmp_path):
        from types import SimpleNamespace

        from plugins.pendo.handlers.ledger import LedgerHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_ledger_add_defaults.db"))

        try:
            handler = LedgerHandler(db=db)
            result = asyncio.run(handler.handle("u1", "add 28 午饭", SimpleNamespace()))
            item   = db.get_item(result["item_id"], "u1")

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
        from unittest.mock import MagicMock

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

    def test_todo_add_inline_uses_quick_parser(self, tmp_path):

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

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.services.db import Database
        from plugins.pendo.utils import time_utils

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                current = cls(2026, 5, 1, 10, 0, 0)
                return current if tz is None else current.replace(tzinfo=tz)

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
            monkeypatch.setattr(time_utils, "datetime", _FrozenDateTime)

            context = _Context()
            session = _Session({"step": "title", "data": {}, "group_id": 88})

            result = asyncio.run(handler.handle_session_step("u1", "写周报", session, context))
            assert result["status"] == "success"
            assert session["step"] == "plan_date"
            assert session["data"]["title"] == "写周报"

            result = asyncio.run(handler.handle_session_step("u1", "0", session, context))
            item   = db.get_item(result["item_id"], "u1")

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
        from unittest.mock import MagicMock

        from plugins.pendo.handlers.task import TaskHandler

        class _Session(dict):
            def set(self, key, value):
                self[key] = value

        handler = TaskHandler(db=MagicMock())
        session = _Session({"step": "plan_date", "data": {"title": "写周报"}})
        result  = asyncio.run(
            handler.handle_session_step("u1", "不是日期", session, SimpleNamespace())
        )

        assert result["status"] == "info"
        assert "无法解析计划日期" in result["message"]
        assert session["step"] == "plan_date"

    def test_todo_view_returns_detail(self, tmp_path):

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.models.item import TaskItem, TaskStatus
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_todo_view.db"))

        try:
            task = TaskItem(
                owner_id   = "u1",
                title      = "提交报销",
                content    = "整理发票并提交系统",
                category   = "工作",
                priority   = 2,
                status     = TaskStatus.OPEN,
                created_at = "2026-03-28T21:41:10",
                updated_at = "2026-03-28T21:41:10",
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

    def test_todo_unknown_command_returns_error_instead_of_silent_list(self, tmp_path):

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

    def test_todo_category_shortcut_routes_to_list(self, tmp_path):

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.models.item import TaskItem, TaskStatus
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_todo_shortcut.db"))

        try:
            task = TaskItem(
                owner_id   = "u1",
                title      = "提交报销",
                category   = "工作",
                priority   = 2,
                status     = TaskStatus.OPEN,
                created_at = "2026-03-28T21:41:10",
                updated_at = "2026-03-28T21:41:10",
            )
            db.insert_item(task, "tsk12345")

            handler = TaskHandler(db=db)
            result = asyncio.run(handler.handle("u1", "工作 done", SimpleNamespace()))

            assert result["status"] == "success"
            assert "工作" in result["message"]
        finally:
            db.cleanup()
