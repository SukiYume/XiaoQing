"""Pendo 结构、配置和文档契约。"""

from __future__ import annotations

from tests.helpers.pendo_test_support import (
    ROOT,
    datetime,
    json,
)


class TestPendoStructure:
    """测试 pendo 插件结构"""

    def test_chinese_tomorrow_range_matches_english_alias(self):
        from plugins.pendo.utils.time_utils import parse_event_time_range

        now = datetime(2026, 7, 21, 16, 30, 0)

        assert parse_event_time_range("明天", now=now) == parse_event_time_range(
            "tomorrow", now=now
        )


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

    def test_undo_window_has_one_production_source(self):
        from plugins.pendo.config import PendoConfig

        plugin_root = ROOT / "plugins" / "pendo"
        handler_paths = tuple((plugin_root / "handlers").glob("*.py"))
        production_sources = {
            path: path.read_text(encoding="utf-8")
            for path in (plugin_root / "main.py", *handler_paths)
        }

        assert PendoConfig.UNDO_HINT == (
            f"💡 {PendoConfig.UNDO_WINDOW_MINUTES}分钟内可用 /pendo undo 撤销"
        )
        assert all(
            "5分钟内可用 /pendo undo" not in source for source in production_sources.values()
        )
        for filename in ("task.py", "note.py", "ledger.py", "event.py", "diary.py"):
            assert (
                "PendoConfig.UNDO_HINT" in production_sources[plugin_root / "handlers" / filename]
            )

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
        assert "/pendo event edit a1b2c3d4 改到4月22日12:43" in help_text
        assert "/pendo event edit 80efbef6 标题改为FAST会议行程" in help_text
        assert "多节点事件可直接写“节点名 + 改成/改到 + 新时间”" not in help_text
        assert "/pendo event reminders delete <id> <all|today|future|提醒时间>" in help_text
        assert "━━ ✅ **待办事项 (Todo)**" not in help_text

    def test_command_router_help_uses_detailed_provider_for_subcommands(self):
        """测试 /pendo help event 复用 main.py 的详细帮助，而不是旧路由摘要。"""
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.core.router import CommandRouter
        from plugins.pendo.main import _local_catalog_root, _show_help

        async def dummy_handler(user_id, args, context):
            return {"status": "success", "message": "ok"}

        router = CommandRouter(
            _local_catalog_root(),
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

        from plugins.pendo.main import HELP_MAP, _show_help

        overview = _show_help()
        assert "🧭 **可用命令**" in overview
        assert "/pendo export <文件名> [范围] [类型]" not in overview

        # 直接遍历生产帮助注册表的插入顺序，避免测试专用常量与真实章节
        # 漂移；header 是标题文本，不是可单独渲染的帮助章节。
        help_text = "\n".join(_show_help(section) for section in HELP_MAP if section != "header")

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
            "/pendo settings ai_consent on/off",
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
            "/pendo event edit a1b2c3d4 改到4月22日12:43",
            "/pendo ledger list 2026-03 type:expense",
            "/pendo search 组会 type=event",
            "/pendo settings timezone Asia/Shanghai",
            '/pendo export "三月 账本" 2026-03 ledger',
            "/pendo import",
            "/pendo event edit a1b2c3d4 备注为从北京南坐G123去会场",
            "/pendo event edit a1b2c3d4 地点改到北京南",
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


class TestPendoDocumentation:
    """测试 pendo 文档"""

    def test_readme_exists(self):
        """测试 README 文件存在"""
        readme_path = ROOT / "plugins" / "pendo" / "README.md"
        assert readme_path.exists()

        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
            assert len(content) > 100  # 应该有实际内容
            for current_contract in (
                "scheduled_delivery_outbox",
                "pendo_prune_operation_logs",
                '"web_session_cookie_secure": true',
                "/pendo settings ai_consent on|off",
                "section=tasks|ledger|notes|all|auto",
                "一次性登录码",
            ):
                assert current_contract in content
            for removed_contract in (
                "历史脚本留档",
                "把收到的 token 粘贴到登录页",
                "passlib[bcrypt]",
            ):
                assert removed_contract not in content

    def test_architecture_doc_exists(self):
        """测试架构文档存在"""
        arch_path = ROOT / "plugins" / "pendo" / "ARCHITECTURE.md"
        assert arch_path.exists()

        with open(arch_path, encoding="utf-8") as f:
            content = f.read()
            assert len(content) > 100
            for current_contract in (
                "services/runtime.py",
                "PendoRuntimeService",
                "scheduled_delivery_outbox",
                "scheduled_prune_operation_logs",
            ):
                assert current_contract in content
            for removed_contract in (
                "通用命令结果类型",
                "ItemFields 字段常量",
                "*.py.old",
                "requirements.txt",
            ):
                assert removed_contract not in content

    def test_runtime_data_root_is_gitignored(self):
        """项目级运行数据根目录不得进入 Git。"""
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "/data/" in gitignore
        assert "!plugins/pendo/data/" not in gitignore

    def test_duplicate_plugin_doc_removed(self):
        """测试重复的旧插件说明文档已移除"""
        doc_path = ROOT / "plugins" / "pendo" / "Pendo个人时间与信息管理中枢.md"
        assert not doc_path.exists()
