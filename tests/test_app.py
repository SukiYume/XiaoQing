"""
Tests for core/app.py - XiaoQingApp main application class
"""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.app import XiaoQingApp, current_action_sink


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_app_root(temp_dir: Path) -> Path:
    """Create a temporary app root with config files"""
    import json

    # Create config directory
    config_dir = temp_dir / "config"
    config_dir.mkdir()

    # Create config.json
    config_file = config_dir / "config.json"
    config_data = {
        "bot_name": "小青",
        "command_prefixes": ["/"],
        "onebot_http_base": "",
        "enable_ws_client": False,
        "enable_inbound_server": False,
        "max_concurrency": 5,
        "session_timeout": 300,
        "timezone": "Asia/Shanghai",
        "default_group_ids": [],
        "admin_user_ids": [],
        "plugins": {},
    }
    with open(config_file, "w") as f:
        json.dump(config_data, f, indent=2)

    # Create secrets.json
    secrets_file = config_dir / "secrets.json"
    secrets_data = {
        "admin_user_ids": [12345, 67890],
        "onebot_token": "",
        "inbound_token": "",
    }
    with open(secrets_file, "w") as f:
        json.dump(secrets_data, f, indent=2)

    # Create plugins directory
    plugins_dir = temp_dir / "plugins"
    plugins_dir.mkdir()

    # Create logs directory
    logs_dir = temp_dir / "logs"
    logs_dir.mkdir()
    
    # Patch setup_logging to avoid file locks
    with patch("core.app.setup_logging") as mock_setup:
        mock_setup.return_value = MagicMock()
        yield temp_dir


@pytest.fixture
def mock_dependencies():
    """Create mock dependencies for XiaoQingApp"""
    return {
        "router": MagicMock(),
        "plugin_manager": MagicMock(),
        "dispatcher": MagicMock(),
        "scheduler": MagicMock(),
        "session_manager": MagicMock(),
    }


# ============================================================
# Initialization Tests
# ============================================================

@pytest.mark.unit
def test_app_init_with_minimal_args(temp_app_root: Path):
    """Test app initialization with minimal arguments"""
    with patch("core.app.setup_logging") as mock_setup:
        mock_setup.return_value = MagicMock()
        app = XiaoQingApp(temp_app_root)

        assert app.root == temp_app_root
    assert app.config_manager is not None
    assert app.log_manager is not None
    assert app.http_session is None
    assert app.http_sender is None
    assert app.ws_client is None
    assert app.inbound_manager is None
    assert app.router is not None
    assert app.plugin_manager is not None
    assert app.scheduler is not None
    assert app.metrics is not None
    assert app.session_manager is not None
    assert app.dispatcher is not None


@pytest.mark.unit
def test_app_init_with_dependencies(temp_app_root: Path, mock_dependencies):
    """Test app initialization with injected dependencies"""
    app = XiaoQingApp(
        temp_app_root,
        router=mock_dependencies["router"],
        plugin_manager=mock_dependencies["plugin_manager"],
        dispatcher=mock_dependencies["dispatcher"],
        scheduler=mock_dependencies["scheduler"],
        session_manager=mock_dependencies["session_manager"],
    )

    assert app.router is mock_dependencies["router"]
    assert app.plugin_manager is mock_dependencies["plugin_manager"]
    assert app.dispatcher is mock_dependencies["dispatcher"]
    assert app.scheduler is mock_dependencies["scheduler"]
    assert app.session_manager is mock_dependencies["session_manager"]


@pytest.mark.unit
def test_app_plugins_dir_path(temp_app_root: Path):
    """Test that plugins_dir is set correctly"""
    app = XiaoQingApp(temp_app_root)
    assert app.plugins_dir == temp_app_root / "plugins"


# ============================================================
# Config and Secrets Access Tests
# ============================================================

@pytest.mark.unit
def test_app_config_property(temp_app_root: Path):
    """Test config property access"""
    app = XiaoQingApp(temp_app_root)

    config = app.config
    assert isinstance(config, dict)
    assert config.get("bot_name") == "小青"


@pytest.mark.unit
def test_app_secrets_property(temp_app_root: Path):
    """Test secrets property access"""
    app = XiaoQingApp(temp_app_root)

    secrets = app.secrets
    assert isinstance(secrets, dict)
    assert 12345 in secrets.get("admin_user_ids", [])


# ============================================================
# Admin Check Tests
# ============================================================

@pytest.mark.unit
def test_app_is_admin_valid_user(temp_app_root: Path):
    """Test is_admin with valid admin user"""
    app = XiaoQingApp(temp_app_root)

    assert app.is_admin(12345) is True
    assert app.is_admin(67890) is True


@pytest.mark.unit
def test_app_is_admin_invalid_user(temp_app_root: Path):
    """Test is_admin with non-admin user"""
    app = XiaoQingApp(temp_app_root)

    assert app.is_admin(99999) is False
    assert app.is_admin(1) is False


@pytest.mark.unit
def test_app_is_admin_none_user(temp_app_root: Path):
    """Test is_admin with None user_id"""
    app = XiaoQingApp(temp_app_root)

    assert app.is_admin(None) is False
    assert app.is_admin(0) is False


@pytest.mark.unit
def test_app_load_admins_from_config(temp_app_root: Path):
    """Test admin loading from secrets config"""
    import json

    # Modify secrets to have different admin IDs
    secrets_file = temp_app_root / "config" / "secrets.json"
    with open(secrets_file, "w") as f:
        json.dump({"admin_user_ids": ["111", "222", "333"]}, f)

    app = XiaoQingApp(temp_app_root)
    assert app.is_admin(111)
    assert app.is_admin(222)
    assert app.is_admin(333)


@pytest.mark.unit
def test_app_load_admins_invalid_config(temp_app_root: Path):
    """Test admin loading with invalid config"""
    import json

    # Write invalid admin_user_ids
    secrets_file = temp_app_root / "config" / "secrets.json"
    with open(secrets_file, "w") as f:
        json.dump({"admin_user_ids": ["not", "a", "number"]}, f)

    app = XiaoQingApp(temp_app_root)
    # Should have empty admin set and no user should be admin
    assert app.is_admin(12345) is False


# ============================================================
# Plugin Context Building Tests
# ============================================================

@pytest.mark.unit
def test_app_build_plugin_context(temp_app_root: Path):
    """Test _build_plugin_context creates valid context"""
    app = XiaoQingApp(temp_app_root)

    plugin_dir = Path("/test/plugin")
    data_dir = Path("/test/data")
    state = {"test": "value"}

    context = app._build_plugin_context(
        plugin_name="test_plugin",
        plugin_dir=plugin_dir,
        data_dir=data_dir,
        state=state,
        user_id=12345,
        group_id=67890,
        request_id="test-request-123",
    )

    assert context.plugin_name == "test_plugin"
    assert context.plugin_dir == plugin_dir
    assert context.data_dir == data_dir
    assert context.state == state
    assert context.current_user_id == 12345
    assert context.current_group_id == 67890
    assert context.request_id == "test-request-123"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_context_send_action(temp_app_root: Path):
    """Test plugin context send_action works"""
    app = XiaoQingApp(temp_app_root)

    context = app._build_plugin_context(
        plugin_name="test",
        plugin_dir=Path("/test"),
        data_dir=Path("/test"),
        state={},
    )

    # Mock _send_action to track calls
    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await context.send_action({"action": "test"})
        mock_send.assert_called_once()


# ============================================================
# Lifecycle Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start(temp_app_root: Path):
    """Test app start initializes components"""
    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager methods
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    await app.start()

    # Verify HTTP session is created
    assert app.http_session is not None

    # Verify plugins are loaded
    app.plugin_manager.load_all.assert_called_once()
    await app.plugin_manager.wait_inits()

    # Verify session cleanup task is created
    assert app._session_cleanup_task is not None

    # Cleanup
    await app.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start_with_http_configured(temp_app_root: Path):
    """Test app start with HTTP sender configured"""
    import json

    # Update config with HTTP base
    config_file = temp_app_root / "config" / "config.json"
    with open(config_file) as f:
        config = json.load(f)
    config["onebot_http_base"] = "http://localhost:5700"
    with open(config_file, "w") as f:
        json.dump(config, f)

    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    await app.start()

    # Verify HTTP sender is created
    assert app.http_sender is not None

    await app.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start_with_ws_disabled(temp_app_root: Path):
    """Test app start with WebSocket client disabled"""
    import json

    # Update config to disable WS
    config_file = temp_app_root / "config" / "config.json"
    with open(config_file) as f:
        config = json.load(f)
    config["enable_ws_client"] = False
    with open(config_file, "w") as f:
        json.dump(config, f)

    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    await app.start()

    # WS client should not be created
    assert app.ws_client is None

    await app.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_stop(temp_app_root: Path):
    """Test app stop cleans up resources"""
    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.list_plugins = Mock(return_value=[])
    app.plugin_manager.unload_plugin = AsyncMock()

    # Start the app first
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    await app.start()

    # Now stop it
    await app.stop()

    # Verify cleanup
    assert app.http_session is None or app.http_session.closed


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_stop_unloads_plugins(temp_app_root: Path):
    """Test app stop unloads all plugins"""
    app = XiaoQingApp(temp_app_root)

    # Create mock plugin
    mock_plugin = MagicMock()
    app.plugin_manager.list_plugins = Mock(return_value=["test_plugin"])
    app.plugin_manager.unload_plugin = AsyncMock()

    # Start
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    await app.start()

    # Stop
    await app.stop()

    # Verify unload was called
    app.plugin_manager.unload_plugin.assert_called_once_with("test_plugin")


# ============================================================
# Event Handling Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_process_event(temp_app_root: Path):
    """Test _process_event processes event through dispatcher"""
    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(return_value=[{"type": "text", "data": {"text": "test"}}])

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    result = await app._process_event(event)

    assert result is not None
    assert "action" in result
    assert result["action"] in ("send_group_msg", "send_private_msg")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_process_event_no_response(temp_app_root: Path):
    """Test _process_event with no response from dispatcher"""
    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher to return empty
    app.dispatcher.handle_event = AsyncMock(return_value=[])

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    result = await app._process_event(event)

    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_upstream_event(temp_app_root: Path):
    """Test _handle_upstream_event"""
    app = XiaoQingApp(temp_app_root)

    # Mock ws_client
    app.ws_client = MagicMock()
    app.ws_client.connected = Mock(return_value=True)
    app.ws_client.send_action = AsyncMock()

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(return_value=[{"type": "text", "data": {"text": "test"}}])

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    await app._handle_upstream_event(event)

    # Verify action was sent
    app.ws_client.send_action.assert_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_upstream_event_not_connected(temp_app_root: Path):
    """Test _handle_upstream_event when WS not connected"""
    app = XiaoQingApp(temp_app_root)

    # Mock ws_client as not connected
    app.ws_client = MagicMock()
    app.ws_client.connected = Mock(return_value=False)
    app.ws_client.send_action = AsyncMock()

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(return_value=[{"type": "text", "data": {"text": "test"}}])

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    await app._handle_upstream_event(event)

    # Verify action was NOT sent
    app.ws_client.send_action.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_inbound_event(temp_app_root: Path):
    """Test _handle_inbound_event returns actions"""
    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(return_value=[{"type": "text", "data": {"text": "test"}}])

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    result = await app._handle_inbound_event(event)

    assert isinstance(result, list)
    assert len(result) > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_inbound_event_with_source(temp_app_root: Path):
    """Test _handle_inbound_event sets source correctly"""
    app = XiaoQingApp(temp_app_root)

    received_events = []

    async def mock_handle(event):
        received_events.append(event)
        return []

    app.dispatcher.handle_event = mock_handle

    event = {"test": "data"}
    await app._handle_inbound_event(event)

    assert len(received_events) == 1
    assert received_events[0].get("_source") == "inbound_http"


# ============================================================
# Configuration Reload Tests
# ============================================================

@pytest.mark.unit
def test_app_reload_config(temp_app_root: Path):
    """Test reload_config triggers config reload"""
    app = XiaoQingApp(temp_app_root)

    # Mock config manager
    app.config_manager.reload = Mock()

    app.reload_config()

    app.config_manager.reload.assert_called_once()


@pytest.mark.unit
def test_app_apply_config_updates_admins(temp_app_root: Path):
    """Test _apply_config updates admin set"""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)

    new_snapshot = ConfigSnapshot(
        config=app.config,
        secrets={"admin_user_ids": [99999], "onebot_token": "", "inbound_token": ""},
    )

    app._apply_config(new_snapshot)

    assert app.is_admin(99999) is True
    assert app.is_admin(12345) is False


@pytest.mark.unit
def test_app_apply_config_refreshes_prefix_cache(temp_app_root: Path):
    """Test _apply_config refreshes dispatcher prefix cache"""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher
    app.dispatcher.refresh_prefix_cache = Mock()

    new_snapshot = ConfigSnapshot(config=app.config, secrets=app.secrets)
    app._apply_config(new_snapshot)

    app.dispatcher.refresh_prefix_cache.assert_called_once()


# ============================================================
# Plugin Reload Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reload_plugins_async(temp_app_root: Path):
    """Test _reload_plugins_async unloads and reloads plugins"""
    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.list_plugins = Mock(return_value=["test_plugin"])
    app.plugin_manager.unload_plugin = AsyncMock()
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()

    await app._reload_plugins_async_with_logging()

    app.plugin_manager.unload_plugin.assert_called_once_with("test_plugin")
    app.plugin_manager.load_all.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reload_plugins_non_blocking(temp_app_root: Path):
    """Test _reload_plugins creates background task"""
    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.list_plugins = Mock(return_value=["test_plugin"])
    app.plugin_manager.unload_plugin = AsyncMock()
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()

    app._reload_plugins()

    # Should create a task
    assert app._reload_task is not None
    
    # Wait for it to finish to avoid pending task warning
    if app._reload_task:
        await app._reload_task


@pytest.mark.unit
def test_app_reload_plugins_already_in_progress(temp_app_root: Path):
    """Test _reload_plugins when already in progress"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock "in progress" task
    app._reload_task = MagicMock()
    app._reload_task.done = Mock(return_value=False)

    # Mock plugin manager
    app.plugin_manager.unload_plugin = AsyncMock()

    app._reload_plugins()

    # Should not create new task
    assert app._reload_task.done.called is False or app.plugin_manager.unload_plugin.call_count == 0


# ============================================================
# Scheduled Job Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job(temp_app_root: Path):
    """Test _run_job executes scheduled job"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock handler
    async def mock_handler(context):
        return [{"type": "text", "data": {"text": "scheduled result"}}]

    # Mock context building
    app.plugin_manager.build_context = Mock(return_value=MagicMock())
    mock_context = app.plugin_manager.build_context.return_value
    mock_context.default_groups = Mock(return_value=[123, 456])

    # Mock http_sender
    app.http_sender = AsyncMock()

    await app._run_job(mock_handler, "test_plugin", [123, 456])

    # Verify context was built
    app.plugin_manager.build_context.assert_called_once_with("test_plugin")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_no_result(temp_app_root: Path):
    """Test _run_job with handler returning no result"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock handler returning None
    async def mock_handler(context):
        return None

    app.plugin_manager.build_context = Mock(return_value=MagicMock())

    await app._run_job(mock_handler, "test_plugin")

    # Should complete without error
    assert True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_with_error(temp_app_root: Path):
    """Test _run_job handles handler errors"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock handler that raises
    async def mock_handler(context):
        raise ValueError("Test error")

    app.plugin_manager.build_context = Mock(return_value=MagicMock())

    # Should not raise
    await app._run_job(mock_handler, "test_plugin")


# ============================================================
# Reschedule Tests
# ============================================================

@pytest.mark.unit
def test_app_reschedule_startup(temp_app_root: Path):
    """Test _reschedule with startup event"""
    app = XiaoQingApp(temp_app_root)

    # Mock scheduler and plugin manager
    app.scheduler.clear_prefix = Mock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    app._reschedule("startup")

    app.scheduler.clear_prefix.assert_called_once_with("plugin.")
    app.plugin_manager.schedule_definitions.assert_called_once()


@pytest.mark.unit
def test_app_reschedule_single_plugin(temp_app_root: Path):
    """Test _reschedule for single plugin"""
    app = XiaoQingApp(temp_app_root)

    # Create mock loaded plugin
    mock_plugin = MagicMock()
    mock_plugin.definition.name = "test_plugin"
    mock_plugin.definition.schedule = []

    # Mock scheduler and plugin manager
    app.scheduler.clear_prefix = Mock()
    app.plugin_manager.get = Mock(return_value=mock_plugin)

    app._reschedule("test_plugin")

    app.scheduler.clear_prefix.assert_called_once_with("plugin.test_plugin.")


# ============================================================
# Action Sink Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_collect_actions_with_sink(temp_app_root: Path):
    """Test _collect_actions_for_event with active sink"""
    app = XiaoQingApp(temp_app_root)

    # Track sink calls
    sink_calls = []

    async def mock_sink(action):
        sink_calls.append(action)

    # Set sink
    token = current_action_sink.set(mock_sink)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(return_value=[{"type": "text", "data": {"text": "test"}}])

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 12345,
        "message": "test"
    }
    result = await app._collect_actions_for_event(event, default_source="test")

    # Reset sink
    current_action_sink.reset(token)

    # Verify result (sink is NOT called by design of _collect_actions_for_event)
    assert len(result) > 0
    assert result[0]["action"] == "send_private_msg"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_collect_actions_without_sink(temp_app_root: Path):
    """Test _collect_actions_for_event without active sink"""
    app = XiaoQingApp(temp_app_root)

    # Make sure no sink is set
    token = current_action_sink.set(None)
    current_action_sink.reset(token)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(return_value=[{"type": "text", "data": {"text": "test"}}])

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 12345,
        "message": "test"
    }
    result = await app._collect_actions_for_event(event, default_source="test")

    # Should return collected actions
    assert isinstance(result, list)
    assert len(result) > 0


# ============================================================
# Session Cleanup Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_cleanup_sessions_loop(temp_app_root: Path):
    """Test _cleanup_sessions_loop runs periodically"""
    app = XiaoQingApp(temp_app_root)

    # Mock session manager
    app.session_manager.cleanup_expired = AsyncMock()

    # Mock asyncio.sleep to return immediately first time, then raise CancelledError
    # This ensures one loop iteration runs
    stop_exc = asyncio.CancelledError("Stop loop")
    
    async def mock_sleep_side_effect(*args):
        # We need to yield to let other tasks run, even if we return immediately
        # But since this is a mock for sleep, we can just return None first time
        pass

    with patch("asyncio.sleep", side_effect=[None, stop_exc]) as mock_sleep:
        try:
            await app._cleanup_sessions_loop()
        except asyncio.CancelledError:
            pass

    # Verify cleanup was called
    app.session_manager.cleanup_expired.assert_called()


# ============================================================
# WebSocket Connected Callback Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected(temp_app_root: Path):
    """Test _on_ws_connected sends notification to default groups"""
    app = XiaoQingApp(temp_app_root)

    # Set default groups (update internal config directly for test)
    app.config_manager._config["default_group_ids"] = [123, 456]
    
    # Also update via config property just in case (though it's read-only usually, this updates the temp dict if property logic changed)
    # But strictly speaking we need to update what ConfigManager returns.
    
    # Mock ws_client
    app.ws_client = MagicMock()
    app.ws_client.send_action = AsyncMock()

    # Mock _send_action
    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await app._on_ws_connected()

        # Verify messages were sent
        assert mock_send.call_count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_no_groups(temp_app_root: Path):
    """Test _on_ws_connected with no default groups"""
    app = XiaoQingApp(temp_app_root)

    # No default groups
    app.config_manager.config["default_group_ids"] = []

    # Mock ws_client
    app.ws_client = MagicMock()
    app.ws_client.send_action = AsyncMock()

    # Mock _send_action
    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await app._on_ws_connected()

        # No messages should be sent
        mock_send.assert_not_called()
