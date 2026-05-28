"""
pendo 插件单元测试

测试个人时间与信息管理中枢插件的功能。
由于 pendo 插件使用相对导入且有复杂的模块结构，我们主要测试文件结构和配置。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from core.config import ConfigSnapshot

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

        with open(main_path, encoding="utf-8") as f:
            content = f.read()
            assert "class Plugin" in content or "plugin" in content.lower()

    def test_config_module_exists(self):
        """测试配置模块存在"""
        config_path = ROOT / "plugins" / "pendo" / "config.py"
        assert config_path.exists()

        with open(config_path, encoding="utf-8") as f:
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
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        assert "name" in config
        assert "version" in config
        assert "description" in config
        assert "commands" in config

    def test_plugin_commands_exist(self):
        """测试插件有命令定义"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        commands = config.get("commands", [])
        assert len(commands) > 0, "No commands defined in plugin.json"

    def test_plugin_help_mentions_widget_token(self):
        """测试插件摘要帮助包含 widget-token 提示"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        commands = config.get("commands", [])
        assert commands
        assert "widget-token" in commands[0].get("help", "")

    def test_plugin_has_schedule(self):
        """测试插件有定时任务配置"""
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        # 检查是否有 schedule 配置
        schedule = config.get("schedule", [])
        assert isinstance(schedule, list)

    def test_show_help_uses_overview_and_subcommand_sections(self):
        """测试根帮助只显示命令总览，子命令帮助保留分节样式"""
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.main import _show_help

        overview = _show_help()
        event_help = _show_help("event")

        assert "🧭 **可用命令**" in overview
        assert "• /pendo event" in overview
        assert "💡 查看详细用法" in overview
        assert "━━ 🗓️ **日程管理 (Event)**" not in overview
        assert "━━ 🗓️ **日程管理 (Event)**" in event_help
        assert "/pendo event add 3月8日下午两点，国自然截止" in event_help

    def test_show_help_for_subcommand_only_renders_requested_section(self):
        """测试子模块帮助只渲染对应模块并保留顶部导航提示"""
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.main import _show_help

        help_text = _show_help("event")

        assert "🧭 输入 `/pendo` 查看完整总览" in help_text
        assert "`/pendo help <模块>`" in help_text
        assert "━━ 🗓️ **日程管理 (Event)**" in help_text
        assert "先用 view 集合ID 查看节点ID，再编辑具体节点" in help_text
        assert "集合ID只编辑整体标题、分类、地点、备注，不修改某个节点时间" in help_text
        assert "/pendo event edit 80efbef6_m03 改到4月22日12:43" in help_text
        assert "/pendo event edit 80efbef6 标题改为FAST会议行程" in help_text
        assert "多节点事件可直接写“节点名 + 改成/改到 + 新时间”" not in help_text
        assert "/pendo event reminders delete <id> <all|today|future|提醒时间>" in help_text
        assert "━━ ✅ **待办事项 (Todo)**" not in help_text

    def test_command_router_help_uses_detailed_provider_for_subcommands(self):
        """测试 /pendo help event 复用 main.py 的详细帮助，而不是旧路由摘要。"""
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.core.router import CommandRouter
        from plugins.pendo.main import _show_help

        async def dummy_handler(user_id, args, context):
            return {"status": "success", "message": "ok"}

        router = CommandRouter(
            {
                "event": dummy_handler,
                "note": dummy_handler,
                "diary": dummy_handler,
                "settings": dummy_handler,
                "confirm": dummy_handler,
            },
            help_provider=_show_help,
        )

        event_help = router.get_help_message("event")
        assert "━━ 🗓️ **日程管理 (Event)**" in event_help
        assert "/pendo event reminders delete <id> <all|today|future|提醒时间>" in event_help
        assert "📖 event - 管理日程" not in event_help

        reminder_help = router.get_help_message("reminder")
        assert "━━ ⏰ **提醒操作**" in reminder_help

        alias_help = router.get_help_message("confirm")
        assert "━━ ⏰ **提醒操作**" in alias_help
        assert "/pendo event reminders set/delete/confirm <id> ..." in alias_help

        assert router.alias_map["calendar"] == "event"
        assert router.alias_map["idea"] == "note"
        assert router.alias_map["journal"] == "diary"
        assert router.alias_map["config"] == "settings"

    def test_help_map_covers_current_command_surface_and_beginner_examples(self):
        """测试 HELP_MAP 覆盖当前命令面、关键参数和可复制示例。"""
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.main import HELP_SECTION_ORDER, _show_help

        overview = _show_help()
        assert "🧭 **可用命令**" in overview
        assert "/pendo export <文件名> [范围] [类型]" not in overview

        help_text = "\n".join(_show_help(section) for section in HELP_SECTION_ORDER)

        expected_fragments = [
            # event
            "/pendo event add <内容>",
            "/pendo event list [范围] [cat:分类] [#标签]",
            "/pendo event view <id>",
            "/pendo event edit <id> <内容>",
            "/pendo event delete <id>",
            "/pendo event reminders [id|范围]",
            "/pendo event reminders list [范围]",
            "/pendo event reminders set <id> <描述>",
            "/pendo event reminders delete <id> <all|today|future|提醒时间>",
            "/pendo event reminders confirm <id> [today|future|all|提醒时间]",
            # todo
            "/pendo todo add <内容>",
            "remind:YYYY-MM-DDTHH:MM",
            "/pendo todo list [范围|状态|分类|cat:分类|#标签]",
            "last7d/last30d",
            "/pendo todo view <id>",
            "/pendo todo done <id>",
            "/pendo todo cancel <id>",
            "/pendo todo undone <id>",
            "/pendo todo edit <id> <内容>",
            "/pendo todo delete <id|cat:分类>",
            # note
            "/pendo note add <内容>",
            "ref:条目ID",
            "since:范围",
            "/pendo note view <id>",
            "/pendo note edit <id>",
            "/pendo note append <id>",
            "/pendo note tag <id>",
            "untag <id>",
            "/pendo note link <id>",
            "/pendo note delete <id|cat:分类>",
            # diary
            "/pendo diary add [日期]",
            "favorite:true",
            "/pendo diary template [编号|名称]",
            "/pendo diary list [范围] [mood:情绪] [cat:分类] [#标签]",
            "/pendo diary view [日期|ID]",
            "/pendo diary delete <日期|ID>",
            # ledger
            "/pendo ledger add",
            "/pendo ledger quick <金额> <描述>",
            "type:expense/income/transfer",
            "/pendo ledger list [范围] [筛选]",
            "all page:N",
            "/pendo ledger view <id>",
            "/pendo ledger edit <id>",
            "/pendo ledger delete <id>",
            "/pendo ledger summary [范围]",
            # search/settings/common/export/web
            "/pendo search <关键词>",
            "#标签",
            "tag=<标签>",
            "transaction_type=income/expense/transfer",
            "/pendo confirm <id>",
            "/pendo snooze <id> <时间>",
            "/pendo undo [分钟]",
            "/pendo export <文件名> [范围] [类型]",
            "/pendo import - 查看 Web 导入入口和支持格式",
            "/pendo settings timezone <IANA时区>",
            "/pendo settings quiet_hours <开始>-<结束>",
            "/pendo web widget-token",
            "/pendo web status",
            "别名: `task`, `t`, `待办`, `任务`",
            "别名: `bill`, `finance`, `记账`, `账单`",
        ]

        for fragment in expected_fragments:
            assert fragment in help_text

        beginner_examples = [
            "/pendo event add 明天9点组会",
            "/pendo todo add 写周报",
            "/pendo note add title:读书摘录",
            "/pendo diary add 今天跑步5公里",
            "/pendo ledger quick 35.5 午饭",
            "/pendo event add 3月8日下午两点，国自然截止，提前一周和一天提醒",
            "/pendo event add 每月18号上午十点，公积金提取，重复7个月",
            "/pendo event edit 80efbef6_m03 改到4月22日12:43",
            "/pendo ledger list 2026-03 type:expense",
            "/pendo search 组会 type=event",
            "/pendo settings timezone Asia/Shanghai",
            "/pendo export \"三月 账本\" 2026-03 ledger",
            "/pendo import",
            "/pendo event edit 80efbef6_m03 备注为从北京南坐G123去会场",
            "/pendo event edit 80efbef6_m03 地点改到北京南",
        ]
        for example in beginner_examples:
            assert example in help_text

        stale_or_internal_phrases = [
            "reminder rules",
            "提醒规则",
            "节点名 + 改成/改到",
            "/pendo event edit 80efbef6 会议开始改成",
            "title:<标题>\\n<正文多行>",
        ]
        for phrase in stale_or_internal_phrases:
            assert phrase not in help_text


class TestPendoServices:
    """测试 pendo 服务模块"""

    def test_database_service_exists(self):
        """测试数据库服务模块"""
        db_path = ROOT / "plugins" / "pendo" / "services" / "db.py"
        with open(db_path, encoding="utf-8") as f:
            content = f.read()

        assert "class" in content
        assert "Database" in content

    def test_ai_parser_service_exists(self):
        """测试 AI 解析服务模块"""
        ai_path = ROOT / "plugins" / "pendo" / "services" / "ai_parser.py"
        with open(ai_path, encoding="utf-8") as f:
            content = f.read()

        assert "async def" in content or "def" in content

    def test_rule_parser_service_exists(self):
        """测试规则解析服务模块"""
        rule_path = ROOT / "plugins" / "pendo" / "services" / "rule_parser.py"
        with open(rule_path, encoding="utf-8") as f:
            content = f.read()

        assert "async def" in content or "def" in content

    def test_exporter_service_exists(self):
        """测试导出服务模块"""
        exporter_path = ROOT / "plugins" / "pendo" / "services" / "exporter.py"
        with open(exporter_path, encoding="utf-8") as f:
            content = f.read()

        assert "async def" in content or "def" in content

    def test_reminder_service_exists(self):
        """测试提醒服务模块"""
        reminder_path = ROOT / "plugins" / "pendo" / "services" / "reminder.py"
        with open(reminder_path, encoding="utf-8") as f:
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
                with open(model_file, encoding="utf-8") as f:
                    content = f.read()
                    # 检查是否有数据类或类型定义
                    assert "class" in content or "dataclass" in content


class TestPendoDocumentation:
    """测试 pendo 文档"""

    def test_readme_exists(self):
        """测试 README 文件存在"""
        readme_path = ROOT / "plugins" / "pendo" / "README.md"
        assert readme_path.exists()

        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
            assert len(content) > 100  # 应该有实际内容

    def test_architecture_doc_exists(self):
        """测试架构文档存在"""
        arch_path = ROOT / "plugins" / "pendo" / "ARCHITECTURE.md"
        assert arch_path.exists()

        with open(arch_path, encoding="utf-8") as f:
            content = f.read()
            assert len(content) > 100

    def test_duplicate_plugin_doc_removed(self):
        """测试重复的旧插件说明文档已移除"""
        doc_path = ROOT / "plugins" / "pendo" / "Pendo个人时间与信息管理中枢.md"
        assert not doc_path.exists()


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
                with open(cmd_file, encoding="utf-8") as f:
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
    def test_init_reads_demo_switch_from_global_config_and_updates_on_reload(self, monkeypatch):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.config import PendoConfig

        class _DummyDb:
            def cleanup(self):
                return None

        class _DummyConfigManager:
            def __init__(self):
                self.callbacks = []

            def on_reload(self, callback):
                self.callbacks.append(callback)

        config_manager = _DummyConfigManager()
        context = SimpleNamespace(
            config={"plugins": {"pendo": {"web_demo_enabled": True}}},
            config_manager=config_manager,
            state={},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )

        monkeypatch.setattr(PendoConfig, "WEB_ENABLED", False)
        monkeypatch.setattr(pendo_main, "Database", lambda path: _DummyDb())
        monkeypatch.setattr(pendo_main, "_startup_db", None, raising=False)

        pendo_main.init(context)

        assert PendoConfig.WEB_DEMO_ENABLED is True
        assert len(config_manager.callbacks) == 1

        config_manager.callbacks[0](ConfigSnapshot(
            config={"plugins": {"pendo": {"web_demo_enabled": False}}},
            secrets={},
        ))

        assert PendoConfig.WEB_DEMO_ENABLED is False

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

    def test_import_command_guides_user_to_web_import(self, monkeypatch):
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

        assert "import" in router.commands

        result = asyncio.run(router.route("import", "u1", "", SimpleNamespace(state={})))

        assert result["status"] == "success"
        assert "/pendo web token" in result["message"]
        assert "Web 数据迁移页" in result["message"]
        assert "不接收本地文件路径" in result["message"]

    def test_plugin_trigger_aliases_are_routed_to_matching_subcommands(self, monkeypatch):
        import logging

        from plugins.pendo import main as pendo_main

        task_handler = _StubCaptureHandler()
        event_handler = _StubCaptureHandler()
        diary_handler = _StubCaptureHandler()
        services = {
            "db": object(),
            "reminder_service": object(),
            "exporter": _StubExporter(),
            "event_handler": event_handler,
            "task_handler": task_handler,
            "note_handler": _StubSimpleHandler(),
            "diary_handler": diary_handler,
            "search_handler": _StubSimpleHandler(),
            "ledger_handler": _StubSimpleHandler(),
            "web_handler": _StubSimpleHandler(),
        }

        monkeypatch.setattr(pendo_main, "_get_services", lambda context: services)
        context = SimpleNamespace(state={}, logger=logging.getLogger("pendo-test"))

        asyncio.run(pendo_main.handle("待办", "add TEST_ALIAS_TASK", {"user_id": "u1"}, context))
        asyncio.run(pendo_main.handle("日程", "add TEST_ALIAS_EVENT", {"user_id": "u1"}, context))
        asyncio.run(pendo_main.handle("日记", "add TEST_ALIAS_DIARY", {"user_id": "u1"}, context))

        assert task_handler.calls[0]["args"] == "add TEST_ALIAS_TASK"
        assert event_handler.calls[0]["args"] == "add TEST_ALIAS_EVENT"
        assert diary_handler.calls[0]["args"] == "add TEST_ALIAS_DIARY"

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

    def test_export_month_week_and_type_combinations_use_list_style_ranges(self, tmp_path, monkeypatch):
        import shutil
        from datetime import datetime as real_datetime

        from plugins.pendo.services import exporter as exporter_module
        from plugins.pendo.services.db import Database
        from plugins.pendo.services.exporter import ExporterService
        from plugins.pendo.utils import time_utils

        class FrozenDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                base = cls(2026, 5, 3, 16, 0, 0)
                return base if tz is None else base.replace(tzinfo=tz)

        monkeypatch.setattr(time_utils, "datetime", FrozenDateTime)
        monkeypatch.setattr(
            exporter_module,
            "_get_export_dir",
            lambda user_id: tmp_path / "exports" / user_id,
        )

        db = Database(str(tmp_path / "pendo-export.db"))
        owner = "u-export-ranges"
        rows = [
            {
                "id": "ld_may1",
                "owner_id": owner,
                "type": "ledger",
                "title": "五月一日账目",
                "ledger_category": "餐饮",
                "transaction_type": "expense",
                "amount": 10,
                "amount_cents": 1000,
                "ledger_date": "2026-05-01",
                "created_at": "2026-05-01T12:00:00",
                "updated_at": "2026-05-01T12:00:00",
            },
            {
                "id": "ld_may3",
                "owner_id": owner,
                "type": "ledger",
                "title": "五月三日账目",
                "ledger_category": "交通",
                "transaction_type": "expense",
                "amount": 20,
                "amount_cents": 2000,
                "ledger_date": "2026-05-03",
                "created_at": "2026-05-03T12:00:00",
                "updated_at": "2026-05-03T12:00:00",
            },
            {
                "id": "ld_may4",
                "owner_id": owner,
                "type": "ledger",
                "title": "五月四日账目",
                "ledger_category": "交通",
                "transaction_type": "expense",
                "amount": 30,
                "amount_cents": 3000,
                "ledger_date": "2026-05-04",
                "created_at": "2026-05-04T12:00:00",
                "updated_at": "2026-05-04T12:00:00",
            },
            {
                "id": "ev_may1",
                "owner_id": owner,
                "type": "event",
                "title": "五月一日日程",
                "start_time": "2026-05-01T09:00:00",
                "end_time": "2026-05-01T10:00:00",
                "created_at": "2026-05-01T08:00:00",
                "updated_at": "2026-05-01T08:00:00",
            },
            {
                "id": "tk_may3",
                "owner_id": owner,
                "type": "task",
                "title": "五月三日待办",
                "status": "open",
                "priority": 1,
                "plan_date": "2026-05-03",
                "created_at": "2026-05-03T09:00:00",
                "updated_at": "2026-05-03T09:00:00",
            },
            {
                "id": "note_may",
                "owner_id": owner,
                "type": "note",
                "title": "五月笔记",
                "content": "note",
                "created_at": "2026-05-02T10:00:00",
                "updated_at": "2026-05-02T10:00:00",
            },
        ]
        for row in rows:
            db.insert_item(row)

        service = ExporterService(db)

        month_result = service.export_markdown(owner, "本月账本 month ledger", {})
        month_text = Path(month_result["file_path"]).read_text(encoding="utf-8")
        assert month_result["record_count"] == 3
        assert "2026-05-01 00:00 .. 2026-05-31 23:59" in month_result["range_label"]
        assert "五月一日账目" in month_text
        assert "五月四日账目" in month_text

        week_result = service.export_markdown(owner, "本周账本 week ledger", {})
        week_text = Path(week_result["file_path"]).read_text(encoding="utf-8")
        assert week_result["record_count"] == 2
        assert "2026-04-27 00:00 .. 2026-05-03 23:59" in week_result["range_label"]
        assert "五月一日账目" in week_text
        assert "五月三日账目" in week_text
        assert "五月四日账目" not in week_text

        combo_result = service.export_markdown(owner, '"五月 工作" 2026-05 event,todo', {})
        assert combo_result["file_name"] == "五月 工作.md"
        assert combo_result["counts"]["event"] == 1
        assert combo_result["counts"]["task"] == 1
        assert combo_result["counts"]["note"] == 0

        shutil.rmtree(tmp_path / "exports", ignore_errors=True)

    def test_pendo_help_root_is_overview_and_subcommand_is_detailed(self):
        from plugins.pendo import main as pendo_main

        overview = pendo_main._show_help("")
        export_help = pendo_main._show_help("export")

        assert "🧭 **可用命令**" in overview
        assert "• /pendo event" in overview
        assert "/pendo export <文件名>" not in overview
        assert "多节点事件会生成" not in overview
        assert "/pendo export <文件名> [范围] [类型]" in export_help
        assert "week(本周), month(本月)" in export_help

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

    def test_get_database_reuses_shared_singleton_across_contexts(self):
        from plugins.pendo.services.db import Database
        from plugins.pendo.utils import db_ops

        db = Database(":memory:")
        try:
            db_ops.set_database_singleton(db)
            ctx_a = SimpleNamespace()
            ctx_b = SimpleNamespace()

            assert db_ops.get_database(ctx_a) is db
            assert db_ops.get_database(ctx_b) is db
            assert ctx_a.pendo_db is db
            assert ctx_b.pendo_db is db
        finally:
            db.cleanup()
            db_ops.set_database_singleton(None)

    def test_cached_empty_values_do_not_fall_through_to_sql(self, monkeypatch):
        from plugins.pendo.services.db import Database

        db = Database(":memory:")
        try:
            settings_key = db._cache_key("settings", "u-empty")
            items_key = db._cache_key("items", "u-empty", {"type": "note"}, 10, 0)
            db._cache_set(settings_key, {})
            db._cache_set(items_key, [])

            def _boom():
                raise AssertionError("should not hit sqlite when cache already has an empty value")

            monkeypatch.setattr(db, "get_connection", _boom)

            assert db.get_user_settings("u-empty") == {}
            assert db.get_items("u-empty", {"type": "note"}, 10, 0) == []
        finally:
            db.cleanup()

    def test_start_web_server_restarts_existing_server(self, monkeypatch):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.web import server as web_server

        calls = []
        state = {"running": True}
        db = object()

        monkeypatch.setattr(web_server, "is_running", lambda: state["running"])

        def fake_stop():
            calls.append("stop")
            state["running"] = False
            return True

        def fake_start(start_db):
            calls.append(("start", start_db))
            state["running"] = True
            return True

        monkeypatch.setattr(web_server, "stop", fake_stop)
        monkeypatch.setattr(web_server, "start", fake_start)

        assert pendo_main._start_web_server(db) is True
        assert calls == ["stop", ("start", db)]
        assert state["running"] is True

    def test_start_web_server_does_not_start_when_old_server_cannot_stop(self, monkeypatch):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.web import server as web_server

        calls = []
        state = {"running": True}

        monkeypatch.setattr(web_server, "is_running", lambda: state["running"])

        def fake_stop():
            calls.append("stop")
            return False

        def fake_start(_db):
            calls.append("start")
            return True

        monkeypatch.setattr(web_server, "stop", fake_stop)
        monkeypatch.setattr(web_server, "start", fake_start)

        assert pendo_main._start_web_server(object()) is False
        assert calls == ["stop"]

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
            owner_id="u1",
            title="摘要截止",
            event_role="multi_node_child",
            event_collection_id="conf2026",
            event_collection_kind="multi_node",
            event_index=1,
            event_node_key="m01",
            notes="备注",
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
                    {"name": "申请截止", "time": "2026-06-14T14:00:00"},
                ],
                "notes": "https://example.com/job",
            }
        )

        async def run():
            with patch.object(parser, "_call_llm", new=AsyncMock(return_value=mock_response)):
                return await parser.parse_event_with_ai("...", "user1")

        result = asyncio.run(run())
        assert result["title"] == "悉尼大学博后申请"
        assert result["start_time"] == "2026-06-14T14:00:00"
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

    def test_reminder_message_uses_collection_and_leaf_notes(self):
        """多节点 leaf 提醒显示集合标题、节点标题和节点备注"""
        from types import SimpleNamespace

        service = self._make_service()
        service.db.get_event_collection.return_value = {
            "id": "abc12345",
            "kind": "multi_node",
            "title": "星团会议",
            "notes": "这是整场会议的全局说明",
        }

        item = SimpleNamespace(
            id="abc12345_m01",
            title="注册截止",
            start_time="2030-04-06T00:00:00",
            end_time=None,
            location="江苏溧水",
            notes="报名材料今晚前发给秘书",
            remind_times=["2030-04-05T00:00:00", "2030-04-05T23:00:00"],
            context={},
            owner_id="user1",
            event_collection_id="abc12345",
            event_collection_kind="multi_node",
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
            id="abc12345",
            title="普通会议",
            start_time="2030-04-06T09:00:00",
            end_time=None,
            location="",
            notes="会议链接: https://meet.example.com",
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

    def test_edit_recurring_collection_updates_header_without_shifting_occurrences(self, tmp_path):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))
        collection_id = "series123"

        try:
            db.items.create_event_collection({
                "id": collection_id,
                "owner_id": "u1",
                "kind": "recurring",
                "title": "重复会议",
                "category": "工作",
                "rrule": "FREQ=DAILY;COUNT=2",
                "start_time": "2030-01-01T09:00:00",
                "end_time": "2030-01-02T10:00:00",
            })
            first = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-01T09:00:00",
                end_time="2030-01-01T10:00:00",
                remind_times=["2030-01-01T08:00:00"],
                event_role="recurring_occurrence",
                event_collection_id=collection_id,
                event_collection_kind="recurring",
                event_index=1,
                event_node_key="20300101",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-02T09:00:00",
                end_time="2030-01-02T10:00:00",
                remind_times=["2030-01-02T08:00:00"],
                event_role="recurring_occurrence",
                event_collection_id=collection_id,
                event_collection_kind="recurring",
                event_index=2,
                event_node_key="20300102",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "series123_20300101")
            db.items.insert_item(second, "series123_20300102")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"title": "新版重复会议"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler.edit_event("u1", f"{collection_id} 改名为新版重复会议", MagicMock())
            )

            assert result["status"] == "success"

            updated_first = db.items.get_item("series123_20300101", "u1")
            updated_second = db.items.get_item("series123_20300102", "u1")

            assert updated_first is not None
            assert updated_second is not None
            assert db.items.get_event_collection(collection_id, "u1")["title"] == "新版重复会议"
            assert updated_first.start_time == "2030-01-01T09:00:00"
            assert updated_second.start_time == "2030-01-02T09:00:00"
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

        handler = EventHandler(db=MagicMock(), ai_parser=_FakeAiParser(), reminder_service=MagicMock())
        current_event = EventItem(
            owner_id="u1",
            title="会议开始",
            location="杭州",
            notes="",
            start_time="2030-01-22T10:30:00",
        )

        updates = asyncio.run(
            handler._parse_updates("备注从北京南坐G123去会场", current_event)
        )

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
            assert updated.title == "FAST2026观测申请截止"
            assert updated.category == "工作"
            assert updated.start_time == "2026-04-07T14:00:00"
            assert updated.remind_times == [
                "2026-04-06T14:00:00",
                "2026-04-07T13:00:00",
                "2026-04-07T14:00:00",
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
            db.items.create_event_collection({
                "id": collection_id,
                "owner_id": "u1",
                "kind": "recurring",
                "title": "重复会议",
                "category": "工作",
                "rrule": "FREQ=DAILY;COUNT=2",
                "start_time": "2030-01-01T10:00:00",
                "end_time": "2030-01-02T11:00:00",
            })
            first = EventItem(
                owner_id="u1",
                title="重复会议",
                category="工作",
                start_time="2030-01-01T10:00:00",
                end_time="2030-01-01T11:00:00",
                remind_times=["2030-01-01T09:00:00"],
                event_role="recurring_occurrence",
                event_collection_id=collection_id,
                event_collection_kind="recurring",
                event_index=1,
                event_node_key="20300101",
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
                event_role="recurring_occurrence",
                event_collection_id=collection_id,
                event_collection_kind="recurring",
                event_index=2,
                event_node_key="20300102",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "series123_20300101")
            db.items.insert_item(second, "series123_20300102")

            ai_parser = MagicMock()
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

            updated_first = db.items.get_item("series123_20300101", "u1")
            updated_second = db.items.get_item("series123_20300102", "u1")

            assert updated_first is not None
            assert updated_second is not None
            assert updated_first.remind_times == [
                "2029-12-31T10:00:00",
                "2030-01-01T09:00:00",
                "2030-01-01T10:00:00",
            ]
            assert updated_second.remind_times == [
                "2030-01-01T10:00:00",
                "2030-01-02T09:00:00",
                "2030-01-02T10:00:00",
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
            db.items.insert_item(event, "evtlogs1")
            db.get_connection().execute(
                """
                INSERT INTO reminder_logs (item_id, remind_time, repeat_count)
                VALUES (?, ?, 1)
                """,
                ("evtlogs1", "2030-01-01T08:30:00"),
            )
            db.get_connection().commit()
            db.log_reminder("evtlogs1", "2030-01-01T09:00:00", sent=True)
            db.log_reminder("evtlogs1", "2030-01-01T10:00:00", sent=True)

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"start_time": "2030-01-10T10:00:00"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(
                handler.edit_event("u1", "evtlogs1 改到2030-01-10 10:00", MagicMock())
            )

            assert result["status"] == "success"
            logs = db.get_reminder_logs("evtlogs1")
            assert sorted(log["remind_time"] for log in logs) == [
                "2030-01-01T09:00:00",
                "2030-01-01T10:00:00",
            ]
            assert all(log["sent_at"] for log in logs)
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
            db.items.create_event_collection({
                "id": "mile1234",
                "owner_id": "u1",
                "kind": "multi_node",
                "title": "报名流程",
                "category": "未分类",
                "start_time": "2030-01-01T10:00:00",
                "end_time": "2030-01-03T10:00:00",
            })
            first = EventItem(
                owner_id="u1",
                title="开始",
                start_time="2030-01-01T10:00:00",
                event_role="multi_node_child",
                event_collection_id="mile1234",
                event_collection_kind="multi_node",
                event_index=1,
                event_node_key="m01",
                remind_times=["2029-12-31T10:00:00", "2030-01-01T10:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id="u1",
                title="截止",
                start_time="2030-01-03T10:00:00",
                event_role="multi_node_child",
                event_collection_id="mile1234",
                event_collection_kind="multi_node",
                event_index=2,
                event_node_key="m02",
                remind_times=["2030-01-03T10:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "mile1234_m01")
            db.items.insert_item(second, "mile1234_m02")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            async def fake_parse_updates(changes, current_event):
                return {"start_time": "2030-01-05T10:00:00"}

            handler._parse_updates = fake_parse_updates

            result = asyncio.run(handler.edit_event("u1", "mile1234_m01 改到1月5日10点", MagicMock()))

            assert result["status"] == "success"
            assert "已更新日程" in result["message"]

            updated_first = db.items.get_item("mile1234_m01", "u1")
            updated_second = db.items.get_item("mile1234_m02", "u1")
            assert updated_first is not None
            assert updated_second is not None
            assert updated_first.start_time == "2030-01-05T10:00:00"
            assert updated_second.start_time == "2030-01-03T10:00:00"
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
            db.items.create_event_collection({
                "id": "mile5678",
                "owner_id": "u1",
                "kind": "multi_node",
                "title": "学术会议",
                "category": "工作",
                "notes": "旧备注",
                "start_time": "2030-01-06T00:00:00",
                "end_time": "2030-01-26T12:00:00",
            })
            event = EventItem(
                owner_id="u1",
                title="会议开始",
                start_time="2030-01-22T10:30:00",
                remind_times=[
                    "2030-01-21T10:30:00",
                    "2030-01-22T09:30:00",
                    "2030-01-22T10:30:00",
                ],
                notes="",
                event_role="multi_node_child",
                event_collection_id="mile5678",
                event_collection_kind="multi_node",
                event_index=3,
                event_node_key="m03",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "mile5678_m03")

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

            updated = db.items.get_item("mile5678_m03", "u1")
            assert updated is not None
            assert updated.notes == "从北京南坐G123去会场"
            assert db.items.get_event_collection("mile5678", "u1")["notes"] == "旧备注"
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
            db.items.create_event_collection({
                "id": "abcd1234",
                "owner_id": "u1",
                "kind": "recurring",
                "title": "重复会议",
                "category": "未分类",
                "rrule": "FREQ=DAILY;COUNT=2",
                "start_time": "2030-01-01T10:00:00",
                "end_time": "2030-01-02T10:00:00",
            })
            first = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-01T10:00:00",
                remind_times=["2030-01-01T09:00:00"],
                event_role="recurring_occurrence",
                event_collection_id="abcd1234",
                event_collection_kind="recurring",
                event_index=1,
                event_node_key="20300101",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            second = EventItem(
                owner_id="u1",
                title="重复会议",
                start_time="2030-01-02T10:00:00",
                remind_times=["2030-01-02T09:00:00"],
                event_role="recurring_occurrence",
                event_collection_id="abcd1234",
                event_collection_kind="recurring",
                event_index=2,
                event_node_key="20300102",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(first, "abcd1234_20300101")
            db.items.insert_item(second, "abcd1234_20300102")

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
            db.items.create_event_collection({
                "id": "mileweek",
                "owner_id": "u1",
                "kind": "multi_node",
                "title": "学术会议",
                "category": "未分类",
                "start_time": "2030-01-22T10:30:00",
                "end_time": "2030-01-26T12:00:00",
            })
            start_event = EventItem(
                owner_id="u1",
                title="会议开始",
                start_time="2030-01-22T10:30:00",
                remind_times=[
                    "2030-01-21T10:30:00",
                    "2030-01-22T09:30:00",
                    "2030-01-22T10:30:00",
                ],
                event_role="multi_node_child",
                event_collection_id="mileweek",
                event_collection_kind="multi_node",
                event_index=1,
                event_node_key="m01",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            end_event = EventItem(
                owner_id="u1",
                title="会议结束",
                start_time="2030-01-26T12:00:00",
                remind_times=[
                    "2030-01-25T12:00:00",
                    "2030-01-26T12:00:00",
                ],
                event_role="multi_node_child",
                event_collection_id="mileweek",
                event_collection_kind="multi_node",
                event_index=2,
                event_node_key="m02",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(start_event, "mileweek_m01")
            db.items.insert_item(end_event, "mileweek_m02")
            db.log_reminder("mileweek_m01", "2030-01-21T10:30:00", sent=True)

            monkeypatch.setattr(
                event_module,
                "parse_event_time_range",
                lambda _query: ("2030-01-20T00:00:00", "2030-01-26T23:59:59"),
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

    def test_note_add_parses_references_and_view_shows_linked_item(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.note import NoteHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_note_reference.db"))

        try:
            db.items.insert_item(
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
            item = db.items.get_item(result["item_id"], "u1")
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
            db.items.insert_item(
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
            db.items.insert_item(
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

            updated = db.items.get_item("note_main", "u1")
            linked = db.items.get_item("note_ref", "u1")
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
                "data": {"amount": 1000, "title": "还款", "owner_id": "u1"},
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
            item = db.items.get_item(result["item_id"], "u1")

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
            item = db.items.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert item is not None
            assert item.title == "写项目周报"
            assert item.category == "工作"
            assert item.priority == 2
            assert item.plan_date == "2026-05-01"
            assert item.deadline_at == "2026-05-01T18:00:00"
            assert item.remind_times == ["2026-04-30T09:00:00"]
            assert item.tags == ["周报"]
            assert "分类: 工作" in result["message"]
            assert "优先级" in result["message"]
            assert "提醒: 1 个" in result["message"]
            assert "标签: #周报" in result["message"]
        finally:
            db.cleanup()

    def test_todo_add_session_collects_content_then_finishes_after_plan_date(self, tmp_path, monkeypatch):
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
            monkeypatch.setattr(handler, "_user_local_now", lambda _user_id: datetime(2026, 5, 1, 10, 0))

            context = _Context()
            session = _Session({"step": "title", "data": {}, "group_id": 88})

            result = asyncio.run(handler.handle_session_step("u1", "写周报", session, context))
            assert result["status"] == "success"
            assert session["step"] == "plan_date"
            assert session["data"]["title"] == "写周报"

            result = asyncio.run(handler.handle_session_step("u1", "0", session, context))
            item = db.items.get_item(result["item_id"], "u1")

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
                status=TaskStatus.OPEN,
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
            db.items.insert_item(note, "not12345")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(handler.delete_event("u1", "not12345", SimpleNamespace()))
            preserved = db.items.get_item("not12345", "u1")

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

            saved = db.items.get_item(result["item_id"], "u1")

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
            db.items.insert_item(diary, "dia12345")

            handler = DiaryHandler(db=db, ai_parser=ai_parser)
            result = asyncio.run(
                handler.add_diary("u1", "2026-03-28 晚上玩得很开心", SimpleNamespace(), None)
            )

            original = db.items.get_item("dia12345", "u1")
            created = db.items.get_item(result["item_id"], "u1")
            entries = db.items.query_items_by_date_range(
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

            saved = db.items.get_item(result["item_id"], "u1")

            assert result["status"] == "success"
            assert saved is not None
            assert saved.diary_date == "2026-01-31"
            assert saved.entry_time.startswith("2026-01-31T")
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
            assert db.items.query_items_by_date_range(
                "u1", "diary", "diary_date", "2026-01-31", "2026-01-31"
            ) == []
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
            assert db.items.query_items_by_date_range(
                "u1", "diary", "diary_date", "2026-03-28", "2026-03-28"
            ) == []
        finally:
            db.cleanup()


class TestReminderBackfillRegression:
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

    def test_event_list_handles_timezone_aware_event_start_time(self, tmp_path):
        import sys
        from typing import Any, cast
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_event_aware_start.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="带时区会议",
                start_time="2026-05-01T09:00:00+08:00",
                end_time="2026-05-01T10:00:00+08:00",
                remind_times=["2026-05-01T08:30:00+08:00"],
                created_at="2026-05-01T00:00:00",
                updated_at="2026-05-01T00:00:00",
            )
            db.items.insert_item(event, "evtaware")
            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            result = asyncio.run(
                handler.list_events("u1", "2026", cast(Any, SimpleNamespace()))
            )

            assert result["status"] == "success"
            assert "带时区会议" in result["message"]
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

        from plugins.pendo.commands.operations import handle_confirm
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database
        from plugins.pendo.services.reminder import ReminderService

        db = Database(str(tmp_path / "pendo.db"))

        try:
            event = EventItem(
                owner_id="u1",
                title="提醒测试",
                start_time="2020-01-01T10:00:00",
                remind_times=["2020-01-01T08:00:00", "2020-01-01T09:00:00"],
                created_at="2020-01-01T00:00:00",
                updated_at="2020-01-01T00:00:00",
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
                    ("evt123", "2020-01-01T08:00:00", "2020-01-01T08:00:05"),
                )
                cursor.execute(
                    """
                    INSERT INTO reminder_logs (item_id, remind_time, sent_at)
                    VALUES (?, ?, ?)
                    """,
                    ("evt123", "2020-01-01T09:00:00", "2020-01-01T09:00:05"),
                )

            result = asyncio.run(handle_confirm("u1", "evt123", ReminderService(db), db))

            assert result["status"] == "success"
            logs = db.items.get_reminder_logs("evt123")
            confirmed_logs = [log for log in logs if log["confirmed_at"]]
            pending_logs = [log for log in logs if not log["confirmed_at"]]

            assert [log["remind_time"] for log in confirmed_logs] == ["2020-01-01T09:00:00"]
            assert [log["remind_time"] for log in pending_logs] == ["2020-01-01T08:00:00"]
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

    def test_generate_daily_briefing_includes_today_multi_node_leaf_events(self, tmp_path, monkeypatch):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        fixed_now = datetime.fromisoformat("2030-01-02T08:00:00+08:00")
        monkeypatch.setattr(
            scheduled_module, "now_in_timezone", lambda user_id=None, db=None: fixed_now
        )

        db = Database(str(tmp_path / "pendo_briefing_milestone.db"))

        try:
            db.items.create_event_collection({
                "id": "milebrief",
                "owner_id": "u1",
                "kind": "multi_node",
                "title": "学术会议",
                "category": "未分类",
                "start_time": "2030-01-01T09:00:00",
                "end_time": "2030-01-03T18:00:00",
            })
            event = EventItem(
                owner_id="u1",
                title="主会场报告",
                start_time="2030-01-02T10:30:00",
                remind_times=[],
                event_role="multi_node_child",
                event_collection_id="milebrief",
                event_collection_kind="multi_node",
                event_index=2,
                event_node_key="m02",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.items.insert_item(event, "milebrief_m02")

            briefing = asyncio.run(
                scheduled_module._generate_briefing_content("u1", db, MagicMock())
            )

            assert "🗓️ **今日日程**" in briefing
            assert "10:30 学术会议 · 主会场报告" in briefing
            assert "报到" not in briefing
            assert "闭幕" not in briefing
            assert "今日暂无日程安排" not in briefing
        finally:
            db.cleanup()

    def test_migrate_todos_returns_messages_without_send_action(self, monkeypatch):
        import sys
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
        with open(plugin_json_path, encoding="utf-8") as f:
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

        generate_calls = []

        async def fake_generate_summary(*_args, **_kwargs):
            generate_calls.append(_args)
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
        assert generate_calls == [
            (db, "1001", "2029-12-31", "2030-01-06", "12/31 - 01/06", "📆 本周财务总结")
        ]

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

        generate_calls = []

        async def fake_generate_summary(*_args, **_kwargs):
            generate_calls.append(_args)
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
        assert generate_calls == [
            (db, "1001", "2030-03-01", "2030-03-31", "2030/03/01 - 2030/03/31", "🧾 月底财务总结")
        ]

    def test_finance_summary_uses_amount_cents_and_ledger_date_range(self):
        import shutil
        import sys
        import uuid

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.services.db import Database

        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_finance_summary_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        db = Database(str(temp_dir / "pendo.db"))
        owner_id = "u-finance-summary"

        try:
            db.insert_item({
                "id": "sum_expense",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount_cents": 12345,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-05-02",
                "account_name": "微信",
            })
            db.insert_item({
                "id": "sum_income",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "工资",
                "amount_cents": 500000,
                "transaction_type": "income",
                "ledger_category": "工资",
                "ledger_date": "2026-05-03",
                "account_name": "招商银行卡",
            })
            db.insert_item({
                "id": "sum_transfer",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "转入储蓄",
                "amount_cents": 20000,
                "transaction_type": "transfer",
                "ledger_category": "转账",
                "ledger_date": "2026-05-04",
                "account_name": "招商银行卡",
                "counter_account_name": "储蓄卡",
            })
            db.insert_item({
                "id": "sum_outside",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "范围外支出",
                "amount_cents": 999999,
                "transaction_type": "expense",
                "ledger_category": "测试",
                "ledger_date": "2026-06-01",
                "account_name": "微信",
            })
            conn = db.get_connection()
            with conn:
                conn.execute(
                    "UPDATE items SET amount = 0 WHERE id IN (?, ?)",
                    ("sum_expense", "sum_income"),
                )
            db.cache_clear()

            summary = asyncio.run(
                scheduled_module._generate_finance_summary_content(
                    db,
                    owner_id,
                    "2026-05-01",
                    "2026-05-31",
                    "2026/05/01 - 2026/05/31",
                    "测试财务总结",
                )
            )

            assert "🧾 共 3 笔流水" in summary
            assert "💰 收入: ¥5000.00" in summary
            assert "💸 支出: ¥123.45" in summary
            assert "📊 结余: +¥4876.55" in summary
            assert "🔁 转账: ¥200.00" in summary
            assert "📂 最大支出分类: 餐饮 ¥123.45" in summary
            assert "📥 主要收入来源: 工资 ¥5000.00" in summary
            assert "🔥 最大单笔支出: 午饭 ¥123.45 (2026-05-02)" in summary
            assert "账户收支:" in summary
            assert "招商银行卡 收入¥5000.00 支出¥0.00 净额+¥5000.00" in summary
            assert "微信 收入¥0.00 支出¥123.45 净额¥-123.45" in summary
            assert "转账流向:" in summary
            assert "招商银行卡 → 储蓄卡 ¥200.00" in summary
            assert "范围外支出" not in summary
        finally:
            db.cleanup()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_scheduled_private_send_skips_non_numeric_owner_ids(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        messages = []
        result = asyncio.run(
            scheduled_module._send_private_or_collect(
                SimpleNamespace(),
                messages,
                "demo_web_TEST",
                "测试消息",
            )
        )

        assert result is False
        assert messages == []


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
    def test_pendo_manifest_does_not_claim_bare_biji_trigger(self):
        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        triggers = config["commands"][0]["triggers"]
        assert "笔记" not in triggers


class TestSessionRegression:
    def test_group_private_reply_scope_is_marked_before_routing(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo import main as pendo_main

        seen = []

        class _Router:
            alias_map = {}

            async def route(self, subcommand, user_id, rest_args, context):
                seen.append(getattr(context, "_pendo_reply_private", None))
                return {"status": "success", "message": "ok"}

        async def _fake_privacy_mode(user_id, context):
            return True

        monkeypatch.setattr(pendo_main, "_build_command_router", lambda context, group_id=None: _Router())
        monkeypatch.setattr(pendo_main, "_get_user_privacy_mode", _fake_privacy_mode)

        result = asyncio.run(
            pendo_main._handle_command_routing(
                "1001",
                "todo add",
                SimpleNamespace(metrics=None),
                group_id=2002,
            )
        )

        assert seen == [True]
        assert result[0]["data"]["text"] == "✅ 已发送私聊 (保护隐私)"

    def test_group_private_todo_add_creates_private_session_for_continuation(self, monkeypatch):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from core.session import SessionManager
        from plugins.pendo.config import PendoConfig
        from plugins.pendo.handlers.task import TaskHandler

        manager = SessionManager()

        class _Context:
            plugin_name = "pendo"

            def __init__(self, group_id):
                self.current_user_id = 1001
                self.current_group_id = group_id
                self.session_manager = manager

            async def create_session(self, initial_data=None, timeout=300.0):
                return await manager.create(
                    user_id=self.current_user_id,
                    group_id=self.current_group_id,
                    plugin_name=self.plugin_name,
                    initial_data=initial_data,
                    timeout=timeout,
                )

        handler = TaskHandler(db=MagicMock())
        monkeypatch.setattr(handler, "_user_local_now", lambda _user_id: datetime(2026, 5, 1, 10, 0))

        group_context = _Context(group_id=2002)
        group_context._pendo_reply_private = True
        result = asyncio.run(handler.handle("1001", "add", group_context, group_id=2002))

        assert result["status"] == "success"
        assert "开始添加待办" in result["message"]
        assert asyncio.run(manager.get(1001, 2002)) is None

        private_session = asyncio.run(manager.get(1001, None))
        assert private_session is not None
        assert private_session.plugin_name == "pendo"
        assert private_session.get("type") == PendoConfig.SESSION_TYPE_TASK_ADD
        assert private_session.get("group_id") == 2002

        private_context = _Context(group_id=None)
        next_result = asyncio.run(
            handler.handle_session_step("1001", "写周报", private_session, private_context)
        )

        assert next_result["status"] == "success"
        assert private_session.get("step") == "plan_date"
        assert private_session.get("data")["title"] == "写周报"

    def test_group_todo_add_keeps_group_session_when_reply_is_not_private(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from core.session import SessionManager
        from plugins.pendo.handlers.task import TaskHandler

        manager = SessionManager()

        class _Context:
            plugin_name = "pendo"
            current_user_id = 1001
            current_group_id = 2002
            session_manager = manager

            async def create_session(self, initial_data=None, timeout=300.0):
                return await manager.create(
                    user_id=self.current_user_id,
                    group_id=self.current_group_id,
                    plugin_name=self.plugin_name,
                    initial_data=initial_data,
                    timeout=timeout,
                )

        handler = TaskHandler(db=MagicMock())
        result = asyncio.run(handler.handle("1001", "add", _Context(), group_id=2002))

        assert result["status"] == "success"
        assert asyncio.run(manager.get(1001, None)) is None
        assert asyncio.run(manager.get(1001, 2002)) is not None

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
    def test_confirm_handles_aware_reminder_times(self, monkeypatch):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import operations
        from plugins.pendo.commands.operations import handle_confirm

        fixed_now = datetime.fromisoformat("2030-01-01T09:30:00+08:00")
        monkeypatch.setattr(operations.TimezoneHelper, "now", lambda tz=None: fixed_now)

        db = MagicMock()
        db.items.get_item.return_value = SimpleNamespace(
            title="带时区提醒",
            type="event",
            remind_times=[
                "2030-01-01T08:00:00+08:00",
                "2030-01-01T10:00:00+08:00",
            ],
        )

        reminder_service = MagicMock()
        reminder_service.confirm_reminder.return_value = {"status": "success", "message": "ok"}

        result = asyncio.run(handle_confirm("u1", "evt123", reminder_service, db))

        assert result["status"] == "success"
        assert "后续还有 1 个提醒" in result["message"]
        reminder_service.confirm_reminder.assert_called_once_with("evt123", "confirmed", "u1")

    def test_snooze_keeps_future_aware_reminders(self, monkeypatch):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import operations
        from plugins.pendo.commands.operations import handle_snooze

        fixed_now = datetime.fromisoformat("2030-01-01T09:30:00+08:00")

        monkeypatch.setattr(operations.TimezoneHelper, "now", lambda tz=None: fixed_now)
        monkeypatch.setattr(
            operations,
            "_parse_snooze_time",
            lambda time_arg, base_time=None: "2030-01-01T11:00:00+08:00",
        )

        async def fake_last_sent_remind_time(db, item_id):
            return None

        monkeypatch.setattr(operations, "_get_last_sent_remind_time", fake_last_sent_remind_time)

        db = MagicMock()
        db.items.get_item.return_value = SimpleNamespace(
            title="延后测试",
            type="event",
            remind_times=[
                "2030-01-01T08:00:00+08:00",
                "2030-01-01T10:00:00+08:00",
            ],
        )
        db.items.update_item.return_value = {"status": "success"}

        reminder_service = MagicMock()
        reminder_service.db = db
        reminder_service.confirm_reminder.return_value = {"status": "success", "message": "ok"}

        result = asyncio.run(handle_snooze("u1", "evt123 10m", reminder_service))

        assert result["status"] == "success"
        assert "已将提醒延后到: 2030-01-01T11:00:00+08:00" in result["message"]
        db.items.update_item.assert_called_once()

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

    def test_export_markdown_includes_note_references(self, monkeypatch, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import exporter as exporter_module
        from plugins.pendo.services.exporter import ExporterService

        monkeypatch.setattr(exporter_module, "_get_export_dir", lambda user_id: tmp_path)

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
            def get_items(self, user_id, filters, limit):
                return [note_item] if filters.get("type") == "note" else []

        service = ExporterService(SimpleNamespace(items=_Repo(), log_transfer=lambda **kwargs: 1))
        result = service.export_markdown("u1", "笔记档案 note", {})

        assert result["status"] == "success"
        exported = (tmp_path / "笔记档案.md").read_text(encoding="utf-8")
        assert "**关联条目**" in exported
        assert "- 待办: 整理卡片 (`task1`)" in exported

    def test_export_markdown_requires_filename(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.exporter import ExporterService

        service = ExporterService(SimpleNamespace(items=SimpleNamespace()))
        result = service.export_markdown("u1", "", {})

        assert result["status"] == "error"
        assert "请提供导出文件名" in result["message"]

    def test_export_markdown_uses_event_collection_context(self, monkeypatch, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import exporter as exporter_module
        from plugins.pendo.services.exporter import ExporterService

        monkeypatch.setattr(exporter_module, "_get_export_dir", lambda user_id: tmp_path)

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
            def get_items(self, user_id, filters, limit):
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

        service = ExporterService(SimpleNamespace(items=_Repo(), log_transfer=lambda **kwargs: 1))
        result = service.export_markdown("u1", "日程导出 event", {})

        assert result["status"] == "success"
        exported = (tmp_path / "日程导出.md").read_text(encoding="utf-8")
        assert "### 01. FRB2026会议 · 摘要截止" in exported
        assert "| 分类 | 学术 |" in exported
        assert "| 地点 | 上海 |" in exported
        assert "- 集合标题: FRB2026会议" in exported
        assert "- 集合类型: multi_node" in exported


class TestPendoWebHandler:
    """测试 pendo web 命令格式化与发送行为"""

    def test_web_token_sends_token_as_separate_private_message(self, monkeypatch):
        import importlib
        import sys
        import types

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
        import importlib
        import sys
        import types

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
        import importlib
        import sys
        import types

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

    def test_web_stop_reports_external_running_server_without_failing(self):
        import importlib
        import sys
        import types

        sys.path.insert(0, str(ROOT))
        sys.modules.pop("plugins.pendo.handlers.web", None)
        sys.modules["plugins.pendo.web.server"] = types.SimpleNamespace(
            get_url=lambda: "http://127.0.0.1:8765",
            is_running=lambda: True,
            is_managed_running=lambda: False,
            start=lambda _db: False,
            stop=lambda: False,
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

        sys.path.insert(0, str(ROOT))
        sys.modules.pop("plugins.pendo.handlers.web", None)
        sys.modules["plugins.pendo.web.server"] = types.SimpleNamespace(
            get_url=lambda: "http://127.0.0.1:8765",
            is_running=lambda: True,
            start=lambda _db: True,
            stop=lambda: True,
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")
        monkeypatch.setattr(
            web_module, "generate_widget_token", lambda *_args, **_kwargs: "widget-token"
        )

        actions = []

        async def send_action(action):
            actions.append(action)

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=None)

        result = asyncio.run(handler.handle("1001", "widget-token", context=context))

        assert result["status"] == "success"
        assert "Widget Token 已单独私聊发送" in result["message"]
        assert "widget-token" not in result["message"]
        assert len(actions) == 1
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert "Pendo Web Widget Token" in token_text
        assert "widget-token" in token_text


class TestPendoSearchAndImportRegression:
    def test_search_handler_applies_date_field_for_range_filters(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.search import SearchHandler

        calls = []

        class _ItemsRepo:
            def search_items(self, owner_id, query, filters):
                calls.append((owner_id, query, filters))
                return []

        handler = SearchHandler(SimpleNamespace(items=_ItemsRepo()))
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
            db.get_connection().close()


class TestPendoRedesignRegression:
    def test_widget_ledger_panel_prefers_amount_cents(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.db import Database
        from plugins.pendo.web.api.widget import build_widget_summary

        db = Database(str(tmp_path / "pendo.db"))
        owner_id = "u-widget-ledger"
        try:
            db.insert_item({
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
            })

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
            collection_id = db.create_event_collection({
                "id": "test_collection_category",
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": "TEST_STATS 学术会议",
                "category": "工作",
                "location": "上海",
            })
            db.insert_item({
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
            })

            rows = db.search_items(
                owner_id,
                "学术会议",
                {"type": "event", "category": "工作"},
            )

            assert [row.id for row in rows] == ["test_collection_node"]
        finally:
            db.cleanup()
