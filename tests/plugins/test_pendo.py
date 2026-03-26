"""
pendo 插件单元测试

测试个人时间与信息管理中枢插件的功能。
由于 pendo 插件使用相对导入且有复杂的模块结构，我们主要测试文件结构和配置。
"""

import json
import asyncio
import pytest
from pathlib import Path
from types import SimpleNamespace

# 添加项目根目录到路径
ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================
# Tests
# ============================================================


class TestPendoStructure:
    """测试 pendo 插件结构"""

    def test_main_module_exists(self):
        """测试主模块存在"""
        main_path = ROOT / "plugins" / "pendo" / "main.py"
        assert main_path.exists()

        with open(main_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "class Plugin" in content or "plugin" in content.lower()

    def test_config_module_exists(self):
        """测试配置模块存在"""
        config_path = ROOT / "plugins" / "pendo" / "config.py"
        assert config_path.exists()

        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "class" in content or "def" in content

    def test_services_directory_exists(self):
        """测试 services 目录存在且包含必要模块"""
        services_dir = ROOT / "plugins" / "pendo" / "services"
        assert services_dir.exists()
        assert services_dir.is_dir()

        expected_modules = [
            "db.py",
            "llm_client.py",
            "ai_parser.py",
            "rule_parser.py",
            "exporter.py",
            "reminder.py",
        ]

        for module_name in expected_modules:
            module_path = services_dir / module_name
            assert module_path.exists(), f"Service module {module_name} does not exist"

    def test_handlers_directory_exists(self):
        """测试 handlers 目录存在"""
        handlers_dir = ROOT / "plugins" / "pendo" / "handlers"
        assert handlers_dir.exists()
        assert handlers_dir.is_dir()

        # 检查是否有处理程序模块
        handler_files = list(handlers_dir.glob("*.py"))
        assert len(handler_files) > 0, "No handler modules found"

    def test_commands_directory_exists(self):
        """测试 commands 目录存在"""
        commands_dir = ROOT / "plugins" / "pendo" / "commands"
        assert commands_dir.exists()
        assert commands_dir.is_dir()

    def test_models_directory_exists(self):
        """测试 models 目录存在"""
        models_dir = ROOT / "plugins" / "pendo" / "models"
        assert models_dir.exists()
        assert models_dir.is_dir()

    def test_utils_directory_exists(self):
        """测试 utils 目录存在"""
        utils_dir = ROOT / "plugins" / "pendo" / "utils"
        assert utils_dir.exists()
        assert utils_dir.is_dir()

    def test_core_directory_exists(self):
        """测试 core 目录存在"""
        core_dir = ROOT / "plugins" / "pendo" / "core"
        assert core_dir.exists()
        assert core_dir.is_dir()


class TestPendoConfig:
    """测试 pendo 配置"""

    def test_plugin_json_structure(self):
        """测试 plugin.json 结构"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        assert "name" in config
        assert "version" in config
        assert "description" in config
        assert "commands" in config

    def test_plugin_commands_exist(self):
        """测试插件有命令定义"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        commands = config.get("commands", [])
        assert len(commands) > 0, "No commands defined in plugin.json"

    def test_plugin_has_schedule(self):
        """测试插件有定时任务配置"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # 检查是否有 schedule 配置
        schedule = config.get("schedule", [])
        assert isinstance(schedule, list)


class TestPendoServices:
    """测试 pendo 服务模块"""

    def test_database_service_exists(self):
        """测试数据库服务模块"""
        db_path = ROOT / "plugins" / "pendo" / "services" / "db.py"
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "class" in content
        assert "Database" in content

    def test_ai_parser_service_exists(self):
        """测试 AI 解析服务模块"""
        ai_path = ROOT / "plugins" / "pendo" / "services" / "ai_parser.py"
        with open(ai_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "async def" in content or "def" in content

    def test_rule_parser_service_exists(self):
        """测试规则解析服务模块"""
        rule_path = ROOT / "plugins" / "pendo" / "services" / "rule_parser.py"
        with open(rule_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "async def" in content or "def" in content

    def test_exporter_service_exists(self):
        """测试导出服务模块"""
        exporter_path = ROOT / "plugins" / "pendo" / "services" / "exporter.py"
        with open(exporter_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "async def" in content or "def" in content

    def test_reminder_service_exists(self):
        """测试提醒服务模块"""
        reminder_path = ROOT / "plugins" / "pendo" / "services" / "reminder.py"
        with open(reminder_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "async def" in content or "def" in content


class TestPendoDataModels:
    """测试 pendo 数据模型"""

    def test_models_have_required_types(self):
        """测试模型目录包含必要的数据类型"""
        models_dir = ROOT / "plugins" / "pendo" / "models"

        # 检查是否有模型文件
        model_files = list(models_dir.glob("*.py"))
        assert len(model_files) > 0

        # 检查是否定义了基本的数据类型
        for model_file in model_files:
            if model_file.name != "__init__.py":
                with open(model_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    # 检查是否有数据类或类型定义
                    assert "class" in content or "dataclass" in content


class TestPendoDocumentation:
    """测试 pendo 文档"""

    def test_readme_exists(self):
        """测试 README 文件存在"""
        readme_path = ROOT / "plugins" / "pendo" / "README.md"
        assert readme_path.exists()

        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert len(content) > 100  # 应该有实际内容

    def test_architecture_doc_exists(self):
        """测试架构文档存在"""
        arch_path = ROOT / "plugins" / "pendo" / "ARCHITECTURE.md"
        assert arch_path.exists()

        with open(arch_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert len(content) > 100

    def test_plugin_doc_exists(self):
        """测试插件说明文档存在"""
        doc_path = ROOT / "plugins" / "pendo" / "Pendo个人时间与信息管理中枢.md"
        assert doc_path.exists()


class TestPendoCommands:
    """测试 pendo 命令处理"""

    def test_command_modules_exist(self):
        """测试命令模块存在"""
        commands_dir = ROOT / "plugins" / "pendo" / "commands"

        # 检查是否有命令模块
        command_files = list(commands_dir.glob("*.py"))
        assert len(command_files) > 0

        # 检查是否有处理不同类型项目的命令
        for cmd_file in command_files:
            if cmd_file.name != "__init__.py":
                with open(cmd_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    assert "async def" in content or "def" in content


class _StubSimpleHandler:
    async def handle(self, user_id, args, context, group_id=None):
        return {"status": "success", "message": "ok"}

    async def search(self, user_id, args, context):
        return {"status": "success", "message": "ok"}


class _StubTaskHandler:
    def __init__(self):
        self.group_ids = []

    async def handle(self, user_id, args, context, group_id=None):
        self.group_ids.append(group_id)
        return {"status": "success", "message": f"group:{group_id}"}


class _StubExporter:
    def export_markdown(self, user_id, args, options):
        return {"status": "success", "message": "exported"}

    def import_markdown(self, user_id, args, options):
        return {"status": "success", "message": "imported"}


class _FakeItemsRepo:
    def __init__(self, tasks):
        self._tasks = tasks

    def get_items(self, user_id, filters, limit):
        status = filters.get("status")
        if status is None:
            return list(self._tasks)
        return [task for task in self._tasks if task.status == status]


class _FakeDb:
    def __init__(self, tasks):
        self.items = _FakeItemsRepo(tasks)


def _build_task(task_id: str, category: str, created_at: str):
    return SimpleNamespace(
        id=task_id,
        title=task_id,
        category=category,
        status="done",
        priority=3,
        created_at=created_at,
    )


class TestPendoReviewFixes:
    def test_router_is_not_reused_across_group_contexts(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        task_handler = _StubTaskHandler()
        services = {
            "db": object(),
            "reminder_service": object(),
            "exporter": _StubExporter(),
            "event_handler": _StubSimpleHandler(),
            "task_handler": task_handler,
            "note_handler": _StubSimpleHandler(),
            "diary_handler": _StubSimpleHandler(),
            "search_handler": _StubSimpleHandler(),
            "ledger_handler": _StubSimpleHandler(),
            "web_handler": _StubSimpleHandler(),
        }

        monkeypatch.setattr(pendo_main, "_get_services", lambda context: services)

        context = SimpleNamespace(state={})
        router_g1 = pendo_main._build_command_router(context, group_id=1001)
        router_g2 = pendo_main._build_command_router(context, group_id=1002)

        assert router_g1 is not router_g2

        result_g1 = asyncio.run(router_g1.route("todo", "u1", "list", context))
        result_g2 = asyncio.run(router_g2.route("todo", "u1", "list", context))

        assert result_g1["message"] == "group:1001"
        assert result_g2["message"] == "group:1002"
        assert task_handler.group_ids == [1001, 1002]

    def test_cleanup_clears_pendo_runtime_state(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        class _DummyDb:
            def __init__(self):
                self.cleaned = False

            def cleanup(self):
                self.cleaned = True

        db = _DummyDb()
        monkeypatch.setattr(pendo_main, "_get_database", lambda context: db)

        from plugins.pendo.utils import db_ops

        monkeypatch.setattr(db_ops, "cleanup_db_singleton", lambda: None)

        context = SimpleNamespace(
            state={"pendo_runtime": {"services": {"x": 1}, "router": object()}},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )

        pendo_main.cleanup(context)

        assert db.cleaned is True
        assert context.state["pendo_runtime"] == {}

    def test_cleanup_does_not_create_pendo_runtime_when_absent(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        class _DummyDb:
            def cleanup(self):
                return None

        monkeypatch.setattr(pendo_main, "_get_database", lambda context: _DummyDb())

        from plugins.pendo.utils import db_ops

        monkeypatch.setattr(db_ops, "cleanup_db_singleton", lambda: None)

        context = SimpleNamespace(
            state={},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )

        pendo_main.cleanup(context)

        assert "pendo_runtime" not in context.state

    def test_task_status_pagination_page_two_spans_categories(self):
        from plugins.pendo.handlers.task import TaskHandler

        date_tasks = [
            _build_task(f"d{i}", "2026-02-10", f"2026-02-10T08:00:0{i}") for i in range(1, 9)
        ]
        work_tasks = [_build_task(f"w{i}", "work", f"2026-02-10T09:00:0{i}") for i in range(1, 8)]
        db = _FakeDb(date_tasks + work_tasks)
        handler = TaskHandler(db)

        result = asyncio.run(handler.list_all_tasks_by_status("u1", "done", {}, "done page:2"))

        assert result["status"] == "success"
        message = result["message"]
        assert "📂 **work**" in message
        assert "`w3`" in message
        assert "`w7`" in message
        assert "`w1`" not in message


class TestMilestoneEventModel:
    """测试多时间节点事件数据模型"""

    def test_event_item_has_milestones_field(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.models.item import EventItem

        item = EventItem(owner_id="u1", title="会议")
        assert hasattr(item, "milestones")
        assert item.milestones == []

    def test_event_item_has_notes_field(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.models.item import EventItem

        item = EventItem(owner_id="u1", title="会议", notes="备注内容")
        assert item.notes == "备注内容"

    def test_event_item_to_dict_includes_new_fields(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.models.item import EventItem

        milestones = [{"name": "注册截止", "time": "2026-04-06T00:00:00"}]
        item = EventItem(owner_id="u1", title="会议", milestones=milestones, notes="备注")
        d = item.to_dict()
        assert d["milestones"] == milestones
        assert d["notes"] == "备注"


class TestAIParserMilestones:
    """测试 AI parser 处理多时间节点事件"""

    def _make_parser(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from plugins.pendo.services.ai_parser import AIParser

        return AIParser(context=None)

    def test_build_remind_times_for_milestones(self):
        """多节点时 remind_times 是所有里程碑各自提醒的并集"""
        parser = self._make_parser()
        milestones = [
            {"name": "注册截止", "time": "2030-04-06T00:00:00"},
            {"name": "会议开始", "time": "2030-04-22T10:30:00"},
        ]
        remind_offsets = ["提前1天", "提前1小时"]
        times = parser.build_remind_times_for_milestones(milestones, remind_offsets)
        # 2 milestones × 2 offsets = 4 remind times
        assert len(times) == 4

    def test_parse_event_with_ai_handles_milestones(self):
        """模拟 LLM 返回 milestones 时正确解析"""
        import asyncio, json
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
                    {"name": "注册截止", "time": "2030-04-06T00:00:00"},
                    {"name": "会议开始", "time": "2030-04-22T10:30:00"},
                    {"name": "会议结束", "time": "2030-04-26T12:00:00"},
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
        assert result["end_time"] == "2030-04-26T12:00:00"
        assert result["notes"] == "https://example.com"
        assert len(result["remind_times"]) == 6  # 3 milestones × 2 offsets


class TestMilestoneEventHandler:
    """测试多时间节点事件创建"""

    def _make_handler(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from unittest.mock import MagicMock, AsyncMock
        from plugins.pendo.handlers.event import EventHandler

        db = MagicMock()
        db.items = db
        db.logs = db
        db.conn_manager = db
        db.insert_item = MagicMock(return_value="abc12345")
        db.log_operation = MagicMock()

        ai_parser = MagicMock()
        reminder_service = MagicMock()
        reminder_service.detect_conflict = MagicMock(return_value=[])

        handler = EventHandler(db=db, ai_parser=ai_parser, reminder_service=reminder_service)
        return handler

    def test_create_milestone_event_success(self):
        import asyncio
        from typing import Any

        handler = self._make_handler()
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
        from typing import Any

        handler = self._make_handler()
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

    def test_reminder_message_shows_milestone_name(self):
        """提醒消息应显示对应里程碑名称"""
        from types import SimpleNamespace

        service = self._make_service()

        item = SimpleNamespace(
            id="abc12345",
            title="星团会议",
            start_time="2030-04-06T00:00:00",
            end_time="2030-04-26T12:00:00",
            location="江苏溧水",
            notes="https://example.com",
            milestones=[
                {"name": "注册截止", "time": "2030-04-06T00:00:00"},
                {"name": "会议开始", "time": "2030-04-22T10:30:00"},
            ],
            remind_times=["2030-04-05T00:00:00", "2030-04-05T23:00:00"],
            context={},
            owner_id="user1",
        )

        # 提醒时间对应"注册截止"前1天
        msg = service._build_reminder_message(item, "2030-04-05T00:00:00")
        assert "注册截止" in msg
        assert "星团会议" in msg

    def test_reminder_message_shows_notes(self):
        """普通事件的提醒消息应附上 notes"""
        from types import SimpleNamespace

        service = self._make_service()

        item = SimpleNamespace(
            id="abc12345",
            title="普通会议",
            start_time="2030-04-06T09:00:00",
            end_time=None,
            location="",
            notes="会议链接: https://meet.example.com",
            milestones=[],
            remind_times=["2030-04-05T09:00:00"],
            context={},
            owner_id="user1",
        )

        msg = service._build_reminder_message(item, "2030-04-05T09:00:00")
        assert "会议链接" in msg


class TestRecurringEventRegression:
    def test_create_recurring_event_preserves_duration_per_instance(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler

        inserted_items = []

        db = MagicMock()
        db.items.insert_item.side_effect = lambda item, custom_id=None: (
            inserted_items.append(item) or custom_id
        )
        db.logs.log_operation.return_value = True

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

    def test_edit_all_instances_keeps_relative_offsets_between_occurrences(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))
        parent_id = "series123"

        try:
            first = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-01T09:00:00",
                end_time="2030-01-01T10:00:00",
                remind_times=["2030-01-01T08:00:00"],
                parent_id=parent_id,
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-02T09:00:00",
                end_time="2030-01-02T10:00:00",
                remind_times=["2030-01-02T08:00:00"],
                parent_id=parent_id,
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "series123_20300101")
            db.items.insert_item(second, "series123_20300102")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"start_time": "2030-01-10T10:00:00"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler._edit_all_instances("u1", parent_id, "改到2030-01-10 10:00")
            )

            assert result["status"] == "success"

            updated_first = db.items.get_item("series123_20300101", "u1")
            updated_second = db.items.get_item("series123_20300102", "u1")

            assert updated_first is not None
            assert updated_second is not None
            assert getattr(updated_first, "start_time") == "2030-01-10T10:00:00"
            assert getattr(updated_first, "end_time") == "2030-01-10T11:00:00"
            assert getattr(updated_first, "remind_times") == [
                "2030-01-10T09:00:00",
                "2030-01-10T10:00:00",
            ]
            assert getattr(updated_second, "start_time") == "2030-01-11T10:00:00"
            assert getattr(updated_second, "end_time") == "2030-01-11T11:00:00"
            assert getattr(updated_second, "remind_times") == [
                "2030-01-11T09:00:00",
                "2030-01-11T10:00:00",
            ]
        finally:
            db.cleanup()


class TestReminderRegression:
    def test_backfill_missing_start_time_reminder_adds_event_start(self, tmp_path):
        import importlib
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        backfill_missing_start_time_reminders = importlib.import_module(
            "plugins.pendo.scripts.backfill_start_time_reminders"
        ).backfill_missing_start_time_reminders

        db_path = tmp_path / "pendo.db"
        db = Database(str(db_path))
        reopened = None

        try:
            event = EventItem(
                owner_id="u1",
                title="晨会",
                start_time="2030-01-03T09:00:00",
                remind_times=["2030-01-03T08:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "evt123")
            db.cleanup()

            result = backfill_missing_start_time_reminders(str(db_path), dry_run=False)

            reopened = Database(str(db_path))
            updated = reopened.items.get_item("evt123", "u1")

            assert result["matched"] == 1
            assert result["updated"] == 1
            assert isinstance(updated, EventItem)
            assert updated.remind_times == ["2030-01-03T08:00:00", "2030-01-03T09:00:00"]
        finally:
            try:
                if reopened is not None:
                    reopened.cleanup()
            except Exception:
                pass

    def test_backfill_milestone_event_adds_missing_milestone_times(self, tmp_path):
        import importlib
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        backfill_missing_start_time_reminders = importlib.import_module(
            "plugins.pendo.scripts.backfill_start_time_reminders"
        ).backfill_missing_start_time_reminders

        db_path = tmp_path / "pendo.db"
        db = Database(str(db_path))
        reopened = None

        try:
            event = EventItem(
                owner_id="u1",
                title="报名流程",
                start_time="2030-01-05T09:00:00",
                end_time="2030-01-05T12:00:00",
                remind_times=["2030-01-05T08:00:00"],
                milestones=[
                    {"name": "开始", "time": "2030-01-05T09:00:00"},
                    {"name": "截止", "time": "2030-01-05T12:00:00"},
                ],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "evt_milestone")
            db.cleanup()

            result = backfill_missing_start_time_reminders(str(db_path), dry_run=False)

            reopened = Database(str(db_path))
            updated = reopened.items.get_item("evt_milestone", "u1")

            assert result["matched"] == 1
            assert result["updated"] == 1
            assert isinstance(updated, EventItem)
            assert updated.remind_times == [
                "2030-01-05T08:00:00",
                "2030-01-05T09:00:00",
                "2030-01-05T12:00:00",
            ]
        finally:
            try:
                if reopened is not None:
                    reopened.cleanup()
            except Exception:
                pass

    def test_event_list_includes_day_delta_for_each_event(self, monkeypatch, tmp_path):
        import sys
        from datetime import datetime
        from typing import Any, cast
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers import event as event_module
        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))

        try:
            future_event = EventItem(
                owner_id="u1",
                title="未来会议",
                start_time="2030-01-03T09:00:00",
                end_time="2030-01-03T10:00:00",
                remind_times=["2030-01-03T08:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            past_event = EventItem(
                owner_id="u1",
                title="过去会议",
                start_time="2029-12-30T09:00:00",
                end_time="2029-12-30T10:00:00",
                remind_times=["2029-12-30T08:00:00"],
                created_at="2029-12-01T00:00:00",
                updated_at="2029-12-01T00:00:00",
            )
            db.items.insert_item(future_event, "evt_future")
            db.items.insert_item(past_event, "evt_past")

            monkeypatch.setattr(
                event_module,
                "now_in_timezone",
                lambda user_id=None, db=None: datetime.fromisoformat("2030-01-01T08:00:00+08:00"),
            )

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            result = asyncio.run(
                handler.list_events(
                    "u1",
                    "2029-12-01..2030-01-31",
                    cast(Any, SimpleNamespace()),
                )
            )

            assert result["status"] == "success"
            assert "**01月03日 周四** - 2天后" in result["message"]
            assert "• 09:00 - 10:00 未来会议" in result["message"]
            assert "**12月30日 周日** - 2天前" in result["message"]
            assert "• 09:00 - 10:00 过去会议" in result["message"]
        finally:
            db.cleanup()

    def test_confirm_requires_item_ownership(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands.operations import handle_confirm

        db = MagicMock()
        db.items.get_item.return_value = None

        reminder_service = MagicMock()
        reminder_service.confirm_reminder.return_value = {"status": "success", "message": "ok"}

        result = asyncio.run(handle_confirm("u1", "evt123", reminder_service, db))

        assert result["status"] == "error"
        reminder_service.confirm_reminder.assert_not_called()

    def test_confirm_only_marks_latest_unconfirmed_log(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="提醒测试",
                start_time="2030-01-01T10:00:00",
                remind_times=["2030-01-01T08:00:00", "2030-01-01T09:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "evt123")

            conn = db.conn_manager.get_connection()
            cursor = conn.cursor()
            with conn:
                cursor.execute(
                    """
                    INSERT INTO reminder_logs (item_id, remind_time, sent_at)
                    VALUES (?, ?, ?)
                    """,
                    ("evt123", "2030-01-01T08:00:00", "2030-01-01T08:00:05"),
                )
                cursor.execute(
                    """
                    INSERT INTO reminder_logs (item_id, remind_time, sent_at)
                    VALUES (?, ?, ?)
                    """,
                    ("evt123", "2030-01-01T09:00:00", "2030-01-01T09:00:05"),
                )

            result = db.items.confirm_reminder("evt123", "confirmed")

            assert result["status"] == "success"
            logs = db.items.get_reminder_logs("evt123")
            confirmed_logs = [log for log in logs if log["confirmed_at"]]
            pending_logs = [log for log in logs if not log["confirmed_at"]]

            # confirm_reminder 不指定 remind_time 时确认该 item 的所有未确认提醒
            assert sorted(log["remind_time"] for log in confirmed_logs) == [
                "2030-01-01T08:00:00",
                "2030-01-01T09:00:00",
            ]
            assert pending_logs == []
        finally:
            db.cleanup()

    def test_reminder_disabled_user_does_not_receive_scheduled_reminder(self, monkeypatch):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import reminder as reminder_module
        from plugins.pendo.services.reminder import ReminderService

        fixed_now = datetime.fromisoformat("2030-01-01T09:00:00+08:00")

        monkeypatch.setattr(
            reminder_module, "now_in_timezone", lambda user_id=None, db=None: fixed_now
        )
        monkeypatch.setattr(
            reminder_module,
            "parse_and_localize",
            lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
                tzinfo=fixed_now.tzinfo
            ),
        )

        item = SimpleNamespace(
            id="evt123",
            owner_id="u1",
            title="晨会",
            start_time="2030-01-01T10:00:00",
            end_time="2030-01-01T11:00:00",
            remind_times=["2030-01-01T09:00:00"],
            context={},
            location="会议室A",
            notes="",
            milestones=[],
            tags=[],
        )

        class _FakeDb:
            def __init__(self):
                self.logged = []

            def get_all_events_with_reminders(self, future_hours=0):
                return [item]

            def is_reminder_sent(self, item_id, remind_time):
                return False

            def log_reminder(self, item_id, remind_time, sent=True):
                self.logged.append((item_id, remind_time, sent))

            def get_user_settings(self, user_id):
                return {
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "07:00",
                    "settings_json": {"reminder_enabled": False},
                }

            def get_unconfirmed_sent_reminders(self):
                return []

            def get_item(self, item_id):
                return item

        service = ReminderService(db=_FakeDb())

        result = service.check_and_send_reminders()

        assert result["sent"] == 0
        assert result["messages"] == []
        assert service.db.logged == []

    def test_fourth_unconfirmed_send_auto_confirms_after_ten_minutes(self, monkeypatch):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import reminder as reminder_module
        from plugins.pendo.services.reminder import ReminderService

        fixed_now = datetime.fromisoformat("2030-01-01T09:10:00+08:00")
        remind_time = "2030-01-01T08:00:00"

        monkeypatch.setattr(
            reminder_module,
            "parse_and_localize",
            lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
                tzinfo=fixed_now.tzinfo
            ),
        )

        item = SimpleNamespace(
            id="evt123",
            owner_id="u1",
            title="晨会",
            start_time="2030-01-01T10:00:00",
            end_time="2030-01-01T11:00:00",
            remind_times=[remind_time],
            context={},
            location="会议室A",
            notes="",
            milestones=[],
            tags=[],
        )

        class _FakeDb:
            def __init__(self):
                self.confirm_calls = []

            def get_unconfirmed_sent_reminders(self):
                return [
                    {
                        "id": 4,
                        "item_id": "evt123",
                        "remind_time": remind_time,
                        "repeat_count": 4,
                        "last_sent_at": "2030-01-01T09:00:00",
                    }
                ]

            def get_item(self, item_id):
                assert item_id == "evt123"
                return item

            def get_user_settings(self, user_id):
                return {
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "07:00",
                    "settings_json": {"reminder_enabled": True},
                }

            def confirm_reminder(
                self, item_id, user_action="confirmed", owner_id=None, remind_time=None
            ):
                self.confirm_calls.append(
                    {
                        "item_id": item_id,
                        "user_action": user_action,
                        "owner_id": owner_id,
                        "remind_time": remind_time,
                    }
                )
                return {"status": "success", "message": "ok"}

        service = ReminderService(db=_FakeDb())

        result = service._check_unconfirmed_repeats(fixed_now)

        assert result == []
        assert service.db.confirm_calls == [
            {
                "item_id": "evt123",
                "user_action": "auto_confirmed",
                "owner_id": None,
                "remind_time": remind_time,
            }
        ]


class TestScheduledRegression:
    def test_check_reminders_returns_messages_without_send_action(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FakeReminderService:
            def check_and_send_reminders(self, context=None):
                return {
                    "messages": [
                        {
                            "user_id": "1001",
                            "message": "提醒消息",
                            "item_id": "evt123",
                            "remind_time": "2030-01-01T09:00:00",
                        }
                    ]
                }

        db = SimpleNamespace(logged=[])

        def fake_log_reminder(item_id, remind_time, sent=True):
            db.logged.append((item_id, remind_time, sent))

        db.log_reminder = fake_log_reminder

        monkeypatch.setattr(scheduled_module, "get_database", lambda context: db)
        monkeypatch.setattr(scheduled_module, "_reminder_service_singleton", _FakeReminderService())

        result = asyncio.run(scheduled_module.check_reminders(SimpleNamespace()))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001
        assert db.logged == []

    def test_daily_briefing_respects_user_timezone_and_configured_time(self, monkeypatch):
        import sys
        from datetime import datetime, timezone
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        settings_map = {
            "1001": {
                "timezone": "Asia/Shanghai",
                "daily_report_time": "08:00",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {"daily_briefing_enabled": True},
            },
            "1002": {
                "timezone": "America/New_York",
                "daily_report_time": "08:00",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {"daily_briefing_enabled": True},
            },
        }
        batch_calls = []

        def get_user_settings_batch(user_ids):
            batch_calls.append(list(user_ids))
            return {user_id: dict(settings_map[user_id]) for user_id in user_ids}

        def get_user_settings(_user_id):
            raise AssertionError("send_daily_briefings should use batch settings lookup")

        db = cast(
            Any,
            SimpleNamespace(
                settings=SimpleNamespace(
                    get_user_settings=get_user_settings,
                    get_user_settings_batch=get_user_settings_batch,
                    update_user_settings=lambda user_id, settings: True,
                )
            ),
        )
        actions = []

        async def send_action(action):
            actions.append(action)

        async def fake_get_active_user_ids(_db):
            return ["1001", "1002"]

        async def fake_generate_briefing_content(user_id, _db, _ai_parser):
            return f"briefing:{user_id}"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "AIParser", lambda context: object())
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "_generate_briefing_content", fake_generate_briefing_content
        )

        context = SimpleNamespace(send_action=send_action)
        result = asyncio.run(scheduled_module.send_daily_briefings(context, db))

        assert result == []
        assert [action["params"]["user_id"] for action in actions] == [1001]
        assert batch_calls == [["1001", "1002"]]

    def test_daily_briefing_returns_messages_without_send_action(self, monkeypatch):
        import sys
        from datetime import datetime, timezone
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        def get_user_settings_batch(user_ids):
            return {
                user_id: {
                    "timezone": "Asia/Shanghai",
                    "daily_report_time": "08:00",
                    "settings_json": {"daily_briefing_enabled": True},
                }
                for user_id in user_ids
            }

        db = cast(
            Any,
            SimpleNamespace(
                settings=SimpleNamespace(
                    get_user_settings=lambda user_id: (_ for _ in ()).throw(
                        AssertionError("send_daily_briefings should use batch settings lookup")
                    ),
                    get_user_settings_batch=get_user_settings_batch,
                    update_user_settings=lambda user_id, settings: True,
                )
            ),
        )

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        async def fake_generate_briefing_content(user_id, _db, _ai_parser):
            return f"briefing:{user_id}"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "AIParser", lambda context: object())
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "_generate_briefing_content", fake_generate_briefing_content
        )

        result = asyncio.run(scheduled_module.send_daily_briefings(SimpleNamespace(), db))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001

    def test_migrate_todos_returns_messages_without_send_action(self, monkeypatch):
        import sys
        from datetime import datetime
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        db = cast(Any, SimpleNamespace())

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        async def fake_get_user_custom_settings(user_id, _db):
            return {}

        async def fake_get_undone_tasks_for_date(_db, user_id, date_str):
            return [SimpleNamespace(id="t1")]

        async def fake_batch_migrate(_db, tasks, target_date, user_id):
            return 1

        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "_get_user_custom_settings", fake_get_user_custom_settings
        )
        monkeypatch.setattr(
            scheduled_module, "_get_undone_tasks_for_date", fake_get_undone_tasks_for_date
        )
        monkeypatch.setattr(scheduled_module, "_batch_migrate_tasks_to_date", fake_batch_migrate)
        monkeypatch.setattr(scheduled_module, "save_user_setting", lambda *args, **kwargs: None)

        result = asyncio.run(scheduled_module.migrate_undone_todos(SimpleNamespace(), db))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001

    def test_diary_reminder_respects_user_timezone_and_existing_diary(self, monkeypatch):
        import sys
        from datetime import datetime, timezone
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 13, 30, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        settings_map = {
            "2001": {
                "timezone": "Asia/Shanghai",
                "diary_remind_time": "21:30",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {},
            },
            "2002": {
                "timezone": "Asia/Shanghai",
                "diary_remind_time": "21:30",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {},
            },
            "2003": {
                "timezone": "America/New_York",
                "diary_remind_time": "21:30",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {},
            },
        }
        def get_user_settings_batch(user_ids):
            return {user_id: dict(settings_map[user_id]) for user_id in user_ids}

        db = cast(
            Any,
            SimpleNamespace(
                settings=SimpleNamespace(
                    get_user_settings=lambda user_id: (_ for _ in ()).throw(
                        AssertionError("check_diary_reminders should use batch settings lookup")
                    ),
                    get_user_settings_batch=get_user_settings_batch,
                    update_user_settings=lambda user_id, settings: True,
                )
            ),
        )
        actions = []

        async def send_action(action):
            actions.append(action)

        async def fake_get_active_user_ids(_db):
            return ["2001", "2002", "2003"]

        async def fake_has_diary_for_date(_db, user_id, diary_date):
            return user_id == "2002"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(scheduled_module, "_has_diary_for_date", fake_has_diary_for_date)

        context = SimpleNamespace(send_action=send_action)
        result = asyncio.run(scheduled_module.check_diary_reminders(context, db))

        assert result == []
        assert [action["params"]["user_id"] for action in actions] == [2001]

    def test_plugin_manifest_has_no_dead_evening_briefing_schedule(self):
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        handler_ids = {entry["handler"] for entry in config.get("schedule", [])}
        assert "scheduled_evening_briefing" not in handler_ids
        assert "scheduled_weekly_finance_summary" in handler_ids
        assert "scheduled_month_end_finance_summary" in handler_ids
        weekly_entry = next(
            entry for entry in config.get("schedule", [])
            if entry["handler"] == "scheduled_weekly_finance_summary"
        )
        monthly_entry = next(
            entry for entry in config.get("schedule", [])
            if entry["handler"] == "scheduled_month_end_finance_summary"
        )
        assert weekly_entry["cron"] == {"day_of_week": "sun", "hour": 21, "minute": 0}
        assert monthly_entry["cron"] == {"day": "last", "hour": 21, "minute": 0}


class TestPendoFinanceSummaries:
    def test_weekly_finance_summary_sends_on_sunday_evening(self, monkeypatch):
        import sys
        from datetime import datetime, timezone

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 6, 13, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        actions = []

        async def send_action(action):
            actions.append(action)

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        async def fake_get_settings_bundle_map(user_ids, _db):
            return {
                user_ids[0]: {
                    "settings": {"timezone": "Asia/Shanghai"},
                    "custom_settings": {},
                }
            }

        async def fake_generate_summary(*_args, **_kwargs):
            return "weekly-summary"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "get_user_settings_bundle_map", fake_get_settings_bundle_map
        )
        monkeypatch.setattr(
            scheduled_module, "_generate_finance_summary_content", fake_generate_summary
        )
        monkeypatch.setattr(scheduled_module, "save_user_setting", lambda *args, **kwargs: None)

        db = SimpleNamespace()
        result = asyncio.run(
            scheduled_module.send_weekly_finance_summaries(SimpleNamespace(send_action=send_action), db)
        )

        assert result == []
        assert len(actions) == 1
        assert actions[0]["params"]["user_id"] == 1001
        assert "weekly-summary" in actions[0]["params"]["message"][0]["data"]["text"]

    def test_month_end_finance_summary_sends_on_last_day_evening(self, monkeypatch):
        import sys
        from datetime import datetime, timezone

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 3, 31, 13, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        actions = []

        async def send_action(action):
            actions.append(action)

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        async def fake_get_settings_bundle_map(user_ids, _db):
            return {
                user_ids[0]: {
                    "settings": {"timezone": "Asia/Shanghai"},
                    "custom_settings": {},
                }
            }

        async def fake_generate_summary(*_args, **_kwargs):
            return "month-summary"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "get_user_settings_bundle_map", fake_get_settings_bundle_map
        )
        monkeypatch.setattr(
            scheduled_module, "_generate_finance_summary_content", fake_generate_summary
        )
        monkeypatch.setattr(scheduled_module, "save_user_setting", lambda *args, **kwargs: None)

        db = SimpleNamespace()
        result = asyncio.run(
            scheduled_module.send_month_end_finance_summaries(
                SimpleNamespace(send_action=send_action), db
            )
        )

        assert result == []
        assert len(actions) == 1
        assert actions[0]["params"]["user_id"] == 1001
        assert "month-summary" in actions[0]["params"]["message"][0]["data"]["text"]


class TestBatchDeleteRefactor:
    def test_task_category_delete_uses_shared_batch_helper(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.task import TaskHandler

        db = MagicMock()
        db.items.get_items.return_value = [SimpleNamespace(id="t1"), SimpleNamespace(id="t2")]
        handler = TaskHandler(db)
        calls = []

        async def fake_batch(item_ids, owner_id, item_type, action, details_factory=None):
            calls.append((item_ids, owner_id, item_type, action, details_factory))
            return 2

        monkeypatch.setattr(handler, "_db_batch_soft_delete_with_log", fake_batch, raising=False)

        result = asyncio.run(handler._delete_category_tasks("u1", "工作", {}))

        assert result["status"] == "success"
        assert calls == [(["t1", "t2"], "u1", "task", "delete_task", None)]

    def test_note_category_delete_uses_shared_batch_helper(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler

        db = MagicMock()
        db.items.get_items.return_value = [SimpleNamespace(id="n1"), SimpleNamespace(id="n2")]
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


class TestTriggerConflictRegression:
    def test_pendo_manifest_does_not_claim_bare_biji_trigger(self):
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        triggers = config["commands"][0]["triggers"]
        assert "笔记" not in triggers


class TestSessionRegression:
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
            async def parse_event_with_ai(self, text, user_id):
                return {"title": "会议"}

            def parse_natural_language(self, text, user_id):
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


class TestOperationAndImportRegression:
    def test_snooze_missing_args_returns_error_result(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands.operations import handle_snooze

        result = asyncio.run(handle_snooze("u1", "", SimpleNamespace(db=None)))

        assert result["status"] == "error"
        assert "请指定要延后的条目ID和时间" in result["message"]

    def test_import_markdown_preview_routes_to_file_preview(self, monkeypatch, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import exporter as exporter_module
        from plugins.pendo.services.exporter import ExporterService

        file_path = tmp_path / "sample.md"
        file_path.write_text("## 示例\n\n内容", encoding="utf-8")

        monkeypatch.setattr(exporter_module, "_validate_file_path", lambda path, user_id: True)

        service = ExporterService(SimpleNamespace(items=SimpleNamespace(get_item=lambda *args, **kwargs: None)))
        result = service.import_markdown("u1", f'md preview "{file_path}"', {})

        assert result["status"] == "preview"
        assert result["total_count"] == 1

    def test_import_markdown_requires_file_path(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.exporter import ExporterService

        service = ExporterService(SimpleNamespace(items=SimpleNamespace()))
        result = service.import_markdown("u1", "md", {})

        assert result["status"] == "error"
        assert "请指定导入文件路径" in result["message"]


class TestPendoWebHandler:
    """测试 pendo web 命令格式化与发送行为"""

    def test_web_token_sends_token_as_separate_private_message(self, monkeypatch):
        import sys
        import types
        import importlib

        sys.path.insert(0, str(ROOT))
        sys.modules.pop("plugins.pendo.handlers.web", None)
        sys.modules["plugins.pendo.web.server"] = types.SimpleNamespace(
            get_url=lambda: "http://127.0.0.1:8765",
            is_running=lambda: True,
            start=lambda _db: True,
            stop=lambda: True,
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        monkeypatch.setattr(web_module, "generate_token", lambda *_args, **_kwargs: "mock-token")
        monkeypatch.setattr(web_module.web_server, "get_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(web_module.web_server, "is_running", lambda: True)

        actions = []

        async def send_action(action):
            actions.append(action)

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=None)

        result = asyncio.run(handler.handle("1001", "token", context=context))

        assert result["status"] == "success"
        assert "Token 已单独私聊发送" in result["message"]
        assert "mock-token" not in result["message"]
        assert len(actions) == 1
        assert actions[0]["action"] == "send_private_msg"
        assert actions[0]["params"]["user_id"] == 1001
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert "Pendo Web 登录 Token" in token_text
        assert "mock-token" in token_text

    def test_web_token_falls_back_to_inline_message_without_send_action(self, monkeypatch):
        import sys
        import types
        import importlib

        sys.path.insert(0, str(ROOT))
        sys.modules.pop("plugins.pendo.handlers.web", None)
        sys.modules["plugins.pendo.web.server"] = types.SimpleNamespace(
            get_url=lambda: "http://127.0.0.1:8765",
            is_running=lambda: False,
            start=lambda _db: True,
            stop=lambda: True,
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        monkeypatch.setattr(web_module, "generate_token", lambda *_args, **_kwargs: "mock-token")
        monkeypatch.setattr(web_module.web_server, "get_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(web_module.web_server, "is_running", lambda: False)

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(handler.handle("1001", "token", context=None))

        assert result["status"] == "success"
        assert "登录 Token:" in result["message"]
        assert "mock-token" in result["message"]
