"""
CommandRouter 单元测试
"""

from typing import Any

import pytest

from core.router import (
    CommandCatalogNode,
    CommandConflictError,
    CommandRouter,
    CommandSpec,
    resolve_catalog_invocation,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def router() -> CommandRouter:
    """创建空的路由器"""
    return CommandRouter()


@pytest.fixture
def empty_router() -> CommandRouter:
    """创建空的路由器（edge case 测试用）"""
    return CommandRouter()


async def dummy_handler(
    command: str, args: str, event: dict[str, Any], context
) -> list[dict[str, Any]]:
    """测试用的空处理函数"""
    return [{"type": "text", "data": {"text": f"{command}: {args}"}}]


def make_spec(
    name: str,
    triggers: list[str],
    plugin: str      = "test",
    priority: int    = 0,
    admin_only: bool = False,
) -> CommandSpec:
    """创建测试用的 CommandSpec"""
    return CommandSpec(
        plugin     = plugin,
        name       = name,
        triggers   = triggers,
        help_text  = f"Help for {name}",
        admin_only = admin_only,
        handler    = dummy_handler,
        priority   = priority,
    )


# ============================================================
# 测试用例
# ============================================================


class TestCommandRouter:
    """CommandRouter 测试类"""

    def test_register_and_resolve_basic(self, router: CommandRouter):
        """测试基本的注册和解析"""
        spec = make_spec("echo", ["echo", "回显"])
        router.register(spec)

        result = router.resolve("echo hello world")
        assert result is not None
        assert result[0].name == "echo"
        assert result[1] == "hello world"

    def test_resolve_with_chinese_trigger(self, router: CommandRouter):
        """测试中文触发词"""
        spec = make_spec("echo", ["echo", "回显"])
        router.register(spec)

        result = router.resolve("回显 测试内容")
        assert result is not None
        assert result[0].name == "echo"
        assert result[1] == "测试内容"

    def test_resolve_no_match(self, router: CommandRouter):
        """测试无匹配命令"""
        spec = make_spec("echo", ["echo"])
        router.register(spec)

        result = router.resolve("unknown command")
        assert result is None

    def test_resolve_empty_args(self, router: CommandRouter):
        """测试无参数命令"""
        spec = make_spec("help", ["help", "帮助"])
        router.register(spec)

        result = router.resolve("help")
        assert result is not None
        assert result[0].name == "help"
        assert result[1] == ""

    def test_longer_trigger_priority(self, router: CommandRouter):
        """测试长触发词优先匹配（同优先级下）"""
        spec1 = make_spec("dict", ["dict", "字典"])
        spec2 = make_spec("dict_add", ["dict_add", "字典添加"])
        router.register(spec1)
        router.register(spec2)

        # "dict_add xxx" 应该匹配 dict_add 而不是 dict
        result = router.resolve("dict_add new_word")
        assert result is not None
        assert result[0].name == "dict_add"
        assert result[1] == "new_word"

    def test_explicit_priority(self, router: CommandRouter):
        """测试显式优先级"""
        # 低优先级但触发词更长
        spec1 = make_spec("long_command", ["command"], priority=0)
        # 高优先级但触发词短
        spec2 = make_spec("short_cmd", ["cmd"], priority=10)
        router.register(spec1)
        router.register(spec2)

        # 高优先级的 "cmd" 应该先匹配（尽管 "command" 也匹配）
        result = router.resolve("cmd test")
        assert result is not None
        # priority=10 的 short_cmd 应该胜出
        assert result[0].name == "short_cmd"

    def test_clear_plugin(self, router: CommandRouter):
        """测试清除插件命令"""
        spec1 = make_spec("cmd1", ["cmd1"], plugin="plugin_a")
        spec2 = make_spec("cmd2", ["cmd2"], plugin="plugin_b")
        router.register(spec1)
        router.register(spec2)

        # 清除 plugin_a 的命令
        router.clear_plugin("plugin_a")

        assert router.resolve("cmd1") is None
        assert router.resolve("cmd2") is not None

    def test_replace_plugin_swaps_only_one_complete_generation(
        self,
        router: CommandRouter,
    ):
        old_first = make_spec("old_first", ["old_first"], plugin="plugin_a")
        old_second = make_spec("old_second", ["old_second"], plugin="plugin_a")
        unrelated = make_spec("other", ["other"], plugin="plugin_b")
        replacement = make_spec("new", ["new"], plugin="plugin_a")
        router.register(old_first)
        router.register(unrelated)
        router.register(old_second)

        previous = router.replace_plugin("plugin_a", [replacement])

        assert previous == (old_first, old_second)
        assert router.resolve("old_first") is None
        assert router.resolve("old_second") is None
        assert router.resolve("new") == (replacement, "")
        assert router.resolve("other") == (unrelated, "")

    def test_register_rejects_equal_priority_trigger_conflict_without_mutation(
        self,
        router: CommandRouter,
    ):
        first = make_spec("first", ["exec"], plugin="plugin_a", priority=10)
        second = make_spec("second", ["exec"], plugin="plugin_b", priority=10)
        router.register(first)

        with pytest.raises(CommandConflictError, match="plugin_a.first conflicts") as raised:
            router.register(second)

        assert raised.value.trigger == "exec"
        assert raised.value.priority == 10
        assert router.resolve("exec") == (first, "")

    def test_replace_rejects_conflict_and_preserves_previous_generation(
        self,
        router: CommandRouter,
    ):
        existing = make_spec("existing", ["exec"], plugin="plugin_a", priority=10)
        old = make_spec("old", ["old"], plugin="plugin_b", priority=10)
        conflicting = make_spec("new", ["exec"], plugin="plugin_b", priority=10)
        router.register(existing)
        router.register(old)

        with pytest.raises(CommandConflictError, match="plugin_a.existing conflicts"):
            router.replace_plugin("plugin_b", [conflicting])

        assert router.resolve("exec") == (existing, "")
        assert router.resolve("old") == (old, "")

    def test_explicit_distinct_priorities_resolve_duplicate_trigger(self, router: CommandRouter):
        lower = make_spec("lower", ["exec"], plugin="plugin_a", priority=10)
        higher = make_spec("higher", ["exec"], plugin="plugin_b", priority=20)

        router.register(lower)
        router.register(higher)

        assert router.resolve("exec") == (higher, "")

    def test_get_command_catalog_builds_structured_fallback(self, router: CommandRouter):
        spec1 = make_spec("echo", ["echo", "回显"])
        spec2 = make_spec("help", ["help"])
        router.register(spec1)
        router.register(spec2)

        catalog = router.get_command_catalog()

        assert tuple(node.code for node in catalog) == ("test.echo", "test.help")
        assert catalog[0].aliases == ("回显",)
        assert catalog[0].usage == "/echo"

    def test_get_command_catalog_preserves_explicit_recursive_catalog(
        self,
        router: CommandRouter,
    ):
        spec       = make_spec("echo", ["echo"])
        spec.usage = "/echo <text>"
        child      = CommandCatalogNode(
            code      = "test.echo.raw",
            plugin    = "test",
            path      = ("echo", "raw"),
            name      = "raw",
            aliases   = ("原样",),
            help_text = "原样回显",
            usage     = "/echo raw <text>",
        )
        spec.catalog = CommandCatalogNode(
            code      = "test.echo",
            plugin    = "test",
            path      = ("echo",),
            name      = "echo",
            aliases   = ("回显",),
            help_text = "回显消息",
            usage     = "/echo <text>",
            children  = (child,),
        )
        router.register(spec)

        catalog = router.get_command_catalog()

        assert catalog == (spec.catalog,)
        assert tuple(node.code for node in catalog[0].walk()) == (
            "test.echo",
            "test.echo.raw",
        )
        assert catalog[0].to_dict()["subcommands"][0]["aliases"] == ["原样"]

    def test_admin_only_flag(self, router: CommandRouter):
        """测试管理员命令标记"""
        spec = make_spec("reload", ["reload"], admin_only=True)
        router.register(spec)

        result = router.resolve("reload plugins")
        assert result is not None
        assert result[0].admin_only is True

    def test_multiple_commands_same_plugin(self, router: CommandRouter):
        """测试同一插件注册多个命令"""
        spec1 = make_spec("cmd1", ["cmd1"], plugin="multi")
        spec2 = make_spec("cmd2", ["cmd2"], plugin="multi")
        spec3 = make_spec("cmd3", ["cmd3"], plugin="multi")
        router.register(spec1)
        router.register(spec2)
        router.register(spec3)

        assert router.resolve("cmd1") is not None
        assert router.resolve("cmd2") is not None
        assert router.resolve("cmd3") is not None

        # 清除后应该全部失效
        router.clear_plugin("multi")
        assert router.resolve("cmd1") is None
        assert router.resolve("cmd2") is None
        assert router.resolve("cmd3") is None

    def test_catalog_invocation_uses_longest_recursive_alias_path(self):
        confirm = CommandCatalogNode(
            code      = "pendo.pendo.event.reminders.confirm",
            plugin    = "pendo",
            path      = ("pendo", "event", "reminders", "confirm"),
            name      = "confirm",
            aliases   = ("确认",),
            help_text = "确认提醒",
            usage     = "/pendo event reminders confirm <id>",
        )
        reminders = CommandCatalogNode(
            code      = "pendo.pendo.event.reminders",
            plugin    = "pendo",
            path      = ("pendo", "event", "reminders"),
            name      = "reminders",
            aliases   = ("提醒",),
            help_text = "管理提醒",
            usage     = "/pendo event reminders <操作>",
            children  = (confirm,),
        )
        event = CommandCatalogNode(
            code      = "pendo.pendo.event",
            plugin    = "pendo",
            path      = ("pendo", "event"),
            name      = "event",
            aliases   = ("日程",),
            help_text = "管理日程",
            usage     = "/pendo event <操作>",
            children  = (reminders,),
        )
        root = CommandCatalogNode(
            code      = "pendo.pendo",
            plugin    = "pendo",
            path      = ("pendo",),
            name      = "pendo",
            aliases   = (),
            help_text = "个人管理",
            usage     = "/pendo <命令>",
            children  = (event,),
        )

        invocation = resolve_catalog_invocation(root, "日程 提醒 确认 abc123 today")

        assert invocation.node is confirm
        assert tuple(node.code for node in invocation.chain) == (
            "pendo.pendo",
            "pendo.pendo.event",
            "pendo.pendo.event.reminders",
            "pendo.pendo.event.reminders.confirm",
        )
        assert invocation.arguments == "abc123 today"
        assert invocation.remainder_after(1) == "提醒 确认 abc123 today"

    def test_catalog_invocation_stops_before_dynamic_business_arguments(self):
        child = CommandCatalogNode(
            code       = "chat.chat.help",
            plugin     = "chat",
            path       = ("chat", "help"),
            name       = "help",
            aliases    = ("帮助",),
            help_text  = "帮助",
            usage      = "/chat help",
            match_mode = "exact",
        )
        root = CommandCatalogNode(
            code      = "chat.chat",
            plugin    = "chat",
            path      = ("chat",),
            name      = "chat",
            aliases   = (),
            help_text = "聊天",
            usage     = "/chat <内容>",
            children  = (child,),
        )

        invocation = resolve_catalog_invocation(root, "help me write code")

        assert invocation.node is root
        assert invocation.arguments == "help me write code"


class TestRouterEdgeCases:
    """测试路由器边界情况"""

    def test_empty_trigger_match(self, empty_router):
        """测试空触发词匹配"""
        from core.router import CommandSpec

        async def handler(*args):
            return [{"type": "text", "data": {"text": "ok"}}]

        # 空触发词会被 resolve 跳过（见 router.py:52-53）
        spec = CommandSpec(
            plugin     = "test",
            name       = "test",
            triggers   = [""],
            help_text  = "test",
            admin_only = False,
            handler    = handler,
            priority   = 0,
        )
        empty_router.register(spec)

        # 空触发词应该被跳过，不匹配任何内容
        result = empty_router.resolve("")
        assert result is None

        # 空触发词也不应该匹配非空输入
        result = empty_router.resolve("test")
        assert result is None

    def test_special_char_triggers(self, empty_router):
        """测试特殊字符触发词"""
        from core.router import CommandSpec

        async def handler(*args):
            return []

        # 测试正则特殊字符 - 但路由器是字符串前缀匹配，不是正则
        # 这些特殊字符作为普通字符串处理
        special_triggers = [r"test!", "命令.", "d+"]

        for trigger in special_triggers:
            spec = CommandSpec(
                plugin     = "test",
                name       = f"test_{len(trigger)}",
                triggers   = [trigger],
                admin_only = False,
                handler    = handler,
                priority   = 0,
                help_text  = f"test {trigger}",
            )
            empty_router.register(spec)

        # 验证特殊字符能正常匹配
        result = empty_router.resolve("test! args")
        assert result is not None
        assert result[1] == "args"

        # 中文句号触发词
        result = empty_router.resolve("命令. 参数")
        assert result is not None
        assert result[1] == "参数"

        # d+ 触发词
        result = empty_router.resolve("d+ more")
        assert result is not None
        assert result[1] == "more"

    def test_priority_conflict_resolution(self, empty_router):
        """测试优先级冲突解决"""
        from core.router import CommandSpec

        call_order = []

        async def handler_high(*args):
            call_order.append("high")
            return []

        async def handler_low(*args):
            call_order.append("low")
            return []

        # 注册相同触发词，不同优先级
        empty_router.register(
            CommandSpec(
                plugin     = "test",
                name       = "low",
                triggers   = ["test"],
                admin_only = False,
                handler    = handler_low,
                priority   = 0,
                help_text  = "low",
            )
        )
        empty_router.register(
            CommandSpec(
                plugin     = "test",
                name       = "high",
                triggers   = ["test"],
                admin_only = False,
                handler    = handler_high,
                priority   = 10,
                help_text  = "high",
            )
        )

        # 解析应该返回高优先级的 spec
        result = empty_router.resolve("test args")
        assert result is not None
        spec, args = result
        assert spec.priority == 10
        assert spec.name == "high"
        assert args == "args"
