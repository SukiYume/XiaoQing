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
            "reminder.py"
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
            _build_task(f"d{i}", "2026-02-10", f"2026-02-10T08:00:0{i}")
            for i in range(1, 9)
        ]
        work_tasks = [
            _build_task(f"w{i}", "work", f"2026-02-10T09:00:0{i}")
            for i in range(1, 8)
        ]
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
        assert hasattr(item, 'milestones')
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
        assert d['milestones'] == milestones
        assert d['notes'] == "备注"


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

        mock_response = json.dumps({
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
            "notes": "https://example.com"
        })

        async def run():
            with patch.object(parser, '_call_llm', new=AsyncMock(return_value=mock_response)):
                return await parser.parse_event_with_ai("...", "user1")

        result = asyncio.run(run())
        assert result['milestones'][0]['name'] == "注册截止"
        assert result['start_time'] == "2030-04-06T00:00:00"
        assert result['end_time'] == "2030-04-26T12:00:00"
        assert result['notes'] == "https://example.com"
        assert len(result['remind_times']) == 6  # 3 milestones × 2 offsets


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
        handler = self._make_handler()
        parsed_data = {
            'title': '星团会议',
            'milestones': [
                {'name': '注册截止', 'time': '2030-04-06T00:00:00'},
                {'name': '会议开始', 'time': '2030-04-22T10:30:00'},
                {'name': '会议结束', 'time': '2030-04-26T12:00:00'},
            ],
            'start_time': '2030-04-06T00:00:00',
            'end_time': '2030-04-26T12:00:00',
            'location': '江苏溧水',
            'notes': 'https://example.com',
            'remind_times': ['2030-04-05T00:00:00', '2030-04-05T23:00:00'],
        }
        from plugins.pendo.models.item import ItemType
        parsed_data['type'] = ItemType.EVENT

        async def run():
            return await handler.create_event("user1", parsed_data, {})

        result = asyncio.run(run())
        assert result['status'] == 'success'
        assert '多时间节点' in result['message']
        assert '3个节点' in result['message']
        assert '注册截止' in result['message']
        assert '江苏溧水' in result['message']
        assert 'https://example.com' in result['message']

    def test_create_single_event_with_notes(self):
        import asyncio
        handler = self._make_handler()
        parsed_data = {
            'title': '普通会议',
            'milestones': [],
            'start_time': '2030-04-06T09:00:00',
            'notes': '会议室在3楼',
            'remind_times': ['2030-04-05T09:00:00'],
        }
        from plugins.pendo.models.item import ItemType
        parsed_data['type'] = ItemType.EVENT

        async def run():
            return await handler.create_event("user1", parsed_data, {})

        result = asyncio.run(run())
        assert result['status'] == 'success'
        assert '会议室在3楼' in result['message']


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
