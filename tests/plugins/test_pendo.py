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

    def test_plugin_help_mentions_widget_token(self):
        """测试插件摘要帮助包含 widget-token 提示"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        commands = config.get("commands", [])
        assert commands
        assert "widget-token" in commands[0].get("help", "")

    def test_plugin_has_schedule(self):
        """测试插件有定时任务配置"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # 检查是否有 schedule 配置
        schedule = config.get("schedule", [])
        assert isinstance(schedule, list)

    def test_show_help_uses_navigation_and_section_dividers(self):
        """测试完整帮助使用更明显的导航和分节样式"""
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.main import _show_help

        help_text = _show_help()

        assert "🧭 **模块导航**" in help_text
        assert "━━ ⚡ **快速记录**" in help_text
        assert "━━ 🗓️ **日程管理 (Event)**" in help_text
        assert "📎 例如:" in help_text

    def test_show_help_for_subcommand_only_renders_requested_section(self):
        """测试子模块帮助只渲染对应模块并保留顶部导航提示"""
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.main import _show_help

        help_text = _show_help("event")

        assert "🧭 输入 `/pendo` 查看完整总览" in help_text
        assert "━━ 🗓️ **日程管理 (Event)**" in help_text
        assert "多节点事件可直接写“节点名 + 改成/改到 + 新时间”" in help_text
        assert "/pendo event edit 80efbef6 会议开始改成4月22日12:43" in help_text
        assert "━━ ✅ **待办事项 (Todo)**" not in help_text


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


class _StubCaptureHandler:
    def __init__(self):
        self.calls = []

    async def handle(self, user_id, args, context, group_id=None):
        self.calls.append(
            {"user_id": user_id, "args": args, "context": context, "group_id": group_id}
        )
        return {"status": "success", "message": args}


class _StubExporter:
    def export_markdown(self, user_id, args, options):
        return {
            "status": "success",
            "message": "exported",
            "file_path": "C:/tmp/pendo-export.md",
            "file_name": "pendo-export.md",
        }


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

    def test_import_command_is_removed_from_router(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        services = {
            "db": object(),
            "reminder_service": object(),
            "exporter": _StubExporter(),
            "event_handler": _StubSimpleHandler(),
            "task_handler": _StubSimpleHandler(),
            "note_handler": _StubSimpleHandler(),
            "diary_handler": _StubSimpleHandler(),
            "search_handler": _StubSimpleHandler(),
            "ledger_handler": _StubSimpleHandler(),
            "web_handler": _StubSimpleHandler(),
        }

        monkeypatch.setattr(pendo_main, "_get_services", lambda context: services)

        router = pendo_main._build_command_router(SimpleNamespace(state={}))

        assert "import" not in router.commands

    def test_handle_command_routing_preserves_multiline_note_body(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        note_handler = _StubCaptureHandler()
        services = {
            "db": object(),
            "reminder_service": object(),
            "exporter": _StubExporter(),
            "event_handler": _StubSimpleHandler(),
            "task_handler": _StubSimpleHandler(),
            "note_handler": note_handler,
            "diary_handler": _StubSimpleHandler(),
            "search_handler": _StubSimpleHandler(),
            "ledger_handler": _StubSimpleHandler(),
            "web_handler": _StubSimpleHandler(),
        }

        monkeypatch.setattr(pendo_main, "_get_services", lambda context: services)

        context = SimpleNamespace(state={})
        args = "note add title:AV女优排行\n1. 瀬户环奈\n2. 松本一香\ncat:其他 #av"

        result = asyncio.run(pendo_main._handle_command_routing("u1", args, context))

        assert note_handler.calls
        assert note_handler.calls[0]["args"] == "add title:AV女优排行\n1. 瀬户环奈\n2. 松本一香\ncat:其他 #av"
        assert "title:AV女优排行\n1. 瀬户环奈\n2. 松本一香\ncat:其他 #av" in result[0]["data"]["text"]

    def test_export_command_uploads_private_markdown_file(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        actions = []

        async def send_action(action):
            actions.append(action)

        services = {
            "db": object(),
            "reminder_service": object(),
            "exporter": _StubExporter(),
            "event_handler": _StubSimpleHandler(),
            "task_handler": _StubSimpleHandler(),
            "note_handler": _StubSimpleHandler(),
            "diary_handler": _StubSimpleHandler(),
            "search_handler": _StubSimpleHandler(),
            "ledger_handler": _StubSimpleHandler(),
            "web_handler": _StubSimpleHandler(),
        }

        monkeypatch.setattr(pendo_main, "_get_services", lambda context: services)

        context = SimpleNamespace(state={}, send_action=send_action)
        router = pendo_main._build_command_router(context)
        result = asyncio.run(router.route("export", "1001", "工作档案 last30d event,todo", context))

        assert result["status"] == "success"
        assert "已通过 QQ 私聊文件发送给你" in result["message"]
        assert len(actions) == 1
        assert actions[0]["action"] == "upload_private_file"
        assert actions[0]["params"]["user_id"] == 1001
        assert actions[0]["params"]["name"] == "pendo-export.md"

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

    def test_shutdown_stops_web_and_cleans_all_pendo_databases(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        class _DummyDb:
            def __init__(self):
                self.cleaned = False

            def cleanup(self):
                self.cleaned = True

        runtime_db = _DummyDb()
        startup_db = _DummyDb()
        stopped = []

        async def fake_stop_web_server_async():
            stopped.append("web")

        monkeypatch.setattr(pendo_main, "_get_database", lambda context: runtime_db)
        monkeypatch.setattr(pendo_main, "_stop_web_server_async", fake_stop_web_server_async)
        monkeypatch.setattr(pendo_main, "_startup_db", startup_db)

        from plugins.pendo.utils import db_ops

        monkeypatch.setattr(db_ops, "cleanup_db_singleton", lambda: None)
        monkeypatch.setattr(pendo_main, "cleanup_reminder_singleton", lambda: None)

        context = SimpleNamespace(
            state={"pendo_runtime": {"services": {"x": 1}, "router": object()}},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )

        asyncio.run(pendo_main.shutdown(context))

        assert stopped == ["web"]
        assert runtime_db.cleaned is True
        assert startup_db.cleaned is True
        assert pendo_main._startup_db is None
        assert context.state["pendo_runtime"] == {}

    def test_run_scheduled_task_swallows_cancelled_error(self):
        from plugins.pendo import main as pendo_main

        metrics: list[tuple[str, float, bool]] = []
        context = SimpleNamespace()
        log_messages: list[str] = []
        log = SimpleNamespace(info=lambda msg, *args: log_messages.append(msg % args))

        async def fake_record_metric(_context, name, duration, is_error=False):
            metrics.append((name, duration, is_error))

        async def cancelled_task():
            raise asyncio.CancelledError()

        original_record_metric = pendo_main._record_metric
        pendo_main._record_metric = fake_record_metric
        try:
            result = asyncio.run(
                pendo_main._run_scheduled_task(context, "daily_briefings", cancelled_task, log)
            )
        finally:
            pendo_main._record_metric = original_record_metric

        assert result == []
        assert metrics and metrics[0][0] == "scheduled.daily_briefings"
        assert metrics[0][2] is False
        assert any("cancelled during shutdown" in message for message in log_messages)

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
        assert "节点时间: 04月06日 00:00" in msg
        assert "对应提醒点: 提前1天（04月05日 00:00）" in msg

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

        handler = EventHandler(db=MagicMock(), ai_parser=_FakeAiParser(), reminder_service=MagicMock())
        current_event = EventItem(
            owner_id="u1",
            title="FAST2026观测申请截止",
            category="工作",
            start_time="2026-03-31T14:00:00",
        )

        updates = asyncio.run(handler._parse_updates(changes, current_event))

        assert updates == {
            "start_time": "2026-04-07T14:00:00",
            "remind_times": ["2026-04-06T14:00:00", "2026-04-07T13:00:00"],
        }

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
                owner_id="u1",
                title="FAST2026观测申请截止",
                category="工作",
                start_time="2026-03-31T14:00:00",
                remind_times=[
                    "2026-03-30T14:00:00",
                    "2026-03-31T13:00:00",
                    "2026-03-31T14:00:00",
                ],
                created_at="2026-03-20T00:00:00",
                updated_at="2026-03-20T00:00:00",
            )
            db.items.insert_item(event, "evt12345")

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

            updated = db.items.get_item("evt12345", "u1")
            assert updated is not None
            assert getattr(updated, "title") == "FAST2026观测申请截止"
            assert getattr(updated, "category") == "工作"
            assert getattr(updated, "start_time") == "2026-04-07T14:00:00"
            assert getattr(updated, "remind_times") == [
                "2026-04-06T14:00:00",
                "2026-04-07T13:00:00",
                "2026-04-07T14:00:00",
            ]
        finally:
            db.cleanup()

    def test_edit_all_instances_applies_explicit_reminder_offsets(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))

        try:
            parent_id = "series123"
            first = EventItem(
                owner_id="u1",
                title="重复会议",
                category="工作",
                start_time="2030-01-01T10:00:00",
                end_time="2030-01-01T11:00:00",
                remind_times=["2030-01-01T09:00:00"],
                parent_id=parent_id,
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id="u1",
                title="重复会议",
                category="工作",
                start_time="2030-01-02T10:00:00",
                end_time="2030-01-02T11:00:00",
                remind_times=["2030-01-02T09:00:00"],
                parent_id=parent_id,
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "series123_20300101")
            db.items.insert_item(second, "series123_20300102")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {
                    "start_time": "2030-01-10T10:00:00",
                    "remind_times": ["2030-01-09T10:00:00", "2030-01-10T09:00:00"],
                }

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler._edit_all_instances(
                    "u1",
                    parent_id,
                    "改到2030-01-10 10:00，提前1天和1小时提醒",
                )
            )

            assert result["status"] == "success"

            updated_first = db.items.get_item("series123_20300101", "u1")
            updated_second = db.items.get_item("series123_20300102", "u1")

            assert updated_first is not None
            assert updated_second is not None
            assert getattr(updated_first, "remind_times") == [
                "2030-01-09T10:00:00",
                "2030-01-10T09:00:00",
                "2030-01-10T10:00:00",
            ]
            assert getattr(updated_second, "remind_times") == [
                "2030-01-10T10:00:00",
                "2030-01-11T09:00:00",
                "2030-01-11T10:00:00",
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
                owner_id="u1",
                title="元旦会议",
                start_time="2030-01-01T10:00:00",
                end_time="2030-01-01T11:00:00",
                remind_times=["2030-01-01T09:00:00"],
                created_at="2029-12-01T00:00:00",
                updated_at="2029-12-01T00:00:00",
            )
            db.items.insert_item(event, "evtday01")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.list_events("u1", "2030-01-01", MagicMock()))

            assert result["status"] == "success"
            assert "元旦会议" in result["message"]
            assert "01月01日" in result["message"]
        finally:
            db.cleanup()

    def test_batch_edit_preserves_sent_history_and_prunes_stale_unsent_logs(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_batch_logs.db"))

        try:
            parent_id = "series123"
            first = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-01T10:00:00",
                end_time="2030-01-01T11:00:00",
                remind_times=["2030-01-01T08:30:00", "2030-01-01T09:00:00", "2030-01-01T10:00:00"],
                parent_id=parent_id,
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-02T10:00:00",
                end_time="2030-01-02T11:00:00",
                remind_times=["2030-01-02T08:30:00", "2030-01-02T09:00:00", "2030-01-02T10:00:00"],
                parent_id=parent_id,
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "series123_20300101")
            db.items.insert_item(second, "series123_20300102")
            db.confirm_reminder(
                "series123_20300101",
                "preconfirmed",
                owner_id="u1",
                remind_time="2030-01-01T08:30:00",
                allow_future=True,
            )
            db.confirm_reminder(
                "series123_20300102",
                "preconfirmed",
                owner_id="u1",
                remind_time="2030-01-02T08:30:00",
                allow_future=True,
            )
            db.log_reminder("series123_20300101", "2030-01-01T09:00:00", sent=True)
            db.log_reminder("series123_20300101", "2030-01-01T10:00:00", sent=True)
            db.log_reminder("series123_20300102", "2030-01-02T09:00:00", sent=True)
            db.log_reminder("series123_20300102", "2030-01-02T10:00:00", sent=True)

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"start_time": "2030-01-10T10:00:00"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler._edit_all_instances("u1", parent_id, "改到2030-01-10 10:00")
            )

            assert result["status"] == "success"
            first_logs = db.get_reminder_logs("series123_20300101")
            second_logs = db.get_reminder_logs("series123_20300102")
            assert sorted(log["remind_time"] for log in first_logs) == [
                "2030-01-01T09:00:00",
                "2030-01-01T10:00:00",
            ]
            assert sorted(log["remind_time"] for log in second_logs) == [
                "2030-01-02T09:00:00",
                "2030-01-02T10:00:00",
            ]
            assert all(log["sent_at"] for log in first_logs + second_logs)
            assert db.get_unconfirmed_sent_reminders() == []
        finally:
            db.cleanup()

    def test_edit_milestone_event_shifts_milestones_with_start_time(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_milestone_edit.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="报名流程",
                start_time="2030-01-01T10:00:00",
                end_time="2030-01-03T10:00:00",
                milestones=[
                    {"name": "开始", "time": "2030-01-01T10:00:00"},
                    {"name": "截止", "time": "2030-01-03T10:00:00"},
                ],
                remind_times=["2029-12-31T10:00:00", "2030-01-01T10:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "mile1234")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"start_time": "2030-01-05T10:00:00"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(handler.edit_event("u1", "mile1234 改到1月5日10点", MagicMock()))

            assert result["status"] == "success"
            assert "已更新日程" in result["message"]

            updated = db.items.get_item("mile1234", "u1")
            assert updated is not None
            assert updated.start_time == "2030-01-05T10:00:00"
            assert updated.end_time == "2030-01-07T10:00:00"
            assert updated.milestones == [
                {"name": "开始", "time": "2030-01-05T10:00:00"},
                {"name": "截止", "time": "2030-01-07T10:00:00"},
            ]
        finally:
            db.cleanup()

    def test_edit_milestone_event_updates_targeted_milestone_and_keeps_other_nodes(self, tmp_path):
        import sys
        from unittest.mock import AsyncMock, MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_milestone_targeted_edit.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="学术会议",
                start_time="2030-01-06T00:00:00",
                end_time="2030-01-26T12:00:00",
                milestones=[
                    {"name": "注册截止", "time": "2030-01-06T00:00:00"},
                    {"name": "报告提交截止", "time": "2030-01-13T00:00:00"},
                    {"name": "会议开始", "time": "2030-01-22T10:30:00"},
                    {"name": "会议结束", "time": "2030-01-26T12:00:00"},
                ],
                remind_times=[
                    "2030-01-05T00:00:00",
                    "2030-01-06T00:00:00",
                    "2030-01-12T00:00:00",
                    "2030-01-13T00:00:00",
                    "2030-01-21T10:30:00",
                    "2030-01-22T09:30:00",
                    "2030-01-22T10:30:00",
                    "2030-01-25T12:00:00",
                    "2030-01-26T12:00:00",
                ],
                notes="旧备注",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "mile5678")

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
                    "mile5678 会议开始改成1月22日中午12:43，备注从北京南坐G123去会场",
                    MagicMock(),
                )
            )

            assert result["status"] == "success"

            updated = db.items.get_item("mile5678", "u1")
            assert updated is not None
            assert updated.start_time == "2030-01-06T00:00:00"
            assert updated.end_time == "2030-01-26T12:00:00"
            assert updated.notes == "从北京南坐G123去会场"
            assert updated.milestones == [
                {"name": "注册截止", "time": "2030-01-06T00:00:00"},
                {"name": "报告提交截止", "time": "2030-01-13T00:00:00"},
                {"name": "会议开始", "time": "2030-01-22T12:43:00"},
                {"name": "会议结束", "time": "2030-01-26T12:00:00"},
            ]
            assert updated.remind_times == [
                "2030-01-05T00:00:00",
                "2030-01-06T00:00:00",
                "2030-01-12T00:00:00",
                "2030-01-13T00:00:00",
                "2030-01-21T12:43:00",
                "2030-01-22T11:43:00",
                "2030-01-22T12:43:00",
                "2030-01-25T12:00:00",
                "2030-01-26T12:00:00",
            ]
        finally:
            db.cleanup()

    def test_parent_id_reminder_view_returns_aggregate_series_reminders(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_parent_reminders.db"))

        try:
            first = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-01T10:00:00",
                remind_times=["2030-01-01T09:00:00"],
                parent_id="abcd1234",
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-02T10:00:00",
                remind_times=["2030-01-02T09:00:00"],
                parent_id="abcd1234",
                rrule="FREQ=DAILY;COUNT=2",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "abcd1234_20300101")
            db.items.insert_item(second, "abcd1234_20300102")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.list_reminders("u1", "abcd1234", MagicMock()))

            assert result["status"] == "success"
            assert "共 2 个日程实例" in result["message"]
            assert "01月01日 10:00" in result["message"]
            assert "01月02日 10:00" in result["message"]
        finally:
            db.cleanup()


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
            db.items.insert_item(diary, "82d34407")

            handler = DiaryHandler(db=db)
            result = asyncio.run(handler.view_diary("u1", "82d34407", SimpleNamespace()))

            assert result["status"] == "success"
            assert "2026-03-28的日记" in result["message"]
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
            db.items.insert_item(diary, "82d34407")

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
            db.items.insert_item(event, "evt12345")

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
            db.items.insert_item(diary, "82d34407")

            handler = DiaryHandler(db=db)
            result = asyncio.run(handler.delete_diary("u1", "82d34407", SimpleNamespace()))

            assert result["status"] == "success"
            assert "已删除 2026-03-28 的日记" in result["message"]
            assert db.items.get_item("82d34407", "u1") is None
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
            db.items.insert_item(event, "evt12345")

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
            db.items.insert_item(event, "evt12345")

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
            db.items.insert_item(event, "evt12345")

            handler = NoteHandler(db=db)
            result = asyncio.run(handler.delete_note("u1", "evt12345", SimpleNamespace()))
            preserved = db.items.get_item("evt12345", "u1")

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
            item = db.items.get_item(result["item_id"], "u1")
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
            item = db.items.get_item(result["item_id"], "u1")
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
            item = db.items.get_item(result["item_id"], "u1")
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
            item = db.items.get_item(result["item_id"], "u1")
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
                    'title:我的 笔记标题\n# 一级标题\n正文内容\ncat:工作 #归档',
                    SimpleNamespace(),
                )
            )

            assert result["status"] == "success"
            item = db.items.get_item(result["item_id"], "u1")
            assert item is not None
            assert item.title == "我的 笔记标题"
            assert item.content == "# 一级标题\n正文内容"
            assert item.category == "工作"
            assert item.tags == ["归档"]
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
            db.items.insert_item(diary, "dia12345")

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
        from plugins.pendo.models.item import NoteItem, ItemType
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
            db.items.insert_item(note, "not12345")

            handler = LedgerHandler(db=db)
            result = asyncio.run(
                handler.edit_ledger("u1", "not12345 amount:50 cat:交通", SimpleNamespace())
            )
            preserved = db.items.get_item("not12345", "u1")

            assert result["status"] == "success"
            assert "不是账目ID" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.NOTE
            assert preserved.title == "采购清单"
        finally:
            db.cleanup()

    def test_ledger_add_session_starts_with_amount_then_description(self):
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
        assert "后面我再问描述、收支类型和分类" in result["message"]
        assert create_calls[0][0]["step"] == "amount"

    def test_ledger_add_session_flow_collects_amount_and_description_before_options(self):
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
        assert session["step"] == "direction"
        assert session["data"]["title"] == "午饭"

        result = asyncio.run(handler.handle_session_step("u1", "1", session, context))
        assert result["status"] == "success"
        assert "请选择分类" in result["message"]
        assert session["step"] == "category"
        assert session["data"]["direction"] == "expense"

        result = asyncio.run(handler.handle_session_step("u1", "1", session, context))
        assert result == {"status": "success", "message": "saved"}
        assert context.end_calls == 1
        assert captured["user_id"] == "u1"
        assert captured["group_id"] == 123
        assert captured["data"]["amount"] == 88.5
        assert captured["data"]["title"] == "午饭"
        assert captured["data"]["direction"] == "expense"
        assert captured["data"]["ledger_category"]

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
                status=TaskStatus.TODO,
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.items.insert_item(task, "tsk12345")

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
        from plugins.pendo.models.item import NoteItem, ItemType
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
            db.items.insert_item(note, "not12345")

            handler = TaskHandler(db=db)
            result = asyncio.run(handler.mark_done("u1", "not12345", SimpleNamespace()))
            preserved = db.items.get_item("not12345", "u1")

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
                status=TaskStatus.TODO,
                created_at="2026-03-28T21:41:10",
                updated_at="2026-03-28T21:41:10",
            )
            db.items.insert_item(task, "tsk12345")

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
            db.items.insert_item(diary, "dia12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.edit_event("u1", "dia12345 改到明天下午两点", SimpleNamespace()))
            preserved = db.items.get_item("dia12345", "u1")

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert "/pendo diary view dia12345" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.DIARY
            assert getattr(preserved, "diary_date") == "2026-03-28"
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
            db.items.insert_item(diary, "8bec805e")

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
        from plugins.pendo.models.item import NoteItem, ItemType
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
            db.items.insert_item(note, "not12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(
                handler.set_reminders("u1", "not12345 提前1天提醒", SimpleNamespace())
            )
            preserved = db.items.get_item("not12345", "u1")

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.NOTE
            assert getattr(preserved, "title") == "采购清单"
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
            db.items.insert_item(note, "not12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.delete_event("u1", "not12345", SimpleNamespace()))
            preserved = db.items.get_item("not12345", "u1")

            assert result["status"] == "success"
            assert "不是日程ID" in result["message"]
            assert "/pendo note view not12345" in result["message"]
            assert preserved is not None
            assert preserved.type == ItemType.NOTE
            assert getattr(preserved, "title") == "采购清单"
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

            saved = db.items.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert "情绪: calm" in result["message"]
            assert saved is not None
            assert getattr(saved, "mood") == "calm"
            assert getattr(saved, "mood_score") == 6
        finally:
            db.cleanup()

    def test_append_diary_reanalyzes_mood_with_ai(self, tmp_path):
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
            db.items.insert_item(diary, "dia12345")

            handler = DiaryHandler(db=db, ai_parser=ai_parser)
            result = asyncio.run(
                handler.add_diary("u1", "2026-03-28 晚上玩得很开心", SimpleNamespace(), None)
            )

            updated = db.items.get_item("dia12345", "u1")

            assert result["status"] == "success"
            assert len(ai_parser.calls) == 1
            assert "早上出门。" in ai_parser.calls[0]
            assert "晚上玩得很开心" in ai_parser.calls[0]
            assert updated is not None
            assert getattr(updated, "mood") == "happy"
            assert getattr(updated, "mood_score") == 8
        finally:
            db.cleanup()


class TestReminderBackfillRegression:
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

    def test_confirm_future_remind_time_with_allow_future_creates_preconfirmed_log(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_confirm_future.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="提醒测试",
                start_time="2030-01-02T10:00:00",
                remind_times=["2030-01-02T09:00:00", "2030-01-02T10:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "evtfuture")

            result = db.items.confirm_reminder(
                "evtfuture",
                "preconfirmed",
                owner_id="u1",
                remind_time="2030-01-02T09:00:00",
                allow_future=True,
            )

            assert result["status"] == "success"
            logs = db.items.get_reminder_logs("evtfuture")
            assert len(logs) == 1
            assert logs[0]["remind_time"] == "2030-01-02T09:00:00"
            assert logs[0]["sent_at"] is None
            assert logs[0]["confirmed_at"]
            assert logs[0]["user_action"] == "preconfirmed"
            assert logs[0]["repeat_count"] == 0
            assert logs[0]["last_sent_at"] is None
        finally:
            db.cleanup()

    def test_event_reminders_confirm_today_preconfirms_all_matching_reminders(self, tmp_path, monkeypatch):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers import event as event_module
        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        fixed_now = datetime.fromisoformat("2030-01-02T08:00:00+08:00")
        monkeypatch.setattr(event_module, "now_in_timezone", lambda user_id=None, db=None: fixed_now)

        db = Database(str(tmp_path / "pendo_reminders_confirm_today.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="今日提醒",
                start_time="2030-01-02T14:00:00",
                remind_times=[
                    "2030-01-02T09:00:00",
                    "2030-01-02T13:00:00",
                    "2030-01-02T14:00:00",
                    "2030-01-03T09:00:00",
                ],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "evtday02")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(
                handler.handle_reminders("u1", "confirm evtday02 today", MagicMock())
            )

            assert result["status"] == "success"
            assert "已确认 3 个提醒" in result["message"]
            logs = db.items.get_reminder_logs("evtday02")
            confirmed = {log["remind_time"] for log in logs if log["confirmed_at"]}
            assert confirmed == {
                "2030-01-02T09:00:00",
                "2030-01-02T13:00:00",
                "2030-01-02T14:00:00",
            }
            assert "2030-01-03T09:00:00" not in confirmed
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

    def test_preconfirmed_future_reminder_is_not_sent_by_scheduler(self, monkeypatch):
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

            def get_reminder_logs(self, item_id):
                assert item_id == "evt123"
                return [
                    {
                        "remind_time": "2030-01-01T09:00:00",
                        "sent_at": None,
                        "confirmed_at": "2030-01-01T08:00:00",
                        "user_action": "preconfirmed",
                        "repeat_count": 0,
                        "last_sent_at": None,
                    }
                ]

            def log_reminder(self, item_id, remind_time, sent=True):
                self.logged.append((item_id, remind_time, sent))

            def get_user_settings(self, user_id):
                return {
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "07:00",
                    "settings_json": {"reminder_enabled": True},
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
        assert "scheduled_cleanup_demo_data" in handler_ids
        weekly_entry = next(
            entry for entry in config.get("schedule", [])
            if entry["handler"] == "scheduled_weekly_finance_summary"
        )
        monthly_entry = next(
            entry for entry in config.get("schedule", [])
            if entry["handler"] == "scheduled_month_end_finance_summary"
        )
        cleanup_entry = next(
            entry for entry in config.get("schedule", [])
            if entry["handler"] == "scheduled_cleanup_demo_data"
        )
        assert weekly_entry["cron"] == {"day_of_week": "sun", "hour": 21, "minute": 0}
        assert monthly_entry["cron"] == {"day": "last", "hour": 21, "minute": 0}
        assert cleanup_entry["cron"] == {"hour": "*/6", "minute": 15}

    def test_cleanup_expired_demo_data_runs_periodic_purge(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        calls = []

        def fake_purge(_db):
            calls.append(_db)
            return 3

        monkeypatch.setattr(scheduled_module, "purge_expired_demo_users", fake_purge)

        db = SimpleNamespace()
        result = asyncio.run(scheduled_module.cleanup_expired_demo_data(SimpleNamespace(), db))

        assert result == []
        assert calls == [db]

    def test_main_source_exposes_demo_cleanup_scheduled_handler(self):
        src = (ROOT / "plugins" / "pendo" / "main.py").read_text(encoding="utf-8")

        assert "cleanup_expired_demo_data," in src
        assert "async def scheduled_cleanup_demo_data(context) -> list[dict[str, Any]]:" in src
        assert '"cleanup_demo_data",' in src


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


class TestOperationAndExportRegression:
    def test_snooze_missing_args_returns_error_result(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands.operations import handle_snooze

        result = asyncio.run(handle_snooze("u1", "", SimpleNamespace(db=None)))

        assert result["status"] == "error"
        assert "请指定要延后的条目ID和时间" in result["message"]

    def test_export_markdown_writes_single_file_and_filters_types(self, monkeypatch, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import exporter as exporter_module
        from plugins.pendo.services.exporter import ExporterService

        monkeypatch.setattr(exporter_module, "_get_export_dir", lambda user_id: tmp_path)

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
            milestones=[],
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
            due_time="2026-03-15T18:00:00",
            priority=2,
            status="todo",
            completed_at=None,
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
            def get_items(self, user_id, filters, limit):
                item_type = filters.get("type")
                if item_type == "event":
                    return [event_item]
                if item_type == "task":
                    return [task_item]
                if item_type == "note":
                    return [note_item]
                return []

        service = ExporterService(SimpleNamespace(items=_Repo(), log_transfer=lambda **kwargs: 1))
        result = service.export_markdown(
            "u1",
            "工作档案 2026-03-01..2026-03-31 event,todo",
            {},
        )

        assert result["status"] == "success"
        assert result["record_count"] == 2
        assert result["file_name"] == "工作档案.md"

        exported = (tmp_path / "工作档案.md").read_text(encoding="utf-8")
        assert "# Pendo 导出档案 · 工作档案" in exported
        assert "## 导出摘要" in exported
        assert "## 日程" in exported
        assert "## 待办" in exported
        assert "项目周会" in exported
        assert "提交周报" in exported
        assert "研究想法" not in exported

    def test_export_markdown_requires_filename(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.exporter import ExporterService

        service = ExporterService(SimpleNamespace(items=SimpleNamespace()))
        result = service.export_markdown("u1", "", {})

        assert result["status"] == "error"
        assert "请提供导出文件名" in result["message"]


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
        assert "直接复制这整条消息到网页登录框" in token_text

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
        assert "直接复制这整条消息到网页登录框" in result["message"]

    def test_web_start_surfaces_last_start_error(self):
        import sys
        import types
        import importlib

        sys.path.insert(0, str(ROOT))
        sys.modules.pop("plugins.pendo.handlers.web", None)
        sys.modules["plugins.pendo.web.server"] = types.SimpleNamespace(
            get_url=lambda: "http://127.0.0.1:8765",
            is_running=lambda: False,
            start=lambda _db: False,
            stop=lambda: True,
            get_last_error=lambda: "无法绑定到 127.0.0.1:8765，端口可能已被占用。",
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(handler.handle("1001", "start", context=None))

        assert result["status"] == "error"
        assert "服务启动失败" in result["message"]
        assert "端口可能已被占用" in result["message"]
        assert "PENDO_WEB_PORT" in result["message"]
