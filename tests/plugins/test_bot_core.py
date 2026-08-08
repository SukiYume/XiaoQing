"""bot_core 插件单元测试。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.interfaces import PluginCapabilities, PluginPrincipal
from core.router import CommandCatalogNode
from plugins.bot_core import main as bot_core

ROOT = Path(__file__).resolve().parent.parent.parent


def _catalog_node(
    code: str,
    plugin: str,
    path: tuple[str, ...],
    help_text: str,
    usage: str,
    *,
    aliases: tuple[str, ...] = (),
    children: tuple[CommandCatalogNode, ...] = (),
    permission: str = "public",
    contexts: tuple[str, ...] = ("private", "group"),
    examples: tuple[str, ...] = (),
    invalid_examples: tuple[str, ...] = (),
) -> CommandCatalogNode:
    return CommandCatalogNode(
        code=code,
        plugin=plugin,
        path=path,
        name=path[-1],
        aliases=aliases,
        help_text=help_text,
        usage=usage,
        permission=permission,
        contexts=contexts,
        examples=examples,
        invalid_examples=invalid_examples,
        children=children,
    )


def _sample_catalog() -> tuple[CommandCatalogNode, ...]:
    help_search = _catalog_node(
        "bot_core.help.search",
        "bot_core",
        ("help", "search"),
        "搜索命令",
        "/help search <关键词>",
        aliases=("find", "搜索"),
        examples=("/help search 提醒",),
        invalid_examples=("/help search",),
    )
    return (
        _catalog_node(
            "bot_core.help",
            "bot_core",
            ("help",),
            "查看完整命令目录",
            "/help [关键词]",
            aliases=("h", "帮助"),
            children=(help_search,),
        ),
        _catalog_node(
            "bot_core.reload",
            "bot_core",
            ("reload",),
            "热重载配置和插件",
            "/reload",
            aliases=("重载",),
        ),
        _catalog_node(
            "chat.chat",
            "chat",
            ("chat",),
            "与 AI 对话",
            "/chat <消息>",
            aliases=("gpt",),
        ),
    )


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_context():
    """模拟插件上下文"""

    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "bot_core"
            self.data_dir = self.plugin_dir / "data"
            self.secrets = {"plugins": {"bot_core": {}}}
            self.config_manager = None
            self.principal = PluginPrincipal(
                kind="user",
                user_id=12345,
                is_bot_admin=True,
                is_private=True,
            )
            secret_admin = MagicMock()
            values = {
                "plugins": {
                    "signin": {
                        "yingshijufeng": {"sid": "test_sid_12345"},
                    },
                },
                "admin_user_ids": [12345, 67890],
            }

            def get_secret(path):
                current = values
                for part in path.split("."):
                    if not isinstance(current, dict) or part not in current:
                        raise KeyError(path)
                    current = current[part]
                return current

            secret_admin.get.side_effect = get_secret
            self.capabilities = PluginCapabilities(
                is_bot_admin=True,
                secret_admin=secret_admin,
            )
            self.mute_group = MagicMock()
            self.unmute_group = MagicMock(return_value=True)
            self.get_mute_remaining = MagicMock(return_value=0)

        def get_command_catalog(self):
            """返回 Core 发布的结构化命令快照。"""

            return _sample_catalog()

        def list_plugins(self):
            """返回插件列表"""
            return ["bot_core", "chat", "echo", "choice"]

        def reload_config(self):
            """重载配置"""

        def reload_plugins(self):
            """重载插件"""

    return MockContext()


@pytest.fixture
def mock_event():
    """模拟群消息事件"""
    return {"user_id": 12345, "group_id": 54321, "message": "test", "message_type": "group"}


@pytest.fixture
def mock_private_event():
    """模拟私聊消息事件"""
    return {"user_id": 12345, "message": "test", "message_type": "private"}


@pytest.fixture
def mock_context_with_mute():
    """模拟有静音状态的上下文"""

    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "bot_core"
            self.data_dir = self.plugin_dir / "data"
            self.get_mute_remaining = MagicMock(return_value=5.0)
            self.unmute_group = MagicMock(return_value=True)

        def get_command_catalog(self):
            return ()

    return MockContext()


@pytest.fixture
def mock_context_with_metrics():
    """模拟带metrics的上下文"""

    class MockMetrics:
        async def get_summary(self):
            return {
                "uptime_seconds": 3600,
                "global": {
                    "total_calls": 1000,
                    "success_rate": 0.95,
                    "avg_time": 0.123,
                    "slow_calls": 5,
                    "errors": 50,
                },
                "top_slow_plugins": [
                    {"plugin": "xiaoqing_chat", "avg_time": 1.5},
                    {"plugin": "arxiv_filter", "avg_time": 0.8},
                ],
            }

    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "bot_core"
            self.data_dir = self.plugin_dir / "data"
            self.metrics = MockMetrics()

        def get_command_catalog(self):
            return ()

    return MockContext()


# ============================================================
# Test Help Command
# ============================================================


class TestHelpCommand:
    """测试帮助命令"""

    @pytest.mark.asyncio
    async def test_help_all_commands(self, mock_context):
        """默认帮助只显示插件级功能导航。"""
        result = await bot_core.handle("help", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = result[0]["data"]["text"]
        assert "XiaoQing 功能导航" in result_text
        assert "bot_core（Core）" in result_text
        assert "/help" in result_text
        assert "/reload" in result_text
        assert "chat" in result_text
        assert "查看插件：/help pendo" in result_text
        assert "code:" not in result_text
        assert "bot_core.help.search" not in result_text

    @pytest.mark.asyncio
    async def test_help_with_keyword(self, mock_context):
        """测试带关键词的帮助搜索"""
        result = await bot_core.handle("help", "reload", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该包含搜索结果
        assert "reload" in result_text.lower() or "重载" in result_text

    @pytest.mark.asyncio
    async def test_help_with_plugin_keyword(self, mock_context):
        """测试搜索插件关键词"""
        result = await bot_core.handle("help", "bot_core", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = result[0]["data"]["text"]
        assert "📦 bot_core" in result_text
        assert "/help search" in result_text
        assert "/reload" in result_text
        assert "bot_core.help" not in result_text
        assert "正确示例" not in result_text

    @pytest.mark.asyncio
    async def test_help_exact_leaf_shows_metadata_only_on_detail_page(self, mock_context):
        result = await bot_core.handle("help", "help search", {}, mock_context)

        result_text = result[0]["data"]["text"]
        assert result_text.startswith("📘 命令详情")
        assert "/help search <关键词>" in result_text
        assert "✓ /help search 提醒" in result_text
        assert "✗ /help search" in result_text
        assert "命令码：bot_core.help.search" in result_text
        assert "返回上级：/help help" in result_text

    @pytest.mark.asyncio
    async def test_help_json_without_query_keeps_complete_flat_catalog(self, mock_context):
        """自动化依赖的 JSON 全量目录不随人类首页分层而改变。"""

        result = await bot_core.handle("help", "json page 1", {}, mock_context)
        payload = json.loads(result[0]["data"]["text"])

        assert [command["code"] for command in payload["commands"]] == [
            "bot_core.help",
            "bot_core.help.search",
            "bot_core.reload",
            "chat.chat",
        ]

    @pytest.mark.asyncio
    async def test_help_with_no_results(self, mock_context):
        """测试搜索无结果的情况"""
        result = await bot_core.handle("help", "nonexistent_keyword_xyz", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未找到" in result_text or "不存在" in result_text

    @pytest.mark.asyncio
    async def test_help_empty_commands(self):
        """测试空命令列表的情况"""

        class EmptyContext:
            def get_command_catalog(self):
                return ()

        result = await bot_core.handle("help", "", {}, EmptyContext())
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "暂无命令" in result_text or "命令" in result_text


# ============================================================
# Test Reload Command
# ============================================================


class TestReloadCommand:
    """测试重载命令"""

    @pytest.mark.asyncio
    async def test_reload_success(self, mock_context):
        """插件重载必须留在后台，不能阻塞仍占用 bot_core 执行门的命令。"""
        pending_reload = asyncio.get_running_loop().create_future()
        mock_context.reload_config = MagicMock()
        mock_context.reload_plugins = MagicMock(return_value=pending_reload)

        try:
            result = await asyncio.wait_for(
                bot_core.handle("reload", "", {}, mock_context),
                timeout=0.1,
            )

            assert result is not None
            assert len(result) > 0
            assert "插件正在后台重载" in str(result)
            assert not pending_reload.done()
            mock_context.reload_config.assert_called_once_with()
            mock_context.reload_plugins.assert_called_once_with()
        finally:
            pending_reload.cancel()

    @pytest.mark.asyncio
    async def test_reload_with_error(self):
        """测试重载时出错"""

        class ErrorContext:
            def reload_config(self):
                raise RuntimeError("Config reload failed")

        result = await bot_core.handle("reload", "", {}, ErrorContext())
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "失败" in result_text or "错误" in result_text or "❌" in result_text


# ============================================================
# Test Plugins Command
# ============================================================


class TestPluginsCommand:
    """测试插件列表命令"""

    @pytest.mark.asyncio
    async def test_plugins_list(self, mock_context):
        """测试显示插件列表"""
        result = await bot_core.handle("plugins", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该包含插件列表信息
        assert "插件" in result_text or "plugin" in result_text.lower()

    @pytest.mark.asyncio
    async def test_plugins_empty_list(self):
        """测试空插件列表"""

        class EmptyContext:
            def list_plugins(self):
                return []

        result = await bot_core.handle("plugins", "", {}, EmptyContext())
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "暂无插件" in result_text or "插件" in result_text


# ============================================================
# Test Mute Command
# ============================================================


class TestMuteCommand:
    """测试静音命令"""

    @pytest.mark.asyncio
    async def test_mute_default_duration(self, mock_context, mock_event):
        """测试默认静音时长"""
        result = await bot_core.handle("闭嘴", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该确认静音，并显示时长
        assert "安静" in result_text or "静音" in result_text or "🤐" in result_text
        mock_context.mute_group.assert_called_once_with(54321, bot_core.DEFAULT_MUTE_MINUTES)

    @pytest.mark.asyncio
    async def test_mute_custom_minutes(self, mock_context, mock_event):
        """测试自定义分钟数"""
        result = await bot_core.handle("闭嘴", "30", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该显示30分钟
        assert "30" in result_text
        mock_context.mute_group.assert_called_once_with(54321, 30)

    @pytest.mark.asyncio
    async def test_mute_with_hours(self, mock_context, mock_event):
        """测试小时格式"""
        result = await bot_core.handle("闭嘴", "2h", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该显示小时
        assert "小时" in result_text or "h" in result_text.lower()
        mock_context.mute_group.assert_called_once_with(54321, 120)

    @pytest.mark.asyncio
    async def test_mute_private_chat(self, mock_context, mock_private_event):
        """测试私聊不支持静音"""
        result = await bot_core.handle("闭嘴", "", mock_private_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "不支持" in result_text or "私聊" in result_text
        mock_context.mute_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_mute_too_long(self, mock_context, mock_event):
        """测试超长静音时间"""
        # 超过1440分钟(24小时)
        result = await bot_core.handle("闭嘴", "3000", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "过长" in result_text or "最多" in result_text or "❌" in result_text
        mock_context.mute_group.assert_not_called()

    @pytest.mark.parametrize("value", ["invalid", "nan", "inf", "-1", "1e9999", "1时h"])
    @pytest.mark.asyncio
    async def test_mute_rejects_invalid_duration_without_using_default(
        self, mock_context, mock_event, value
    ):
        result = await bot_core.handle("闭嘴", value, mock_event, mock_context)

        assert "时长格式错误" in str(result)
        mock_context.mute_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_mute_fractional_minutes_are_not_truncated(self, mock_context, mock_event):
        result = await bot_core.handle("闭嘴", "0.5m", mock_event, mock_context)

        assert "0.5 分钟" in str(result)
        mock_context.mute_group.assert_called_once_with(54321, 0.5)


# ============================================================
# Test Unmute Command
# ============================================================


class TestUnmuteCommand:
    """测试解除静音命令"""

    @pytest.mark.asyncio
    async def test_unmute_when_not_muted(self, mock_context, mock_event):
        """测试未静音时解除"""
        result = await bot_core.handle("说话", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该提示本来就没有静音
        assert "没闭嘴" in result_text or "本来" in result_text or "😊" in result_text
        mock_context.get_mute_remaining.assert_called_once_with(54321)
        mock_context.unmute_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_unmute_when_muted(self, mock_context_with_mute, mock_event):
        """测试静音时解除"""
        result = await bot_core.handle("说话", "", mock_event, mock_context_with_mute)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该确认解除静音
        assert "可以说话" in result_text or "解除" in result_text or "😊" in result_text
        mock_context_with_mute.get_mute_remaining.assert_called_once_with(54321)
        mock_context_with_mute.unmute_group.assert_called_once_with(54321)

    @pytest.mark.asyncio
    async def test_unmute_private_chat(self, mock_context, mock_private_event):
        """测试私聊不支持解除静音"""
        result = await bot_core.handle("说话", "", mock_private_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "不支持" in result_text or "私聊" in result_text
        mock_context.get_mute_remaining.assert_not_called()
        mock_context.unmute_group.assert_not_called()


# ============================================================
# Test Set Secret Command
# ============================================================


class TestSetSecretCommand:
    """测试设置密钥命令"""

    @pytest.mark.asyncio
    async def test_set_secret_usage(self, mock_context):
        """测试显示用法"""
        result = await bot_core.handle("set_secret", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "用法" in result_text or "set_secret" in result_text or "/" in result_text

    @pytest.mark.asyncio
    async def test_set_secret_invalid_path(self, mock_context):
        """测试带空格的路径（实际上会解析成功，将空格前的部分作为路径）"""
        result = await bot_core.handle("set_secret", "invalid path value", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 由于split(maxsplit=1)，实际路径是"invalid"，值是"path value"
        # 路径"invalid"不存在时会报错
        assert "不存在" in result_text or "❌" in result_text or "更新" in result_text

    @pytest.mark.asyncio
    async def test_set_secret_no_config_manager(self):
        """测试没有ConfigManager的情况"""

        class NoConfigContext:
            def __init__(self):
                self.config_manager = None

        result = await bot_core.handle("set_secret", "path value", {}, NoConfigContext())
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "不可用" in result_text or "❌" in result_text

    @pytest.mark.asyncio
    async def test_set_secret_does_not_trigger_manual_reload(self):
        """测试 set_secret 不再额外触发 reload_config。"""

        class Ctx:
            def __init__(self):
                self.principal = PluginPrincipal(
                    kind="user",
                    user_id=1,
                    is_bot_admin=True,
                    is_private=True,
                )
                self.secret_admin = MagicMock()
                self.capabilities = PluginCapabilities(
                    is_bot_admin=True,
                    secret_admin=self.secret_admin,
                )
                self.reload_config = MagicMock()

        ctx = Ctx()
        result = await bot_core.handle(
            "set_secret", "plugins.signin.yingshijufeng.sid new_sid", {}, ctx
        )

        assert result is not None
        ctx.secret_admin.set.assert_called_once_with(
            "plugins.signin.yingshijufeng.sid",
            "new_sid",
        )
        ctx.reload_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_secret_redacts_command_log_before_handler(self, mock_context, monkeypatch):
        info = MagicMock()
        monkeypatch.setattr(bot_core.logger, "info", info)

        await bot_core.handle(
            "set_secret",
            "plugins.signin.yingshijufeng.sid CANARY-secret",
            {"user_id": 1, "group_id": None},
            mock_context,
        )

        serialized_calls = str(info.call_args_list)
        assert "CANARY-secret" not in serialized_calls
        assert "<redacted>" in serialized_calls

    @pytest.mark.asyncio
    async def test_set_secret_path_matches_core_hyphen_contract(self, mock_context):
        result = await bot_core.handle(
            "set_secret",
            "plugins.my-plugin.api-key value",
            {},
            mock_context,
        )

        assert "已更新" in str(result)
        mock_context.capabilities.secret_admin.set.assert_called_once_with(
            "plugins.my-plugin.api-key", "value"
        )

    @pytest.mark.asyncio
    async def test_set_secret_does_not_create_non_finite_json_number(self, mock_context):
        await bot_core.handle("set_secret", "plugins.test.value NaN", {}, mock_context)

        mock_context.capabilities.secret_admin.set.assert_called_once_with(
            "plugins.test.value", "NaN"
        )

    @pytest.mark.asyncio
    async def test_set_secret_handles_authorization_revocation(self, mock_context):
        mock_context.capabilities.secret_admin.set.side_effect = PermissionError("revoked")

        result = await bot_core.handle("set_secret", "plugins.test.value secret", {}, mock_context)

        assert "全局管理员" in str(result)
        assert "XQ-PLUGIN-UNEXPECTED" not in str(result)


# ============================================================
# Test Get Secret Command
# ============================================================


class TestGetSecretCommand:
    """测试查看密钥命令"""

    @pytest.mark.asyncio
    async def test_get_secret_usage(self, mock_context):
        """测试显示用法"""
        result = await bot_core.handle("get_secret", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "用法" in result_text or "get_secret" in result_text or "/" in result_text

    @pytest.mark.asyncio
    async def test_get_secret_path(self, mock_context):
        """测试查看路径"""
        result = await bot_core.handle("get_secret", "plugins", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该显示plugins下的键
        assert "plugins" in result_text.lower() or "signin" in result_text

    @pytest.mark.asyncio
    async def test_get_secret_nonexistent(self, mock_context):
        """测试查看不存在的路径"""
        result = await bot_core.handle("get_secret", "nonexistent.path.xyz", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "不存在" in result_text or "❌" in result_text

    @pytest.mark.asyncio
    async def test_get_secret_invalid_dict(self, mock_context):
        """测试无效的字典路径"""
        result = await bot_core.handle("get_secret", "admin_user_ids.nonexistent", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "字典" in result_text or "类型" in result_text or "❌" in result_text

    @pytest.mark.asyncio
    async def test_get_secret_rejects_invalid_path_before_capability(self, mock_context):
        result = await bot_core.handle("get_secret", "plugins.bad path", {}, mock_context)

        assert "路径格式错误" in str(result)
        mock_context.capabilities.secret_admin.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_secret_path_matches_core_hyphen_contract(self, mock_context):
        mock_context.capabilities.secret_admin.get.side_effect = None
        mock_context.capabilities.secret_admin.get.return_value = "secret"

        result = await bot_core.handle("get_secret", "plugins.my-plugin.api-key", {}, mock_context)

        assert "****" in str(result)
        mock_context.capabilities.secret_admin.get.assert_called_once_with(
            "plugins.my-plugin.api-key"
        )

    @pytest.mark.asyncio
    async def test_get_secret_dictionary_truncation_has_separator(self, mock_context):
        mock_context.capabilities.secret_admin.get.side_effect = None
        mock_context.capabilities.secret_admin.get.return_value = {
            f"key-{index}": index for index in range(bot_core.MAX_DISPLAYED_SECRET_KEYS + 1)
        }

        result = await bot_core.handle("get_secret", "plugins", {}, mock_context)

        assert ", ... 还有 1 个" in str(result)

    @pytest.mark.asyncio
    async def test_get_secret_handles_authorization_revocation(self, mock_context):
        mock_context.capabilities.secret_admin.get.side_effect = PermissionError("revoked")

        result = await bot_core.handle("get_secret", "plugins", {}, mock_context)

        assert "全局管理员" in str(result)
        assert "XQ-PLUGIN-UNEXPECTED" not in str(result)


# ============================================================
# Test Metrics Command
# ============================================================


class TestMetricsCommand:
    """测试运行指标命令"""

    @pytest.mark.asyncio
    async def test_metrics_success(self, mock_context_with_metrics):
        """测试成功获取指标"""
        result = await bot_core.handle("metrics", "", {}, mock_context_with_metrics)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该包含指标信息
        assert "指标" in result_text or "📈" in result_text or "运行" in result_text

    @pytest.mark.asyncio
    async def test_metrics_no_metrics(self, mock_context):
        """测试没有metrics的情况"""
        result = await bot_core.handle("metrics", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未启用" in result_text or "❌" in result_text

    @pytest.mark.asyncio
    async def test_metrics_sanitizes_malformed_numeric_and_plugin_fields(self):
        metrics = SimpleNamespace(
            get_summary=AsyncMock(
                return_value={
                    "uptime_seconds": float("nan"),
                    "global": {
                        "total_calls": True,
                        "success_rate": 2.0,
                        "avg_time": -1,
                        "slow_calls": "many",
                        "errors": float("inf"),
                    },
                    "top_slow_plugins": [
                        None,
                        {"plugin": " bad\nname ", "avg_time": float("nan")},
                    ],
                }
            )
        )

        result = await bot_core.handle("metrics", "", {}, SimpleNamespace(metrics=metrics))
        result_text = str(result)

        assert "XQ-PLUGIN-UNEXPECTED" not in result_text
        assert "成功率: n/a" in result_text
        assert "bad name: n/a" in result_text

    @pytest.mark.asyncio
    async def test_metrics_rejects_non_mapping_summary(self):
        metrics = SimpleNamespace(get_summary=AsyncMock(return_value=[]))

        result = await bot_core.handle("metrics", "", {}, SimpleNamespace(metrics=metrics))

        assert "无法获取 Metrics 数据" in str(result)

    @pytest.mark.asyncio
    async def test_metrics_omits_empty_slowest_plugin_section(self):
        metrics = SimpleNamespace(
            get_summary=AsyncMock(return_value={"global": {}, "top_slow_plugins": [None]})
        )

        result = await bot_core.handle("metrics", "", {}, SimpleNamespace(metrics=metrics))

        assert "最慢插件" not in str(result)
        assert "运行时间: n/a" in str(result)
        assert "总调用: n/a" in str(result)


# ============================================================
# Test Mask Secret Function
# ============================================================


class TestMaskSecret:
    """测试密钥遮罩函数"""

    def test_mask_short_string(self):
        """测试短字符串遮罩"""
        result = bot_core.mask_secret("abc")
        assert result == "****"

    def test_mask_long_string(self):
        """测试长字符串遮罩"""
        result = bot_core.mask_secret("my_secret_key_12345")
        assert result == "my****45"

    def test_mask_length_is_bounded(self):
        assert bot_core.mask_secret("a" * 100_000) == "aa****aa"

    def test_mask_number(self):
        """测试数字遮罩"""
        result = bot_core.mask_secret(12345)
        assert result == "****"

    def test_mask_list(self):
        """测试列表遮罩"""
        result = bot_core.mask_secret([1, 2, 3, 4, 5])
        assert "5 values" in result or "5个" in result

    def test_mask_dict(self):
        """测试字典遮罩"""
        result = bot_core.mask_secret({"key1": "value1", "key2": "value2"})
        assert "2 keys" in result or "2个" in result


# ============================================================
# Test Parse Duration Function
# ============================================================


class TestParseDuration:
    """测试时长解析函数"""

    def test_parse_minutes(self):
        """测试解析分钟"""
        result = bot_core._parse_duration("30")
        assert result == 30
        result = bot_core._parse_duration("30m")
        assert result == 30
        result = bot_core._parse_duration("30min")
        assert result == 30
        result = bot_core._parse_duration("30分钟")
        assert result == 30

    def test_parse_hours(self):
        """测试解析小时"""
        result = bot_core._parse_duration("2h")
        assert result == 120
        result = bot_core._parse_duration("1.5h")
        assert result == 90
        result = bot_core._parse_duration("2小时")
        assert result == 120
        assert bot_core._parse_duration("2H") == 120

    def test_parse_empty(self):
        """测试空输入"""
        result = bot_core._parse_duration("")
        assert result is None

    def test_parse_invalid(self):
        """测试无效输入"""
        result = bot_core._parse_duration("invalid")
        assert result is None

    def test_parse_fraction_hours(self):
        """测试小数小时"""
        result = bot_core._parse_duration("0.5h")
        assert result == 30
        result = bot_core._parse_duration("1.25h")
        assert result == 75
        assert bot_core._parse_duration(".5h") == 30

    @pytest.mark.parametrize("value", ["nan", "inf", "-1", "1e9999", "1时h"])
    def test_parse_rejects_malformed_or_non_finite_values(self, value):
        assert bot_core._parse_duration(value) is None


# ============================================================
# Test Unknown Command
# ============================================================


class TestUnknownCommand:
    """测试未知命令处理"""

    @pytest.mark.asyncio
    async def test_unknown_command(self, mock_context):
        """测试未知命令"""
        result = await bot_core.handle("unknown_command", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未知" in result_text or "不认识" in result_text or "❌" in result_text


# ============================================================
# Test Command Aliases
# ============================================================


class TestCommandAliases:
    """测试命令别名"""

    @pytest.mark.asyncio
    async def test_help_alias_h(self, mock_context):
        """测试help别名h"""
        result = await bot_core.handle("help", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_reload_alias_chinese(self, mock_context):
        """测试reload中文别名"""
        # handle函数通过command参数判断，所以传入reload命令名
        result = await bot_core.handle("reload", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0


# ============================================================
# Structured command catalog helpers
# ============================================================


class TestStructuredCommandCatalog:
    def test_legacy_help_format_wrappers_are_removed(self):
        assert not hasattr(bot_core, "_format_catalog_text")
        assert not hasattr(bot_core, "_format_catalog_node")

    def test_plugin_overview_paginates_plugins_instead_of_command_nodes(self):
        roots = tuple(
            _catalog_node(
                f"plugin_{index}.command",
                f"plugin_{index}",
                (f"command_{index}",),
                f"插件 {index} 功能",
                f"/command_{index}",
            )
            for index in range(bot_core.HELP_PLUGIN_PAGE_SIZE + 1)
        )

        first_page, total_pages = bot_core._plugin_overview_page(roots, 1)
        second_page, second_total = bot_core._plugin_overview_page(roots, 2)

        assert total_pages == second_total == 2
        assert len(first_page) == bot_core.HELP_PLUGIN_PAGE_SIZE
        assert len(second_page) == 1

    def test_plugin_overview_prefers_the_root_with_the_complete_command_tree(self):
        canonical = _catalog_node(
            "demo.demo",
            "demo",
            ("demo",),
            "主入口",
            "/demo <子命令>",
            children=(
                _catalog_node(
                    "demo.demo.list",
                    "demo",
                    ("demo", "list"),
                    "列表",
                    "/demo list",
                ),
            ),
        )
        compatibility = _catalog_node(
            "demo.legacy",
            "demo",
            ("legacy",),
            "旧版兼容入口",
            "/legacy",
        )

        output = bot_core._format_plugin_overview_entry(
            "demo",
            (compatibility, canonical),
        )

        assert "• demo · 3个命令" in output
        assert "  /demo（另有1个入口）" in output
        assert "  主入口" in output

    def test_plugin_and_branch_menus_only_show_direct_children(self):
        grandchild = _catalog_node(
            "demo.demo.section.run",
            "demo",
            ("demo", "section", "run"),
            "执行操作",
            "/demo section run <参数>",
            examples=("/demo section run value",),
            invalid_examples=("/demo section run",),
        )
        section = _catalog_node(
            "demo.demo.section",
            "demo",
            ("demo", "section"),
            "分组操作",
            "/demo section <操作>",
            children=(grandchild,),
        )
        root = _catalog_node(
            "demo.demo",
            "demo",
            ("demo",),
            "演示插件",
            "/demo <功能>",
            children=(section,),
        )

        plugin_menu = bot_core._format_plugin_menu((root,), page=1)
        branch_menu = bot_core._format_branch_menu(section, page=1)

        assert "/demo section" in plugin_menu
        assert "/demo section run" not in plugin_menu
        assert "命令码：" not in plugin_menu
        assert "正确示例" not in plugin_menu
        assert "/demo section run" in branch_menu
        assert "/demo section run <参数>" not in branch_menu
        assert "继续查看：/help demo section run" in branch_menu

    def test_text_catalog_uses_smaller_mobile_pages_without_changing_json_pages(self):
        nodes = tuple(
            _catalog_node(
                f"demo.demo.item_{index}",
                "demo",
                ("demo", f"item_{index}"),
                f"项目 {index}",
                f"/demo item_{index}",
            )
            for index in range(bot_core.HELP_PAGE_SIZE + bot_core.HELP_TEXT_PAGE_SIZE + 1)
        )

        text_page, text_pages = bot_core._text_catalog_page(nodes, 1)
        json_page, json_pages = bot_core._catalog_page(nodes, 1)

        assert len(text_page) == bot_core.HELP_TEXT_PAGE_SIZE
        assert len(json_page) == bot_core.HELP_PAGE_SIZE
        assert text_pages > json_pages

    def test_select_by_plugin_returns_every_descendant(self):
        result = bot_core._select_catalog_nodes(_sample_catalog(), "bot_core")

        assert tuple(node.code for node in result) == (
            "bot_core.help",
            "bot_core.help.search",
            "bot_core.reload",
        )

    def test_select_by_stable_code_returns_subtree(self):
        result = bot_core._select_catalog_nodes(_sample_catalog(), "bot_core.help")

        assert tuple(node.code for node in result) == (
            "bot_core.help",
            "bot_core.help.search",
        )

    def test_searches_alias_and_help_without_parsing_formatted_text(self):
        by_alias = bot_core._select_catalog_nodes(_sample_catalog(), "重载")
        by_description = bot_core._select_catalog_nodes(_sample_catalog(), "AI 对话")

        assert tuple(node.code for node in by_alias) == ("bot_core.reload",)
        assert tuple(node.code for node in by_description) == ("chat.chat",)

    @pytest.mark.parametrize("raw", ["page 0", "page abc", "search", "all extra"])
    def test_rejects_invalid_catalog_requests(self, raw):
        with pytest.raises(ValueError):
            bot_core._parse_help_request(raw)

    @pytest.mark.parametrize("raw", ["page ²", "all ٣", "页 １２"])
    def test_rejects_unicode_page_digits(self, raw):
        with pytest.raises(ValueError):
            bot_core._parse_help_request(raw)

    def test_json_export_contains_stable_code_and_child_references(self):
        roots = _sample_catalog()

        output = bot_core._format_catalog_json((roots[0],), "bot_core.help", 1, 1)

        assert '"code": "bot_core.help"' in output
        assert '"bot_core.help.search"' in output
