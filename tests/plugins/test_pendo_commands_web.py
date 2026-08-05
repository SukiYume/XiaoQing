"""批量操作、导出和 Web 命令。"""

from __future__ import annotations

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    asyncio,
    datetime,
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


class TestPendoWebHandler:
    """测试 pendo web 命令格式化与发送行为"""

    def test_web_token_sends_raw_code_as_separate_private_message(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url=lambda: "http://127.0.0.1:8765",
                is_running=lambda: True,
                start=lambda _db: True,
                stop=lambda: True,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        issuance = {}

        def issue_code(owner_id, *, expires_seconds):
            issuance.update(owner_id=owner_id, expires_seconds=expires_seconds)
            return "mock-code"

        monkeypatch.setattr(web_module, "issue_login_code", issue_code)
        monkeypatch.setattr(web_module.web_server, "get_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(web_module.web_server, "is_running", lambda: True)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=None)

        result = asyncio.run(handler.handle("1001", "token", context=context))

        assert result["status"] == "success"
        assert "登录 Code 已单独私聊发送" in result["message"]
        assert "7 天，仅可使用一次" in result["message"]
        assert "mock-code" not in result["message"]
        assert issuance == {"owner_id": "1001", "expires_seconds": 7 * 24 * 60 * 60}
        assert len(actions) == 1
        assert actions[0]["action"] == "send_private_msg"
        assert actions[0]["params"]["user_id"] == 1001
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert token_text == "mock-code"
        assert "http://" not in token_text
        assert "https://" not in token_text

    def test_web_token_fails_closed_when_private_delivery_is_unavailable(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url=lambda: "http://127.0.0.1:8765",
                is_running=lambda: False,
                start=lambda _db: True,
                stop=lambda: True,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        monkeypatch.setattr(web_module, "issue_login_code", lambda *_args, **_kwargs: "mock-code")
        monkeypatch.setattr(web_module.web_server, "get_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(web_module.web_server, "is_running", lambda: False)

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(handler.handle("1001", "token", context=None))

        assert result["status"] == "error"
        assert "无法通过私聊安全发送凭据" in result["message"]
        assert "mock-code" not in result["message"]
        assert "登录 Code:" not in result["message"]

    def test_web_token_reports_unknown_private_delivery_without_exposing_credential(self):
        from plugins.pendo.handlers.web import WebHandler

        async def unknown_delivery(_action):
            return None

        outcome = asyncio.run(
            WebHandler._send_private_text(
                SimpleNamespace(send_action=unknown_delivery),
                "1001",
                "secret-token",
            )
        )
        result = WebHandler._build_token_result(
            token_sent=outcome,
            header="Pendo Web",
            success_line="generated",
            expiry_text="5 minutes",
            private_hint="sent",
            private_copy_hint="copy",
        )

        assert outcome is None
        assert result["status"] == "success"
        assert "未收到最终投递回执" in result["message"]
        assert "secret-token" not in result["message"]

    def test_web_start_surfaces_last_start_error(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url=lambda: "http://127.0.0.1:8765",
                is_running=lambda: False,
                start=lambda _db: False,
                stop=lambda: True,
                get_last_error=lambda: "无法绑定到 127.0.0.1:8765，端口可能已被占用。",
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(handler.handle("1001", "start", context=None))

        assert result["status"] == "error"
        assert "服务启动失败" in result["message"]
        assert "端口可能已被占用" in result["message"]
        assert "plugins.pendo.web_port" in result["message"]

    def test_web_stop_reports_external_running_server_without_failing(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url=lambda: "http://127.0.0.1:8765",
                is_running=lambda: True,
                is_managed_running=lambda: False,
                start=lambda _db: False,
                stop=lambda: False,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(handler.handle("1001", "stop", context=None))

        assert result["status"] == "success"
        assert "外部服务" in result["message"]

    def test_web_widget_token_sends_token_as_separate_private_message(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url=lambda: "http://127.0.0.1:8765",
                is_running=lambda: True,
                start=lambda _db: True,
                stop=lambda: True,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")
        issuance = {}

        def issue_widget(owner_id, *, expires_hours, db):
            issuance.update(owner_id=owner_id, expires_hours=expires_hours, db=db)
            return "widget-token"

        monkeypatch.setattr(web_module, "generate_widget_token", issue_widget)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=None)

        result = asyncio.run(handler.handle("1001", "widget-token", context=context))

        assert result["status"] == "success"
        assert "Widget Token 已单独私聊发送" in result["message"]
        assert "365 天" in result["message"]
        assert "widget-token" not in result["message"]
        assert issuance == {"owner_id": "1001", "expires_hours": 24 * 365, "db": None}
        assert len(actions) == 1
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert "Pendo Web Widget Token" in token_text
        assert "widget-token" in token_text

    def test_web_widget_revoke_revokes_only_callers_registered_tokens(self, monkeypatch):
        import importlib

        web_module = importlib.import_module("plugins.pendo.handlers.web")
        calls = []
        db = SimpleNamespace(
            revoke_widget_tokens=lambda owner_id: calls.append(owner_id) or 2,
        )
        handler = web_module.WebHandler(db=db)

        result = asyncio.run(handler.handle("1001", "widget-revoke", context=None))

        assert result == {
            "status": "success",
            "message": "✅ 已吊销 2 个 Widget Token；请重新生成并录入 Keychain",
        }
        assert calls == ["1001"]


class TestPendoSearchAndImportRegression:
    def test_search_handler_applies_date_field_for_range_filters(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.search import SearchHandler

        monkeypatch.setattr(
            "plugins.pendo.utils.time_utils.now_in_timezone",
            lambda _user_id, _db: datetime(2026, 5, 3, 16, 30, 0),
        )

        calls = []

        class _ItemsRepo:
            def search_items_page(self, owner_id, query, filters, *, limit, offset):
                calls.append((owner_id, query, filters))
                assert limit == 15
                assert offset == 0
                return [], 0

        handler = SearchHandler(_ItemsRepo())
        result = asyncio.run(
            handler.search("u1", "会议 type=event range=last7d", context=SimpleNamespace())
        )

        assert result["status"] == "success"
        assert calls
        assert calls[0][2]["date_field"] == "start_time"
        assert "start_date" in calls[0][2]
        assert "end_date" in calls[0][2]

    def test_batch_insert_or_update_refreshes_fts_rows(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))
        try:
            db.batch_insert_or_update(
                [
                    (
                        "insert",
                        {
                            "id": "note-1",
                            "type": "note",
                            "title": "脉冲星速记",
                            "content": "第一次导入内容",
                            "category": "研究",
                            "tags": ["memo"],
                        },
                    )
                ],
                "u1",
            )
            conn = db.get_connection()
            first_row = conn.execute(
                "SELECT title, content FROM items_fts WHERE id = ?",
                ("note-1",),
            ).fetchone()
            assert first_row is not None
            assert first_row["title"] == "脉冲星速记"

            db.batch_insert_or_update(
                [
                    (
                        "update",
                        {
                            "id": "note-1",
                            "type": "note",
                            "title": "脉冲星速记",
                            "content": "更新后的导入内容",
                            "category": "研究",
                            "tags": ["memo"],
                        },
                    )
                ],
                "u1",
            )
            updated_row = conn.execute(
                "SELECT content FROM items_fts WHERE id = ?",
                ("note-1",),
            ).fetchone()
            assert updated_row is not None
            assert updated_row["content"] == "更新后的导入内容"
        finally:
            db.cleanup()


class TestPendoRedesignRegression:
    def test_widget_ledger_panel_prefers_amount_cents(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.db import Database
        from plugins.pendo.web.api.widget import build_widget_summary

        db = Database(str(tmp_path / "pendo.db"))
        owner_id = "u-widget-ledger"
        try:
            db.insert_item(
                {
                    "id": "widget-ledger-expense",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "TEST_SCRIPTABLE 午饭",
                    "amount": 0,
                    "amount_cents": 12345,
                    "transaction_type": "expense",
                    "ledger_category": "餐饮",
                    "ledger_date": "2026-04-30",
                    "account_name": "微信",
                }
            )

            summary = build_widget_summary(
                db,
                owner_id,
                section="ledger",
                now="2026-04-30T12:00:00",
            )

            assert summary["panel"]["items"][0]["amount_text"] == "-¥123"
            assert summary["panel"]["summary"]["primary"] == "支出 ¥123"
        finally:
            db.cleanup()

    def test_sqlite_backup_includes_uncheckpointed_wal_pages(self, tmp_path):
        import sqlite3
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.scripts.migrate_pendo_redesign import backup_sqlite_database

        db_path = tmp_path / "wal-source.db"
        backup_path = tmp_path / "wal-backup.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT)")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("INSERT INTO demo (value) VALUES (?)", ("from-wal",))
            conn.commit()

            backup_sqlite_database(db_path, backup_path)

            backup = sqlite3.connect(backup_path)
            try:
                rows = backup.execute("SELECT value FROM demo").fetchall()
            finally:
                backup.close()
            assert rows == [("from-wal",)]
        finally:
            conn.close()

    def test_export_range_includes_event_spanning_into_window(self):
        import sys
        from datetime import date

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.web.api.transfer import item_matches_range

        spanning_event = SimpleNamespace(
            start_time="2026-04-29T23:00:00+08:00",
            end_time="2026-04-30T01:00:00+08:00",
        )
        before_event = SimpleNamespace(
            start_time="2026-04-29T20:00:00+08:00",
            end_time="2026-04-29T21:00:00+08:00",
        )

        assert item_matches_range(spanning_event, "event", date(2026, 4, 30), date(2026, 4, 30))
        assert not item_matches_range(before_event, "event", date(2026, 4, 30), date(2026, 4, 30))

    def test_collection_category_search_uses_collection_fallback(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))
        owner_id = "u-search-collection"
        try:
            collection_id = db.create_event_collection(
                {
                    "id": "test_collection_category",
                    "owner_id": owner_id,
                    "kind": "multi_node",
                    "title": "TEST_STATS 学术会议",
                    "category": "工作",
                    "location": "上海",
                }
            )
            db.insert_item(
                {
                    "id": "test_collection_node",
                    "owner_id": owner_id,
                    "type": "event",
                    "title": "摘要截止",
                    "category": "",
                    "start_time": "2026-05-10T09:00:00",
                    "event_role": "multi_node_child",
                    "event_collection_id": collection_id,
                    "event_collection_kind": "multi_node",
                    "event_index": 1,
                }
            )

            rows = db.search_items(
                owner_id,
                "学术会议",
                {"type": "event", "category": "工作"},
            )

            assert [row.id for row in rows] == ["test_collection_node"]
        finally:
            db.cleanup()
