"""
PluginContext 单元测试
"""

import asyncio
import pytest
from pathlib import Path
from typing import Any

from core.context import PluginContext

# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def sample_context(tmp_path: Path) -> PluginContext:
    """创建示例 PluginContext"""
    send_action_called = []

    async def mock_send_action(message):
        send_action_called.append(message)

    def mock_reload_config():
        pass

    def mock_reload_plugins():
        pass

    def mock_list_commands():
        return ["help: 查看帮助", "echo: 回显消息"]

    def mock_list_plugins():
        return ["echo", "help", "choice"]

    return PluginContext(
        config={"bot_name": "测试机器人", "command_prefixes": ["/"]},
        secrets={"admin_user_ids": [12345]},
        plugin_name="echo",
        plugin_dir=tmp_path / "plugins" / "echo",
        data_dir=tmp_path / "data" / "echo",
        http_session=None,
        send_action=mock_send_action,
        reload_config=mock_reload_config,
        reload_plugins=mock_reload_plugins,
        list_commands=mock_list_commands,
        list_plugins=mock_list_plugins,
        current_user_id=12345,
        current_group_id=67890,
        request_id="test_request_001",
        state={"counter": 0},
    )

# ============================================================
# PluginContext 初始化测试
# ============================================================

class TestPluginContextInit:
    """PluginContext 初始化测试"""

    def test_all_properties_set(self, sample_context: PluginContext):
        """测试所有属性正确设置"""
        assert sample_context.config["bot_name"] == "测试机器人"
        assert sample_context.secrets["admin_user_ids"] == [12345]
        assert sample_context.plugin_name == "echo"
        assert sample_context.current_user_id == 12345
        assert sample_context.current_group_id == 67890
        assert sample_context.request_id == "test_request_001"
        assert sample_context.state["counter"] == 0

    def test_optional_properties_none(self):
        """测试可选属性为 None"""
        context = PluginContext(
            config={},
            secrets={},
            plugin_name="test",
            plugin_dir=Path("/tmp"),
            data_dir=Path("/tmp"),
            http_session=None,
            send_action=lambda x: None,
            reload_config=lambda: None,
            reload_plugins=lambda: None,
            list_commands=lambda: [],
            list_plugins=lambda: [],
            current_user_id=None,
            current_group_id=None,
            request_id=None,
            state=None,
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
        assert sample_context.config["command_prefixes"] == ["/"]

    def test_secrets_access(self, sample_context: PluginContext):
        """测试密钥访问"""
        assert sample_context.secrets["admin_user_ids"] == [12345]

    def test_state_access(self, sample_context: PluginContext):
        """测试状态访问"""
        assert sample_context.state["counter"] == 0

    def test_modify_state(self, sample_context: PluginContext):
        """测试修改状态"""
        sample_context.state["counter"] = 5
        assert sample_context.state["counter"] == 5

        sample_context.state["new_key"] = "new_value"
        assert sample_context.state["new_key"] == "new_value"

    def test_plugin_name(self, sample_context: PluginContext):
        """测试插件名称"""
        assert sample_context.plugin_name == "echo"

    def test_plugin_dir(self, sample_context: PluginContext):
        """测试插件目录"""
        assert sample_context.plugin_dir.name == "echo"

    def test_data_dir(self, sample_context: PluginContext):
        """测试数据目录"""
        assert "echo" in str(sample_context.data_dir)

# ============================================================
# PluginContext 方法调用测试
# ============================================================

class TestPluginContextMethods:
    """PluginContext 方法调用测试"""

    @pytest.mark.asyncio
    async def test_send_action(self, sample_context: PluginContext):
        """测试发送消息"""
        message = [{"type": "text", "data": {"text": "测试"}}]
        await sample_context.send_action(message)
        # 验证被调用（通过 fixture 中的列表）
        # 实际验证需要在 fixture 中设置

    def test_reload_config(self, sample_context: PluginContext):
        """测试重载配置"""
        sample_context.reload_config()
        # 应该不抛出异常

    def test_reload_plugins(self, sample_context: PluginContext):
        """测试重载插件"""
        sample_context.reload_plugins()
        # 应该不抛出异常

    def test_list_commands(self, sample_context: PluginContext):
        """测试列出命令"""
        commands = sample_context.list_commands()
        assert "help: 查看帮助" in commands
        assert "echo: 回显消息" in commands

    def test_list_plugins(self, sample_context: PluginContext):
        """测试列出插件"""
        plugins = sample_context.list_plugins()
        assert "echo" in plugins
        assert "help" in plugins
        assert "choice" in plugins

    def test_logger_proxies_missing_methods(self, sample_context: PluginContext):
        sample_context.logger.critical("critical message")

# ============================================================
# 私聊/群聊上下文测试
# ============================================================

class TestPrivateVsGroupContext:
    """私聊/群聊上下文测试"""

    def test_private_message_context(self, tmp_path: Path):
        """测试私聊消息上下文"""
        context = PluginContext(
            config={},
            secrets={},
            plugin_name="test",
            plugin_dir=tmp_path,
            data_dir=tmp_path,
            http_session=None,
            send_action=lambda x: None,
            reload_config=lambda: None,
            reload_plugins=lambda: None,
            list_commands=lambda: [],
            list_plugins=lambda: [],
            current_user_id=12345,
            current_group_id=None,
            request_id="priv_001",
            state={},
        )
        assert context.current_user_id == 12345
        assert context.current_group_id is None

    def test_group_message_context(self, tmp_path: Path):
        """测试群聊消息上下文"""
        context = PluginContext(
            config={},
            secrets={},
            plugin_name="test",
            plugin_dir=tmp_path,
            data_dir=tmp_path,
            http_session=None,
            send_action=lambda x: None,
            reload_config=lambda: None,
            reload_plugins=lambda: None,
            list_commands=lambda: [],
            list_plugins=lambda: [],
            current_user_id=12345,
            current_group_id=67890,
            request_id="group_001",
            state={},
        )
        assert context.current_user_id == 12345
        assert context.current_group_id == 67890

# ============================================================
# 空状态测试
# ============================================================

class TestEmptyState:
    """空状态测试"""

    def test_empty_state_dict(self, tmp_path: Path):
        """测试空状态字典"""
        context = PluginContext(
            config={},
            secrets={},
            plugin_name="test",
            plugin_dir=tmp_path,
            data_dir=tmp_path,
            http_session=None,
            send_action=lambda x: None,
            reload_config=lambda: None,
            reload_plugins=lambda: None,
            list_commands=lambda: [],
            list_plugins=lambda: [],
            current_user_id=None,
            current_group_id=None,
            request_id=None,
            state={},
        )
        assert context.state == {}
        assert len(context.state) == 0

# ============================================================
# HTTP Session 测试
# ============================================================

class TestHttpSession:
    """HTTP Session 测试"""

    def test_none_http_session(self, sample_context: PluginContext):
        """测试 None HTTP session"""
        assert sample_context.http_session is None

    def test_with_http_session(self, tmp_path: Path):
        """测试带 HTTP session"""
        mock_session = object()

        context = PluginContext(
            config={},
            secrets={},
            plugin_name="test",
            plugin_dir=tmp_path,
            data_dir=tmp_path,
            http_session=mock_session,
            send_action=lambda x: None,
            reload_config=lambda: None,
            reload_plugins=lambda: None,
            list_commands=lambda: [],
            list_plugins=lambda: [],
            current_user_id=None,
            current_group_id=None,
            request_id=None,
            state={},
        )
        assert context.http_session is mock_session

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
