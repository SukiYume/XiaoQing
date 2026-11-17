"""
PluginContext 单元测试
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.config import ConfigSnapshot, lookup_dotted
from core.context import PluginContext
from core.interfaces import (
    DeliveryTarget,
    PluginCapabilities,
    PluginPrincipal,
    PluginSettingsSnapshot,
)
from core.router import CommandCatalogNode
from core.session import SessionManager

# ============================================================
# Fixtures
# ============================================================


def _settings_generation(
    config: dict[str, Any],
    secrets: dict[str, Any],
    revision: int = 0,
) -> PluginSettingsSnapshot:
    """按 Core 的方式冻结一代配置，交给读取器返回。"""

    frozen = ConfigSnapshot(config=config, secrets=secrets, revision=revision)
    return PluginSettingsSnapshot(
        config   = frozen.config,
        secrets  = frozen.secrets,
        revision = frozen.revision,
    )


def _readers(plugin_name: str, settings: PluginSettingsSnapshot) -> dict[str, Any]:
    """Core 交给插件上下文的两个读取器：同一代配置里读设置与密钥。"""

    return {
        "settings_reader": lambda: settings,
        "secret_reader"  : lambda path: lookup_dotted(settings.plugin_secrets(plugin_name), path),
    }


@pytest.fixture
def sample_context(tmp_path: Path) -> PluginContext:
    """创建示例 PluginContext"""
    calls: dict[str, Any] = {
        "send_action"   : [],
        "reload_config" : 0,
        "reload_plugins": 0,
    }

    async def mock_send_action(message):
        calls["send_action"].append(message)
        return True

    def mock_reload_config():
        calls["reload_config"] += 1
        return "config-reloaded"

    def mock_reload_plugins():
        calls["reload_plugins"] += 1
        return "plugins-reloaded"

    def mock_get_command_catalog():
        return (
            CommandCatalogNode(
                code      = "bot_core.help",
                plugin    = "bot_core",
                path      = ("help",),
                name      = "help",
                aliases   = ("帮助",),
                help_text = "查看帮助",
                usage     = "/help",
            ),
            CommandCatalogNode(
                code      = "echo.echo",
                plugin    = "echo",
                path      = ("echo",),
                name      = "echo",
                aliases   = ("回显",),
                help_text = "回显消息",
                usage     = "/echo <消息>",
            ),
        )

    def mock_list_plugins():
        return ["echo", "help", "choice"]

    config  = {"bot_name": "测试机器人", "command_prefixes": ["/"]}
    secrets = {
        "admin_user_ids": [12345],
        "plugins"       : {"echo": {"api_key": "plugin-key"}, "other": {"hidden": True}},
    }
    return PluginContext(
        config              = config,
        secrets             = secrets,
        plugin_name         = "echo",
        plugin_dir          = tmp_path / "plugins" / "echo",
        data_dir            = tmp_path / "data" / "echo",
        http_session        = None,
        send_action         = mock_send_action,
        reload_config       = mock_reload_config,
        reload_plugins      = mock_reload_plugins,
        get_command_catalog = mock_get_command_catalog,
        list_plugins        = mock_list_plugins,
        current_user_id     = 12345,
        current_group_id    = 67890,
        principal=PluginPrincipal(kind="user", user_id=12345, is_bot_admin=True),
        capabilities=PluginCapabilities(is_bot_admin=True),
        request_id = "test_request_001",
        state      = {"counter": 0, "calls": calls},
        **_readers("echo", _settings_generation(config, secrets)),
    )


# ============================================================
# PluginContext 初始化测试
# ============================================================


class TestPluginContextInit:
    """PluginContext 初始化测试"""

    def test_all_properties_set(self, sample_context: PluginContext):
        """测试所有属性正确设置"""
        assert sample_context.config["bot_name"] == "测试机器人"
        assert sample_context.secrets == {"plugins": {"echo": {"api_key": "plugin-key"}}}
        assert sample_context.plugin_name == "echo"
        assert sample_context.current_user_id == 12345
        assert sample_context.current_group_id == 67890
        assert sample_context.request_id == "test_request_001"
        assert sample_context.state["counter"] == 0

        snapshot = ConfigSnapshot(
            config  = {"default_group_ids": [123, 456]},
            secrets = {
                "plugins": {
                    "echo": {
                        "api_key": "snapshot-key",
                        "payload": {"items": [{"value": 1}]},
                    }
                }
            },
        )
        sample_context.config  = snapshot.config
        sample_context.secrets = snapshot.secrets
        for name, reader in _readers(
            "echo", PluginSettingsSnapshot(snapshot.config, snapshot.secrets, 0)
        ).items():
            setattr(sample_context, name, reader)
        assert sample_context.default_groups() == [123, 456]
        assert isinstance(sample_context.default_groups(), list)
        assert sample_context.get_secret("api_key") == "snapshot-key"
        payload = sample_context.get_secret("payload")
        assert isinstance(payload, dict)
        assert isinstance(payload["items"], list)
        payload["items"][0]["value"] = 2
        assert snapshot.secrets["plugins"]["echo"]["payload"]["items"][0]["value"] == 1

    def test_optional_properties_none(self):
        """测试可选属性为 None"""
        context = PluginContext(
            config              = {},
            secrets             = {},
            plugin_name         = "test",
            plugin_dir          = Path("/tmp"),
            data_dir            = Path("/tmp"),
            http_session        = None,
            send_action         = lambda x: None,
            reload_config       = lambda: None,
            reload_plugins      = lambda: None,
            get_command_catalog = lambda: (),
            list_plugins        = lambda: [],
            current_user_id     = None,
            current_group_id    = None,
            request_id          = None,
            state               = None,
        )
        assert context.current_user_id is None
        assert context.current_group_id is None
        assert context.request_id is None
        assert context.state is None


# ============================================================
# PluginContext 属性访问测试
# ============================================================


class TestPluginContextAccess:
    """PluginContext 属性访问测试"""

    def test_config_access(self, sample_context: PluginContext):
        """测试配置访问"""
        assert sample_context.config["bot_name"] == "测试机器人"
        assert sample_context.config["command_prefixes"] == ("/",)

    @pytest.mark.parametrize(
        ("delivery_targets", "expected"),
        [
            (
                (
                    DeliveryTarget("group", 123),
                    DeliveryTarget("group", 456),
                ),
                [123, 456],
            ),
            ((), []),
        ],
    )
    def test_scheduled_default_groups_use_core_resolved_targets(
        self,
        sample_context: PluginContext,
        delivery_targets: tuple[DeliveryTarget, ...],
        expected: list[int],
    ) -> None:
        """定时插件读取 Core 已解析的清单目标，显式空列表也不会回退。"""

        sample_context.config    = {"default_group_ids": (999,)}
        sample_context.principal = PluginPrincipal(
            kind             = "scheduled_system",
            delivery_targets = delivery_targets,
        )

        assert sample_context.default_groups() == expected

    def test_secrets_access(self, sample_context: PluginContext):
        """构造器也必须裁剪全局与其他插件密钥，并冻结当前插件视图。"""
        assert "admin_user_ids" not in sample_context.secrets
        assert "other" not in sample_context.secrets["plugins"]
        assert sample_context.get_secret("api_key") == "plugin-key"
        with pytest.raises(TypeError):
            sample_context.secrets["plugins"]["echo"]["api_key"] = "changed"

    def test_modify_state(self, sample_context: PluginContext):
        """测试修改状态"""
        sample_context.state["counter"] = 5
        assert sample_context.state["counter"] == 5

        sample_context.state["new_key"] = "new_value"
        assert sample_context.state["new_key"] == "new_value"

    def test_atomic_settings_reader_and_detached_config_access(
        self,
        sample_context: PluginContext,
    ):
        """成组配置读取只消费一代快照，返回值不能反向修改快照。"""
        source = ConfigSnapshot(
            config={
                "plugins": {
                    "echo": {
                        "nested": {"items": [{"value": 1}]},
                    }
                }
            },
            secrets  = {"plugins": {"echo": {"api_key": "current"}}},
            revision = 7,
        )
        settings = PluginSettingsSnapshot(
            config   = source.config,
            secrets  = source.secrets,
            revision = source.revision,
        )
        reads: list[int] = []

        def read_settings() -> PluginSettingsSnapshot:
            reads.append(settings.revision)
            return settings

        sample_context.settings_reader = read_settings

        observed = sample_context.get_settings_snapshot()
        nested   = sample_context.get_config("nested")

        assert observed is settings
        assert reads == [7, 7]
        assert nested == {"items": [{"value": 1}]}
        nested["items"][0]["value"] = 2
        assert source.config["plugins"]["echo"]["nested"]["items"][0]["value"] == 1

    def test_secret_reads_follow_the_core_supplied_settings_generation(
        self,
        sample_context: PluginContext,
    ):
        """密钥与配置只经 Core 给的读取器读取，读取器换代后立即读到新一代。"""

        for name, reader in _readers(
            "echo",
            _settings_generation(
                {"plugins": {"echo": {}}},
                {"plugins": {"echo": {"api_key": "your-api-key-placeholder"}}},
                revision=8,
            ),
        ).items():
            setattr(sample_context, name, reader)

        assert sample_context.get_secret("api_key") == "your-api-key-placeholder"
        assert sample_context.get_settings_snapshot().revision == 8

    def test_settings_and_secret_reads_fail_without_core_readers(
        self,
        sample_context: PluginContext,
    ):
        """没有 Core 读取器时报错，不再从自己的 config / secrets 字段回退。"""

        sample_context.settings_reader = None
        sample_context.secret_reader   = None

        with pytest.raises(RuntimeError, match="settings reader unavailable"):
            sample_context.get_settings_snapshot()
        with pytest.raises(RuntimeError, match="settings reader unavailable"):
            sample_context.get_config("api_key")
        with pytest.raises(RuntimeError, match="secret reader unavailable"):
            sample_context.get_secret("api_key")

    def test_settings_snapshot_extracts_only_the_scoped_plugin_namespaces(
        self,
        sample_context: PluginContext,
    ):
        sample_context.settings_reader = lambda: PluginSettingsSnapshot(
            config   = {"plugins": {"echo": {"enabled": True}}},
            secrets  = {"plugins": {"echo": {"api_key": "plugin-key"}}},
            revision = 4,
        )
        settings = sample_context.get_settings_snapshot()

        assert settings.plugin_config("echo") == {"enabled": True}
        assert settings.plugin_secrets("echo") == {"api_key": "plugin-key"}
        assert settings.plugin_config("other") == {}
        assert settings.plugin_secrets("other") == {}

    def test_settings_snapshot_treats_malformed_namespaces_as_absent(self):
        settings = PluginSettingsSnapshot(
            config   = {"plugins": {"echo": "invalid"}},
            secrets  = {"plugins": []},
            revision = 3,
        )

        assert settings.plugin_config("echo") == {}
        assert settings.plugin_secrets("echo") == {}


# ============================================================
# PluginContext 方法调用测试
# ============================================================


class TestPluginContextMethods:
    """PluginContext 方法调用测试"""

    @pytest.mark.asyncio
    async def test_send_action(self, sample_context: PluginContext):
        """测试发送消息"""
        message = [{"type": "text", "data": {"text": "测试"}}]
        result  = await sample_context.send_action(message)
        assert result is True
        assert sample_context.state["calls"]["send_action"] == [message]

    def test_reload_config(self, sample_context: PluginContext):
        """测试重载配置"""
        assert sample_context.reload_config() == "config-reloaded"
        assert sample_context.state["calls"]["reload_config"] == 1

    def test_reload_plugins(self, sample_context: PluginContext):
        """测试重载插件"""
        assert sample_context.reload_plugins() == "plugins-reloaded"
        assert sample_context.state["calls"]["reload_plugins"] == 1

    def test_mute_control_delegates_exact_group_duration_and_result(
        self,
        sample_context: PluginContext,
    ):
        mute_control                                 = MagicMock()
        mute_control.unmute_group.return_value       = True
        mute_control.get_mute_remaining.return_value = 12.5
        sample_context.mute_control                  = mute_control

        sample_context.mute_group(67890, 30.5)

        mute_control.mute_group.assert_called_once_with(67890, 30.5)
        assert sample_context.unmute_group(67890) is True
        mute_control.unmute_group.assert_called_once_with(67890)
        assert sample_context.get_mute_remaining(67890) == 12.5
        mute_control.get_mute_remaining.assert_called_once_with(67890)

    def test_get_command_catalog(self, sample_context: PluginContext):
        """测试获取不可变的结构化命令目录。"""

        catalog = sample_context.get_command_catalog()
        assert tuple(node.code for node in catalog) == ("bot_core.help", "echo.echo")
        assert catalog[1].aliases == ("回显",)

    def test_list_plugins(self, sample_context: PluginContext):
        """测试列出插件"""
        plugins = sample_context.list_plugins()
        assert "echo" in plugins
        assert "help" in plugins
        assert "choice" in plugins

    @pytest.mark.asyncio
    async def test_session_helpers_use_snapshots_and_non_touching_exists(
        self,
        sample_context: PluginContext,
    ):
        manager = SessionManager()
        sample_context.session_manager = manager
        created = await sample_context.create_session({"nested": {"value": 1}})
        before = await manager.peek(12345, 67890)
        assert before is not None

        assert await sample_context.has_session() is True
        after_exists = await manager.peek(12345, 67890)
        assert after_exists is not None
        assert after_exists.updated_at == before.updated_at

        created.data["nested"]["value"] = 2
        read                            = await sample_context.get_session()
        assert read is not None and read.data["nested"]["value"] == 1

        await sample_context.update_session(lambda working: working.set("value", 3))
        read.data["nested"]["value"] = 4
        committed                    = await manager.peek(12345, 67890)
        assert committed is not None
        assert committed.get("value") == 3
        assert committed.data["nested"]["value"] == 1

    @pytest.mark.asyncio
    async def test_expired_session_returns_none_until_explicitly_recreated(
        self,
        sample_context: PluginContext,
    ):
        manager                        = SessionManager()
        sample_context.session_manager = manager
        await sample_context.create_session({"step": 1}, timeout=30)
        stored = manager._sessions[(12345, 67890)]
        stored.updated_at -= 31

        assert await sample_context.get_session() is None
        assert await sample_context.has_session() is False
        assert await manager.peek(12345, 67890) is None

        recreated = await sample_context.create_session({"step": 2})
        assert recreated.get("step") == 2
