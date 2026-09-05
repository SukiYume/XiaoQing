"""
Tests for core/interfaces.py - Protocol interface definitions
Uses duck typing instead of isinstance checks for Protocol classes
"""

import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.interfaces import (
    ConfigManagerLike,
    DeliveryTarget,
    PluginPrincipal,
)


def test_delivery_target_is_validated_and_immutable() -> None:
    private = DeliveryTarget("private", 123)
    group   = DeliveryTarget("group", 456)

    assert (private.user_id, private.group_id) == (123, None)
    assert (group.user_id, group.group_id) == (None, 456)
    with pytest.raises(FrozenInstanceError):
        group.target_id = 789  # type: ignore[misc]
    for invalid in (0, -1, True):
        with pytest.raises((TypeError, ValueError)):
            DeliveryTarget("group", invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DeliveryTarget("broadcast", 1)  # type: ignore[arg-type]


def test_plugin_principal_validates_identity_and_group_authority() -> None:
    principal = PluginPrincipal(
        kind       = "user",
        user_id    = 123,
        group_id   = 456,
        group_role = "admin",
    )

    assert principal.can_manage_group(456) is True
    assert principal.can_manage_group(True) is False
    assert principal.can_manage_group("456") is False  # type: ignore[arg-type]
    for invalid_id in (0, -1, True, "123"):
        with pytest.raises((TypeError, ValueError)):
            PluginPrincipal(kind="user", user_id=invalid_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PluginPrincipal(kind="unknown")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PluginPrincipal(kind="user", user_id=1, is_private=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires user_id"):
        PluginPrincipal(kind="user")
    with pytest.raises(ValueError, match="must not have group_id"):
        PluginPrincipal(kind="user", user_id=1, group_id=2, is_private=True)
    with pytest.raises(ValueError, match="must not carry user scope"):
        PluginPrincipal(kind="lifecycle", user_id=1)
    scheduled = PluginPrincipal(kind="scheduled_system", schedule_delivery="silent")
    assert scheduled.schedule_delivery == "silent"
    with pytest.raises(ValueError, match="only scheduled principals"):
        PluginPrincipal(kind="lifecycle", schedule_delivery="silent")
    with pytest.raises(ValueError, match="delivery mode"):
        PluginPrincipal(
            kind              = "scheduled_system",
            schedule_delivery = "plugin",  # type: ignore[arg-type]
        )


# ============================================================
# Helper Fixtures
# ============================================================


@pytest.fixture
def mock_admin_checker():
    """Create a mock AdminCheck implementation"""

    class MockAdminChecker:
        def is_admin(self, user_id):
            return user_id in [12345, 67890]

    return MockAdminChecker()


@pytest.fixture
def mock_config_provider():
    """Create a mock ConfigProvider implementation"""

    class MockConfigProvider:
        @property
        def config(self):
            return {"bot_name": "测试"}

    return MockConfigProvider()


@pytest.fixture
def mock_mute_control():
    """Create a mock MuteControl implementation"""

    class MockMuteControl:
        def __init__(self):
            self._muted_groups = {}

        def mute_group(self, group_id, duration_minutes):
            import time

            self._muted_groups[group_id] = time.time() + duration_minutes * 60

        def unmute_group(self, group_id):
            if group_id in self._muted_groups:
                del self._muted_groups[group_id]
                return True
            return False

        def is_muted(self, group_id):
            import time

            if group_id not in self._muted_groups:
                return False
            return self._muted_groups[group_id] > time.time()

        def get_mute_remaining(self, group_id):
            import time

            if group_id not in self._muted_groups:
                return 0
            remaining = self._muted_groups[group_id] - time.time()
            return max(0, remaining)

    return MockMuteControl()


# ============================================================
# AdminCheck Protocol Tests
# ============================================================


@pytest.mark.unit
def test_admin_check_protocol(mock_admin_checker):
    """Test AdminCheck protocol implementation (duck typing)"""
    # Verify has is_admin method
    assert hasattr(mock_admin_checker, "is_admin")
    assert callable(mock_admin_checker.is_admin)
    assert mock_admin_checker.is_admin(12345) is True
    assert mock_admin_checker.is_admin(99999) is False


@pytest.mark.unit
def test_admin_check_with_none(mock_admin_checker):
    """Test AdminCheck handles None user_id"""
    assert mock_admin_checker.is_admin(None) is False
    assert mock_admin_checker.is_admin(0) is False


@pytest.mark.unit
def test_admin_check_negative_user_id(mock_admin_checker):
    """Test AdminCheck handles negative user_id"""
    assert mock_admin_checker.is_admin(-1) is False


# ============================================================
# ConfigProvider Protocol Tests
# ============================================================


@pytest.mark.unit
def test_config_provider_protocol(mock_config_provider):
    """Test ConfigProvider protocol implementation (duck typing)"""
    assert hasattr(mock_config_provider, "config")
    assert isinstance(mock_config_provider.config, dict)
    assert mock_config_provider.config["bot_name"] == "测试"


@pytest.mark.unit
def test_config_provider_empty_config():
    """Test ConfigProvider with empty config"""

    class EmptyConfigProvider:
        @property
        def config(self):
            return {}

    provider = EmptyConfigProvider()
    assert hasattr(provider, "config")
    assert provider.config == {}


# ============================================================
# PluginRegistry Protocol Tests
# ============================================================


@pytest.mark.unit
def test_plugin_registry_protocol():
    """Test PluginRegistry protocol implementation (duck typing)"""

    class MockPluginRegistry:
        def __init__(self):
            self._plugins = {
                "test_plugin": {"name": "test"},
                "echo": {"name": "echo"},
            }

        def get(self, name):
            return self._plugins.get(name)

    registry = MockPluginRegistry()

    assert hasattr(registry, "get")
    assert callable(registry.get)
    assert registry.get("test_plugin")["name"] == "test"
    assert registry.get("nonexistent") is None


# ============================================================
# MuteControl Protocol Tests
# ============================================================


@pytest.mark.unit
def test_mute_control_protocol(mock_mute_control):
    """Test MuteControl protocol implementation (duck typing)"""
    assert hasattr(mock_mute_control, "mute_group")
    assert hasattr(mock_mute_control, "unmute_group")
    assert hasattr(mock_mute_control, "is_muted")
    assert hasattr(mock_mute_control, "get_mute_remaining")


@pytest.mark.unit
def test_mute_control_mute_unmute(mock_mute_control):
    """Test muting and unmuting groups"""
    group_id = 12345

    assert mock_mute_control.is_muted(group_id) is False

    mock_mute_control.mute_group(group_id, 10)
    assert mock_mute_control.is_muted(group_id) is True

    mock_mute_control.unmute_group(group_id)
    assert mock_mute_control.is_muted(group_id) is False


@pytest.mark.unit
def test_mute_control_unmute_non_muted(mock_mute_control):
    """Test unmuting a group that isn't muted"""
    result = mock_mute_control.unmute_group(99999)
    assert result is False


@pytest.mark.unit
def test_mute_control_remaining_time(mock_mute_control):
    """Test getting remaining mute time"""
    group_id = 12345

    # Not muted - should return 0
    assert mock_mute_control.get_mute_remaining(group_id) == 0

    # Mute for 1 minute
    mock_mute_control.mute_group(group_id, 1)

    # Should have some time remaining
    remaining = mock_mute_control.get_mute_remaining(group_id)
    assert 50 < remaining <= 60


# ============================================================
# ConfigManagerLike Protocol Tests
# ============================================================


@pytest.mark.unit
def test_config_manager_like_declares_atomic_reload_and_snapshot_contract() -> None:
    from core.config import ConfigManager

    for owner in (ConfigManagerLike, ConfigManager):
        reload_signature = inspect.signature(owner.reload)
        notify           = reload_signature.parameters["notify"]
        assert notify.kind is inspect.Parameter.KEYWORD_ONLY
        assert notify.default is False
        assert hasattr(owner, "snapshot")
        assert hasattr(owner, "on_security_update")
        assert hasattr(owner, "on_pending_secrets_change")


# ============================================================
# PluginConfig Protocol Tests
# ============================================================


@pytest.mark.unit
def test_plugin_config_protocol():
    """Test PluginConfig protocol implementation (duck typing)"""

    class MockPluginConfig:
        def __init__(self):
            self.config  = {"bot_name": "测试"}
            self.secrets = {"admin_ids": [12345]}

    plugin_config = MockPluginConfig()

    assert hasattr(plugin_config, "config")
    assert hasattr(plugin_config, "secrets")
    assert plugin_config.config["bot_name"] == "测试"
    assert plugin_config.secrets["admin_ids"] == [12345]


# ============================================================
# PluginRuntime Protocol Tests
# ============================================================


@pytest.mark.unit
def test_plugin_runtime_protocol():
    """Test PluginRuntime protocol implementation (duck typing)"""

    class MockPluginRuntime:
        def __init__(self):
            self.actions_sent = []

        async def send_action(self, action):
            self.actions_sent.append(action)

        def reload_config(self):
            pass

        def reload_plugins(self):
            pass

        def get_command_catalog(self):
            return ()

        def list_plugins(self):
            return ["test_plugin", "echo"]

    runtime = MockPluginRuntime()

    assert callable(runtime.send_action)
    assert callable(runtime.reload_config)
    assert callable(runtime.reload_plugins)
    assert callable(runtime.get_command_catalog)
    assert callable(runtime.list_plugins)


# ============================================================
# SessionAccess Protocol Tests
# ============================================================


@pytest.mark.unit
def test_session_access_protocol():
    """Test SessionAccess protocol implementation (duck typing)"""

    class MockSessionAccess:
        def __init__(self):
            self.session_manager  = MagicMock()
            self.current_user_id  = 12345
            self.current_group_id = 67890

    access = MockSessionAccess()

    assert hasattr(access, "session_manager")
    assert hasattr(access, "current_user_id")
    assert hasattr(access, "current_group_id")
    assert access.session_manager is not None
    assert access.current_user_id == 12345
    assert access.current_group_id == 67890


@pytest.mark.unit
def test_session_access_with_none_values():
    """Test SessionAccess with None values"""

    class MockSessionAccess:
        def __init__(self):
            self.session_manager  = None
            self.current_user_id  = None
            self.current_group_id = None

    access = MockSessionAccess()

    assert access.current_user_id is None
    assert access.current_group_id is None


# ============================================================
# PluginContext Protocol Tests
# ============================================================


@pytest.mark.unit
def test_plugin_context_protocol():
    """Test PluginContext protocol implementation (duck typing)"""

    class MockPluginContext:
        def __init__(self):
            # PluginConfig
            self.config  = {"test": "value"}
            self.secrets = {"token": "secret"}

            # PluginRuntime
            async def send_action(action):
                pass

            self.send_action         = send_action
            self.reload_config       = lambda: None
            self.reload_plugins      = lambda: None
            self.get_command_catalog = lambda: ()
            self.list_plugins        = lambda: ["test"]

            # SessionAccess
            self.session_manager  = MagicMock()
            self.current_user_id  = 12345
            self.current_group_id = 67890

            # Additional required attributes
            self.plugin_name = "test_plugin"
            self.plugin_dir  = Path("/test/plugin")
            self.data_dir    = Path("/test/data")
            self.logger      = MagicMock()
            self.state       = {"key": "value"}

        def default_groups(self):
            return [123, 456]

    context = MockPluginContext()

    assert hasattr(context, "plugin_name")
    assert hasattr(context, "plugin_dir")
    assert hasattr(context, "data_dir")
    assert hasattr(context, "state")
    assert hasattr(context, "default_groups")

    assert context.plugin_name == "test_plugin"
    assert context.plugin_dir == Path("/test/plugin")
    assert context.data_dir == Path("/test/data")
    assert context.state["key"] == "value"
    assert context.default_groups() == [123, 456]


# ============================================================
# ContextFactory Protocol Tests
# ============================================================


@pytest.mark.unit
def test_context_factory_protocol():
    """Test ContextFactory protocol implementation (duck typing)"""

    class MockContext:
        def __init__(self, plugin_name):
            self.plugin_name = plugin_name

    class MockContextFactory:
        def __call__(self, plugin_name, user_id=None, group_id=None, request_id=None):
            return MockContext(plugin_name)

    factory = MockContextFactory()

    assert callable(factory)

    context = factory("test_plugin", user_id=123, group_id=456, request_id="req-123")
    assert context.plugin_name == "test_plugin"


# ============================================================
# Protocol Compatibility Tests
# ============================================================


@pytest.mark.unit
def test_protocol_compatibility_with_core_context():
    """Test that core.context.PluginContext implements the protocol (duck typing)"""
    from core.context import PluginContext

    context = PluginContext(
        config              = {"test": "value"},
        secrets             = {"token": "secret"},
        plugin_name         = "test",
        plugin_dir          = Path("/test"),
        data_dir            = Path("/test/data"),
        http_session        = None,
        send_action         = AsyncMock(),
        reload_config       = lambda: None,
        reload_plugins      = lambda: None,
        get_command_catalog = lambda: (),
        list_plugins        = lambda: ["test"],
        session_manager     = None,
        current_user_id     = 123,
        current_group_id    = 456,
        request_id          = "req-123",
        state               = {"key": "value"},
    )

    # Verify all required attributes exist (duck typing)
    assert hasattr(context, "config")
    assert hasattr(context, "secrets")
    assert hasattr(context, "plugin_name")
    assert hasattr(context, "plugin_dir")
    assert hasattr(context, "data_dir")
    assert hasattr(context, "state")
    assert hasattr(context, "send_action")
    assert hasattr(context, "reload_config")
    assert hasattr(context, "reload_plugins")
    assert hasattr(context, "get_command_catalog")
    assert hasattr(context, "list_plugins")
    assert hasattr(context, "session_manager")
    assert hasattr(context, "current_user_id")
    assert hasattr(context, "current_group_id")
    assert hasattr(context, "default_groups")


# ============================================================
# Protocol Static Type Checking Tests
# ============================================================


@pytest.mark.unit
def test_protocols_allow_structural_subtyping():
    """Test that protocols use structural subtyping"""
    # These classes don't explicitly inherit from protocols
    # but should be compatible because they have the right methods

    class SimpleAdminCheck:
        def is_admin(self, user_id):
            return user_id == 12345

    class SimpleConfigProvider:
        @property
        def config(self):
            return {"key": "value"}

    # Should be compatible with protocols (duck typing)
    admin_check     = SimpleAdminCheck()
    config_provider = SimpleConfigProvider()

    assert admin_check.is_admin(12345) is True
    assert admin_check.is_admin(99999) is False
    assert config_provider.config["key"] == "value"


# ============================================================
# SendAction Type Tests
# ============================================================


@pytest.mark.unit
def test_send_action_type():
    """Test SendAction callable type"""
    from core.interfaces import SendAction

    async def mock_send_action(action: dict) -> bool:
        return True

    # Should match SendAction type
    send_action: SendAction = mock_send_action
    assert callable(send_action)
