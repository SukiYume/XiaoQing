"""批量操作、参数校验、会话和导出命令。"""

from __future__ import annotations

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    asyncio,
    json,
)


class TestBatchDeleteRefactor:
    def test_task_category_delete_uses_shared_batch_helper(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler

        db = MagicMock()
        db.get_item_ids.return_value = ["t1", "t2"]
        handler = TaskHandler(db)
        calls = []

        async def fake_batch(item_ids, owner_id, item_type, action, details_factory=None):
            calls.append((item_ids, owner_id, item_type, action, details_factory))
            return 2

        monkeypatch.setattr(handler, "_db_batch_soft_delete_with_log", fake_batch, raising=False)

        result = asyncio.run(handler._delete_category_tasks("u1", "工作"))

        assert result["status"] == "success"
        assert calls == [(["t1", "t2"], "u1", "task", "delete_task", None)]

    def test_note_category_delete_uses_shared_batch_helper(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler

        db = MagicMock()
        db.get_item_ids.return_value = ["n1", "n2"]
        handler = NoteHandler(db)
        calls = []

        async def fake_batch(item_ids, owner_id, item_type, action, details_factory=None):
            calls.append((item_ids, owner_id, item_type, action, details_factory))
            return 2

        monkeypatch.setattr(handler, "_db_batch_soft_delete_with_log", fake_batch, raising=False)

        result = asyncio.run(handler._delete_category_notes("u1", "工作", {}))

        assert result["status"] == "success"
        assert len(calls) == 1
        item_ids, owner_id, item_type, action, details_factory = calls[0]
        assert item_ids == ["n1", "n2"]
        assert owner_id == "u1"
        assert item_type == "note"
        assert action == "delete_note"  # 与 task 的 "delete_task" 保持命名对称
        assert details_factory is None  # 不再传多余的 details_factory


class TestCommandValidationRegression:
    def test_note_add_invalid_category_returns_validation_error(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_invalid_category.db"))
        try:
            handler = NoteHandler(db)
            result = asyncio.run(
                handler.create_note(
                    "u-note-validation",
                    "title:bad content body cat:<script>",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "error"
            assert "分类名只能包含" in result["message"]
            assert db.get_items("u-note-validation", filters={"type": "note"}) == []
        finally:
            db.cleanup()

    def test_todo_add_invalid_priority_is_rejected(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_todo_invalid_priority.db"))
        try:
            handler = TaskHandler(db)
            result = asyncio.run(
                handler.add_task(
                    "u-task-validation",
                    "非法优先级 p:9 cat:测试",
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "error"
            assert "优先级必须在1-5之间" in result["message"]
            assert db.get_items("u-task-validation", filters={"type": "task"}) == []
        finally:
            db.cleanup()


class TestTriggerConflictRegression:
    def test_pendo_manifest_triggers_match_runtime_normalization(self):
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        from core.router import CommandRouter, CommandSpec
        from plugins.pendo import main as pendo_main

        commands = {item["name"]: item for item in config["commands"]}
        assert commands["pendo"]["triggers"] == ["pendo"]
        assert "笔记" not in commands
        assert set(pendo_main.TRIGGER_SUBCOMMAND_MAP) == {"日程", "待办", "日记"}

        router = CommandRouter()
        for item in config["commands"]:
            router.register(
                CommandSpec(
                    plugin="pendo",
                    name=item["name"],
                    triggers=item["triggers"],
                    help_text=item["help"],
                    admin_only=False,
                    handler=pendo_main.handle,
                )
            )

        for trigger, subcommand in pendo_main.TRIGGER_SUBCOMMAND_MAP.items():
            assert commands[trigger]["triggers"] == [trigger]
            resolved = router.resolve(f"{trigger} list")
            assert resolved is not None
            spec, args = resolved
            assert spec.name == trigger
            assert pendo_main._normalize_trigger_args(spec.name, args) == f"{subcommand} list"


class TestSessionRegression:
    def test_simple_session_types_dispatch_directly_to_cached_handlers(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import session as session_module
        from plugins.pendo.config import PendoConfig

        calls = []

        class _DiaryHandler:
            async def handle_session_message(self, user_id, text, context, session):
                calls.append(("diary", user_id, text, session))
                return {"status": "success", "message": "diary"}

        class _StepHandler:
            def __init__(self, label):
                self.label = label

            async def handle_session_step(self, user_id, text, session, context):
                calls.append((self.label, user_id, text, session))
                return {"status": "success", "message": self.label}

        class _Context:
            def __init__(self):
                self.state = {
                    "pendo_runtime": {
                        "services": {
                            "diary_handler": _DiaryHandler(),
                            "task_handler": _StepHandler("task"),
                            "ledger_handler": _StepHandler("ledger"),
                        }
                    }
                }

        context = _Context()
        cases = (
            (PendoConfig.SESSION_TYPE_DIARY_TEMPLATE, "diary"),
            (PendoConfig.SESSION_TYPE_TASK_ADD, "task"),
            (PendoConfig.SESSION_TYPE_LEDGER_ADD, "ledger"),
        )
        for session_type, expected in cases:
            session = {"type": session_type}
            result = asyncio.run(
                session_module.handle_session_message("1001", "下一步", session, context)
            )
            assert result == {"status": "success", "message": expected}

        assert [call[:3] for call in calls] == [
            ("diary", "1001", "下一步"),
            ("task", "1001", "下一步"),
            ("ledger", "1001", "下一步"),
        ]

    def test_unknown_session_type_is_ended_so_user_can_restart(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import session as session_module

        end_calls = []

        class _Context:
            async def end_session(self):
                end_calls.append(True)
                return True

        result = asyncio.run(
            session_module.handle_session_message(
                "1001",
                "任意消息",
                {"type": "removed_session_type"},
                _Context(),
            )
        )

        assert result == {
            "status": "error",
            "message": "未知的会话类型: removed_session_type",
        }
        assert end_calls == [True]

    def test_event_sessions_with_corrupt_data_end_session(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import session as session_module

        end_calls = []

        class _Context:
            async def end_session(self):
                end_calls.append(True)
                return True

        conflict_result = asyncio.run(
            session_module.handle_event_conflict_session(
                "1001",
                "是",
                {"data": "corrupt"},
                _Context(),
            )
        )
        info_result = asyncio.run(
            session_module.handle_event_info_session(
                "1001",
                "明天九点",
                {"data": "corrupt"},
                _Context(),
            )
        )

        expected = {"status": "error", "message": "日程会话状态损坏，请重新创建"}
        assert conflict_result == expected
        assert info_result == expected
        assert end_calls == [True, True]

    def test_event_info_only_fills_missing_fields_and_uses_original_offsets(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import session as session_module

        created = []
        offset_calls = []
        end_calls = []

        class _EventHandler:
            async def create_event(self, user_id, parsed_data, context, allow_conflict=False):
                created.append((user_id, dict(parsed_data), allow_conflict))
                return {"status": "success", "message": "ok"}

        class _AiParser:
            async def parse_event_with_ai(self, text, user_id, *, partial=False):
                assert partial is True
                return {
                    "title": "补充消息不应覆盖标题",
                    "content": "补充消息不应覆盖正文",
                    "start_time": "2030-01-02T09:00:00",
                    "remind_offsets": ["提前1小时"],
                }

            def build_remind_times_from_offsets(self, start_time, offsets, user_id=None):
                offset_calls.append((start_time, list(offsets), user_id))
                return ["2030-01-02T08:30:00"]

        class _Context:
            def __init__(self):
                self.state = {
                    "pendo_runtime": {
                        "services": {
                            "event_handler": _EventHandler(),
                            "ai_parser": _AiParser(),
                        }
                    }
                }

            async def end_session(self):
                end_calls.append(True)
                return True

        result = asyncio.run(
            session_module.handle_event_info_session(
                "1001",
                "明天九点",
                {
                    "data": {
                        "owner_id": "forged-owner",
                        "parse_source": "old",
                        "title": "原始标题",
                        "content": "原始正文",
                        "remind_offsets": [" 提前30分钟 "],
                    }
                },
                _Context(),
            )
        )

        assert result == {"status": "success", "message": "ok"}
        assert created == [
            (
                "1001",
                {
                    "title": "原始标题",
                    "content": "原始正文",
                    "start_time": "2030-01-02T09:00:00",
                    "remind_times": ["2030-01-02T08:30:00"],
                    "type": "event",
                },
                False,
            )
        ]
        assert offset_calls == [("2030-01-02T09:00:00", ["提前30分钟"], "1001")]
        assert end_calls == [True]

    def test_active_session_with_missing_raw_message_routes_explicit_command(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo import main as pendo_main

        end_calls = []
        routed = []

        class _Session:
            plugin_name = "pendo"

        class _Context:
            logger = pendo_main.logger
            metrics = None

            async def get_session(self):
                return _Session()

            async def end_session(self):
                end_calls.append(True)
                return True

        async def _fake_route(user_id, args, context, group_id=None, log=None):
            routed.append((user_id, args, group_id))
            return [{"type": "text", "data": {"text": "ok"}}]

        monkeypatch.setattr(pendo_main, "_handle_command_routing", _fake_route)

        result = asyncio.run(
            pendo_main.handle(
                "pendo",
                "todo add CX_RAW_FALLBACK",
                {
                    "user_id": "u-raw",
                    "group_id": 42,
                    "raw_message": None,
                },
                _Context(),
            )
        )

        assert result == [{"type": "text", "data": {"text": "ok"}}]
        assert end_calls == [True]
        assert routed == [("u-raw", "todo add CX_RAW_FALLBACK", 42)]

    def test_event_info_session_keeps_new_conflict_session_active(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import session as session_module

        create_calls = []
        end_calls = []

        class _Context:
            async def create_session(self, initial_data=None, timeout=300.0):
                create_calls.append((initial_data, timeout))

            async def end_session(self):
                end_calls.append(True)
                return True

        class _EventHandler:
            async def create_event(self, user_id, parsed_data, context, allow_conflict=False):
                return {
                    "status": "need_confirm",
                    "data": {"title": parsed_data.get("title", "会议")},
                }

        class _AiParser:
            async def parse_event_with_ai(self, text, user_id, *, partial=False):
                assert partial is True
                return {"title": "会议"}

        context = _Context()
        services = {"event_handler": _EventHandler(), "ai_parser": _AiParser()}

        original_get_cached_services = session_module.get_cached_services
        session_module.get_cached_services = lambda _context: services
        try:
            result = asyncio.run(
                session_module.handle_event_info_session(
                    "u1", "补充信息", {"data": {"title": "会议"}}, context
                )
            )
        finally:
            session_module.get_cached_services = original_get_cached_services

        assert result["status"] == "need_confirm"
        assert len(create_calls) == 1
        assert end_calls == []


class TestOperationAndExportRegression:
    def test_confirm_handles_aware_reminder_times(self, monkeypatch):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import operations
        from plugins.pendo.commands.operations import handle_confirm
        from plugins.pendo.models.item import EventItem

        fixed_now = datetime.fromisoformat("2030-01-01T09:30:00+08:00")
        monkeypatch.setattr(operations.TimezoneHelper, "now", lambda tz=None: fixed_now)

        db = MagicMock()
        db.get_item.return_value = EventItem(
            id="evt123",
            title="带时区提醒",
            remind_times=[
                "2030-01-01T08:00:00+08:00",
                "2030-01-01T10:00:00+08:00",
            ],
        )
        db.get_last_unconfirmed_remind_time.return_value = "2030-01-01T08:00:00+08:00"

        reminder_service = MagicMock()
        reminder_service.confirm_reminder.return_value = {"status": "success", "message": "ok"}

        result = asyncio.run(handle_confirm("u1", "evt123", reminder_service, db))

        assert result["status"] == "success"
        assert "后续还有 1 个提醒" in result["message"]
        reminder_service.confirm_reminder.assert_called_once_with(
            "evt123",
            "confirmed",
            "u1",
            "2030-01-01T08:00:00+08:00",
        )

    def test_snooze_keeps_future_aware_reminders(self, monkeypatch):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import operations
        from plugins.pendo.commands.operations import handle_snooze
        from plugins.pendo.models.item import EventItem

        fixed_now = datetime.fromisoformat("2030-01-01T09:30:00+08:00")

        monkeypatch.setattr(operations.TimezoneHelper, "now", lambda tz=None: fixed_now)
        monkeypatch.setattr(
            operations,
            "_parse_snooze_time",
            lambda time_arg, base_time=None, now=None: "2030-01-01T11:00:00+08:00",
        )

        db = MagicMock()
        db.get_item.return_value = EventItem(
            id="evt123",
            title="延后测试",
            remind_times=[
                "2030-01-01T08:00:00+08:00",
                "2030-01-01T10:00:00+08:00",
            ],
        )
        db.get_last_unconfirmed_remind_time.return_value = None
        db.update_item.return_value = True

        reminder_service = MagicMock()
        reminder_service.db = db
        reminder_service.confirm_reminder.return_value = {"status": "success", "message": "ok"}

        result = asyncio.run(handle_snooze("u1", "evt123 10m", reminder_service))

        assert result["status"] == "success"
        assert "已将提醒延后到: 2030-01-01T11:00:00+08:00" in result["message"]
        db.update_item.assert_called_once()
        reminder_service.confirm_reminder.assert_not_called()

    def test_snooze_missing_args_returns_error_result(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands.operations import handle_snooze

        result = asyncio.run(handle_snooze("u1", "", SimpleNamespace(db=None)))

        assert result["status"] == "error"
        assert "请指定要延后的条目ID和时间" in result["message"]

    def test_export_markdown_writes_single_file_and_filters_types(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.exporter import ExporterService

        event_item = SimpleNamespace(
            id="evt1",
            type="event",
            title="项目周会",
            category="工作",
            tags=["会议"],
            created_at="2026-03-10T09:00:00",
            updated_at="2026-03-10T10:00:00",
            start_time="2026-03-12T09:30:00",
            end_time="2026-03-12T10:30:00",
            location="腾讯会议",
            remind_times=["2026-03-12T09:00:00"],
            notes="带上进度表",
            content="讨论本周排期",
        )
        task_item = SimpleNamespace(
            id="todo1",
            type="task",
            title="提交周报",
            category="工作",
            tags=["例行"],
            created_at="2026-03-08T08:00:00",
            updated_at="2026-03-09T08:00:00",
            plan_date="2026-03-15",
            deadline_at="2026-03-15T18:00:00",
            priority=2,
            status="open",
            completed_at=None,
            cancelled_at=None,
            content="同步给导师和组会群",
        )
        note_item = SimpleNamespace(
            id="note1",
            type="note",
            title="研究想法",
            category="灵感",
            tags=["论文"],
            created_at="2026-03-11T08:00:00",
            updated_at="2026-03-11T08:30:00",
            content="这条不应该被导出到 event,todo 结果里",
        )

        class _Repo:
            def get_items(self, user_id, filters, limit, *, use_cache):
                assert use_cache is False
                item_type = filters.get("type")
                if item_type == "event":
                    return [event_item]
                if item_type == "task":
                    return [task_item]
                if item_type == "note":
                    return [note_item]
                return []

        _repo = _Repo()
        _repo.log_transfer = lambda **kwargs: 1
        service = ExporterService(_repo, tmp_path)
        result = service.export_markdown(
            "u1",
            "工作档案 2026-03-01..2026-03-31 event,todo",
            {},
        )

        assert result["status"] == "success"
        assert result["record_count"] == 2
        assert result["file_name"] == "工作档案.md"

        exported = (tmp_path / "u1" / "工作档案.md").read_text(encoding="utf-8")
        assert "# Pendo 导出档案 · 工作档案" in exported
        assert "## 导出摘要" in exported
        assert "## 日程" in exported
        assert "## 待办" in exported
        assert "项目周会" in exported
        assert "提交周报" in exported
        assert "研究想法" not in exported

    def test_export_markdown_includes_note_references(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.exporter import ExporterService

        note_item = SimpleNamespace(
            id="note1",
            type="note",
            title="读书摘录",
            category="学习",
            tags=["阅读"],
            created_at="2026-03-11T08:00:00",
            updated_at="2026-03-11T08:30:00",
            content="正文",
            references=[{"kind": "item", "id": "task1", "type": "task", "title": "整理卡片"}],
        )

        class _Repo:
            def get_items(self, user_id, filters, limit, *, use_cache):
                assert use_cache is False
                return [note_item] if filters.get("type") == "note" else []

        _repo = _Repo()
        _repo.log_transfer = lambda **kwargs: 1
        service = ExporterService(_repo, tmp_path)
        result = service.export_markdown("u1", "笔记档案 note", {})

        assert result["status"] == "success"
        exported = (tmp_path / "u1" / "笔记档案.md").read_text(encoding="utf-8")
        assert "**关联条目**" in exported
        assert "- 待办: 整理卡片 (`task1`)" in exported

    def test_export_markdown_requires_filename(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.exporter import ExporterService

        service = ExporterService(SimpleNamespace(), tmp_path)
        result = service.export_markdown("u1", "", {})

        assert result["status"] == "error"
        assert "请提供导出文件名" in result["message"]

    def test_export_markdown_uses_event_collection_context(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.exporter import ExporterService

        event_item = SimpleNamespace(
            id="conf2026_m01",
            owner_id="u1",
            type="event",
            title="摘要截止",
            category="未分类",
            tags=[],
            created_at="2026-03-01T09:00:00",
            updated_at="2026-03-01T09:00:00",
            start_time="2026-03-05T09:00:00",
            end_time=None,
            location="",
            remind_times=[],
            notes="节点备注",
            content="",
            event_collection_id="conf2026",
            event_collection_kind="multi_node",
        )

        class _Repo:
            def get_items(self, user_id, filters, limit, *, use_cache):
                assert use_cache is False
                return [event_item] if filters.get("type") == "event" else []

            def get_event_collection(self, collection_id, owner_id=None):
                return {
                    "id": collection_id,
                    "kind": "multi_node",
                    "title": "FRB2026会议",
                    "category": "学术",
                    "location": "上海",
                    "notes": "整体备注",
                }

        _repo = _Repo()
        _repo.log_transfer = lambda **kwargs: 1
        service = ExporterService(_repo, tmp_path)
        result = service.export_markdown("u1", "日程导出 event", {})

        assert result["status"] == "success"
        exported = (tmp_path / "u1" / "日程导出.md").read_text(encoding="utf-8")
        assert "### 01. FRB2026会议 · 摘要截止" in exported
        assert "| 分类 | 学术 |" in exported
        assert "| 地点 | 上海 |" in exported
        assert "- 集合标题: FRB2026会议" in exported
        assert "- 集合类型: multi_node" in exported
