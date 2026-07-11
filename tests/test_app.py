"""
Tests for core/app.py - XiaoQingApp main application class
"""

import asyncio
import copy
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.app import XiaoQingApp, current_action_sink
from core.interfaces import PluginPrincipal
from core.plugin_execution import PluginExecutionGate
from core.plugin_manager import LoadedPlugin, PluginDefinition
from core.server import InboundManager

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
        "enable_plugin_watcher": False,
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


def _set_app_config(app: XiaoQingApp, **updates: Any) -> None:
    config = dict(app.config_manager.config)
    config.update(updates)
    app.config_manager._replace_snapshot(config, app.config_manager._secrets)


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
        assert mock_send.await_args.args[0]["_source_plugin"] == "test"


@pytest.mark.unit
def test_app_plugin_context_scopes_and_freezes_config_and_secrets(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    config = dict(app.config_manager.config)
    config["plugins"] = {"alpha": {"enabled": True}, "beta": {"enabled": True}}
    secrets = {"admin_user_ids": [1], "plugins": {"alpha": {"token": "a"}, "beta": {"token": "b"}}}
    app.config_manager._replace_snapshot(config, secrets)

    context = app._build_plugin_context("alpha", Path("/alpha"), Path("/alpha/data"), {})

    assert context.config["plugins"] == {"alpha": {"enabled": True}}
    assert context.secrets == {"plugins": {"alpha": {"token": "a"}}}
    assert context.config_manager is None
    with pytest.raises(TypeError):
        context.config["plugins"]["alpha"]["enabled"] = False
    with pytest.raises(TypeError):
        context.secrets["plugins"]["alpha"]["token"] = "changed"


# ============================================================
# Lifecycle Tests
# ============================================================


@pytest.mark.asyncio
async def test_reconcile_inbound_restarts_when_proxy_security_declaration_changes(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    old_manager = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=False,
    )
    new_manager = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
    )
    old_manager.stop = AsyncMock()
    new_manager.start = AsyncMock()
    app.inbound_manager = old_manager

    with patch("core.app.InboundManager.from_config", return_value=new_manager):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    old_manager.stop.assert_awaited_once()
    new_manager.start.assert_awaited_once()
    assert app.inbound_manager is new_manager

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
    assert app._plugin_watch_task is None

    # Cleanup
    await app.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_start_creates_shared_http_session_with_default_timeout(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    mock_session = MagicMock()
    mock_session.close = AsyncMock()
    captured: dict[str, Any] = {}

    def _fake_client_session(*args, **kwargs):
        captured.update(kwargs)
        return mock_session

    with patch("core.app.aiohttp.ClientSession", side_effect=_fake_client_session):
        await app.start()
        await app.stop()

    timeout = captured.get("timeout")
    assert timeout is not None
    assert timeout.total == 30.0
    assert timeout.connect == 10.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start_tracks_and_stops_background_tasks(temp_app_root: Path):
    """Test app start/stop manages WS and watch background tasks."""
    config_file = temp_app_root / "config" / "config.json"
    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)
    config["enable_ws_client"] = True
    config["onebot_ws_uri"] = "ws://localhost:6700/ws"
    config["enable_plugin_watcher"] = True
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f)

    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    task_cancelled: dict[str, bool] = {"config": False, "plugin": False, "ws": False}

    async def _block_until_cancelled(marker: str):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancelled[marker] = True
            raise

    async def config_watch(interval: float = 2.0):
        await _block_until_cancelled("config")

    async def plugin_watch():
        await _block_until_cancelled("plugin")

    app.config_manager.watch = config_watch
    app.plugin_manager.watch = plugin_watch

    mock_ws_client = MagicMock()
    mock_ws_client.set_on_connect = Mock()
    mock_ws_client.stop = AsyncMock()

    async def ws_connect_and_listen(handler):
        await _block_until_cancelled("ws")

    mock_ws_client.connect_and_listen = AsyncMock(side_effect=ws_connect_and_listen)

    with patch("core.app.OneBotWsClient", return_value=mock_ws_client):
        await app.start()
        await asyncio.sleep(0)

        assert getattr(app, "_config_watch_task", None) is not None
        assert getattr(app, "_plugin_watch_task", None) is not None
        assert getattr(app, "_ws_client_task", None) is not None

        await app.stop()

    assert task_cancelled["config"] is True
    assert task_cancelled["plugin"] is True
    assert task_cancelled["ws"] is True
    mock_ws_client.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_apply_config_toggles_plugin_watch_task(temp_app_root: Path):
    """Test _apply_config can enable/disable plugin watcher at runtime."""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    task_cancelled = {"plugin": False}

    async def plugin_watch():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancelled["plugin"] = True
            raise

    app.plugin_manager.watch = plugin_watch
    app._config_watch_task = MagicMock()

    app._apply_config(ConfigSnapshot(
        config={**app.config, "enable_plugin_watcher": True},
        secrets=app.secrets,
    ))
    await asyncio.sleep(0)

    assert app._plugin_watch_task is not None

    app._apply_config(ConfigSnapshot(
        config={**app.config, "enable_plugin_watcher": False},
        secrets=app.secrets,
    ))
    await asyncio.sleep(0)

    assert task_cancelled["plugin"] is True
    assert app._plugin_watch_task is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_start_binds_inbound_status_providers(temp_app_root: Path):
    """Test inbound manager gets status providers before startup."""
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    mock_manager = MagicMock()
    mock_manager.start = AsyncMock()

    with patch("core.app.InboundManager.from_config", return_value=mock_manager):
        await app.start()
        await app.stop()

    mock_manager.set_status_providers.assert_called_once()
    mock_manager.start.assert_awaited_once()


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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_stop_continues_after_cleanup_failures(temp_app_root: Path):
    """A failed shutdown hook must not prevent the remaining cleanup phases."""
    app = XiaoQingApp(temp_app_root)
    calls: list[str] = []

    async def fail_inbound() -> None:
        calls.append("inbound")
        raise RuntimeError("inbound stop failed")

    async def fail_ws() -> None:
        calls.append("ws")
        raise RuntimeError("ws stop failed")

    async def fail_http() -> None:
        calls.append("http")
        raise RuntimeError("http close failed")

    async def fail_background_task() -> None:
        raise RuntimeError("watcher failed")

    async def unload_plugin(name: str) -> None:
        calls.append(f"plugin:{name}")
        if name == "broken":
            raise RuntimeError("plugin shutdown failed")

    def fail_scheduler(**_: Any) -> None:
        calls.append("scheduler")
        raise RuntimeError("scheduler stop failed")

    app.inbound_manager = MagicMock(stop=AsyncMock(side_effect=fail_inbound))
    app.ws_client = MagicMock(stop=AsyncMock(side_effect=fail_ws))
    app.http_session = MagicMock(close=AsyncMock(side_effect=fail_http))
    app.scheduler.scheduler.shutdown = Mock(side_effect=fail_scheduler)
    app.plugin_manager.list_plugins = Mock(return_value=["broken", "healthy"])
    app.plugin_manager.unload_plugin = AsyncMock(side_effect=unload_plugin)
    app._config_watch_task = asyncio.create_task(fail_background_task())
    await asyncio.sleep(0)

    await asyncio.wait_for(app.stop(), timeout=1)

    assert calls == [
        "inbound",
        "ws",
        "scheduler",
        "plugin:broken",
        "plugin:healthy",
        "http",
    ]
    assert app.inbound_manager is None
    assert app.ws_client is None
    assert app.http_session is None
    assert app._config_watch_task is None
    assert any("inbound server" in error for error in app._last_shutdown_errors)
    assert any("_config_watch_task" in error for error in app._last_shutdown_errors)
    assert any("plugin broken" in error for error in app._last_shutdown_errors)
    assert any("HTTP session" in error for error in app._last_shutdown_errors)


@pytest.mark.unit
def test_app_ignores_config_and_schedule_updates_while_stopping(temp_app_root: Path):
    """Configuration callbacks cannot recreate runtime components after stop begins."""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app._stopping = True
    app.dispatcher.refresh_prefix_cache = Mock()
    app.scheduler.clear_prefix = Mock()
    app.config_manager.reload = Mock()

    snapshot = ConfigSnapshot(config=app.config, secrets=app.secrets)
    app._apply_config(snapshot)
    app.reload_config()
    app._reschedule("startup")

    app.dispatcher.refresh_prefix_cache.assert_not_called()
    app.config_manager.reload.assert_not_called()
    app.scheduler.clear_prefix.assert_not_called()


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
    """Test _handle_upstream_event falls back when WS is not connected"""
    app = XiaoQingApp(temp_app_root)

    # Mock ws_client as not connected
    app.ws_client = MagicMock()
    app.ws_client.connected = Mock(return_value=False)
    app.ws_client.send_action = AsyncMock()
    app.http_sender = SimpleNamespace(http_base="http://onebot", send_action=AsyncMock())

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(return_value=[{"type": "text", "data": {"text": "test"}}])

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    await app._handle_upstream_event(event)

    # Verify fallback delivery was used
    app.ws_client.send_action.assert_not_called()
    app.http_sender.send_action.assert_called_once()


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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_deduplicates_message_id_across_inbound_transports(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.dispatcher.handle_event = AsyncMock(return_value=[])
    event = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 10000,
        "user_id": 12345,
        "message_id": 99,
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }

    await app._handle_upstream_event(event)
    await app._handle_inbound_event(event)

    app.dispatcher.handle_event.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_does_not_deduplicate_events_without_message_id(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.dispatcher.handle_event = AsyncMock(return_value=[])
    event = {"post_type": "message", "message_type": "private", "user_id": 12345, "message": "same"}

    await app._handle_upstream_event(event)
    await app._handle_inbound_event(event)

    assert app.dispatcher.handle_event.await_count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_falls_back_to_http_when_inbound_has_no_ws_clients(temp_app_root: Path):
    """Test inbound manager does not swallow actions when no inbound WS clients are connected."""
    app = XiaoQingApp(temp_app_root)
    app.inbound_manager = MagicMock()
    app.inbound_manager.has_active_ws_clients = Mock(return_value=False)
    app.inbound_manager.broadcast = AsyncMock()
    app.http_sender = MagicMock()
    app.http_sender.http_base = "http://localhost:5700"
    app.http_sender.send_action = AsyncMock()

    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}
    await app._send_single_action(action)

    app.inbound_manager.broadcast.assert_not_called()
    app.http_sender.send_action.assert_awaited_once_with(action)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_does_not_mutate_bypass_action(temp_app_root: Path):
    """Test internal _bypass_sink marker is stripped from delivery copy only."""
    app = XiaoQingApp(temp_app_root)
    app.inbound_manager = MagicMock()
    app.inbound_manager.has_active_ws_clients = Mock(return_value=False)
    app.http_sender = MagicMock()
    app.http_sender.http_base = "http://localhost:5700"
    app.http_sender.send_action = AsyncMock()

    action = {
        "action": "send_group_msg",
        "params": {"group_id": 1, "message": []},
        "_bypass_sink": True,
    }
    await app._send_single_action(action)

    assert action["_bypass_sink"] is True
    sent_action = app.http_sender.send_action.await_args.args[0]
    assert "_bypass_sink" not in sent_action


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_falls_back_to_http_when_ws_send_fails(temp_app_root: Path):
    """Test WS send failures fall through to HTTP sender."""
    app = XiaoQingApp(temp_app_root)
    app.ws_client = MagicMock()
    app.ws_client.connected = Mock(return_value=True)
    app.ws_client.send_action = AsyncMock(return_value=False)
    app.http_sender = MagicMock()
    app.http_sender.http_base = "http://localhost:5700"
    app.http_sender.send_action = AsyncMock()

    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}
    await app._send_single_action(action)

    app.ws_client.send_action.assert_awaited_once_with(action)
    app.http_sender.send_action.assert_awaited_once_with(action)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_action_propagates_onebot_business_rejection(temp_app_root: Path):
    """Plugin callers can distinguish an acknowledged send from a OneBot rejection."""
    app = XiaoQingApp(temp_app_root)
    app.http_sender = MagicMock()
    app.http_sender.http_base = "http://localhost:5700"
    app.http_sender.send_action = AsyncMock(return_value=False)
    app.plugin_manager.get = Mock(return_value=None)

    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}
    assert await app._send_action(action) is False

    context = app._build_plugin_context("test", Path("/test"), Path("/test"), {})
    assert await context.send_action(action) is False


# ============================================================
# Plugin principal and capability tests
# ============================================================


def _plugin_context_for(
    app: XiaoQingApp,
    plugin_name: str,
    *,
    user_id: int | None = None,
    group_id: int | None = None,
    principal: PluginPrincipal | None = None,
):
    return app._build_plugin_context(
        plugin_name,
        app.root / "plugins" / plugin_name,
        app.root / "plugins" / plugin_name / "data",
        {},
        user_id,
        group_id,
        "capability-test",
        principal,
    )


@pytest.mark.unit
def test_app_grants_only_plugin_scoped_capabilities(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    secrets = {
        "admin_user_ids": [12345],
        "onebot_token": "top-level-token",
        "inbound_token": "inbound-token",
        "plugins": {
            "bot_core": {"own_key": "own-value"},
            "other": {"hidden_key": "hidden-value"},
            "xiaoqing_chat": {"chat_key": "chat-value"},
        },
    }
    mutable_config = json.loads(json.dumps(app.config))
    app.config_manager._replace_snapshot(mutable_config, secrets)
    app._load_admins(secrets)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id=12345,
        group_id=None,
        is_private=True,
    )

    bot_context = _plugin_context_for(
        app,
        "bot_core",
        user_id=12345,
        principal=principal,
    )
    assert bot_context.config_manager is None
    assert set(bot_context.secrets["plugins"]) == {"bot_core"}
    assert "onebot_token" not in bot_context.secrets
    assert bot_context.capabilities.is_bot_admin is True
    assert bot_context.capabilities.is_system is False
    assert bot_context.capabilities.secret_admin is not None
    assert (
        bot_context.capabilities.secret_admin.get("plugins.other.hidden_key")
        == "hidden-value"
    )

    ordinary_context = _plugin_context_for(
        app,
        "other",
        user_id=12345,
        principal=principal,
    )
    assert set(ordinary_context.secrets["plugins"]) == {"other"}
    assert ordinary_context.capabilities.secret_admin is None
    assert ordinary_context.capabilities.onebot_media is None
    assert ordinary_context.capabilities.config_subscription is None

    media_context = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id=12345,
        principal=principal,
    )
    assert media_context.capabilities.onebot_media is not None
    assert "onebot_http_base" not in media_context.config
    assert "onebot_token" not in media_context.secrets

    pendo_context = _plugin_context_for(
        app,
        "pendo",
        user_id=12345,
        principal=principal,
    )
    assert pendo_context.capabilities.config_subscription is not None
    assert pendo_context.capabilities.secret_admin is None


@pytest.mark.unit
def test_app_rejects_forged_copied_and_mismatched_principals(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    issued = app.issue_user_principal(
        {"user_id": 12345},
        user_id=12345,
        group_id=None,
        is_private=True,
    )
    forged = PluginPrincipal(
        kind="user",
        user_id=12345,
        is_bot_admin=True,
        is_private=True,
    )
    copied = copy.copy(issued)
    deep_copied = copy.deepcopy(issued)
    assert copied is not issued
    assert deep_copied is not issued

    for principal in (forged, copied, deep_copied, PluginPrincipal(kind="scheduled_system")):
        with pytest.raises(PermissionError, match="not issued"):
            _plugin_context_for(
                app,
                "test",
                user_id=principal.user_id,
                group_id=principal.group_id,
                principal=principal,
            )

    with pytest.raises(PermissionError, match="do not match"):
        _plugin_context_for(app, "test", user_id=67890, principal=issued)


@pytest.mark.unit
def test_app_recomputes_admin_capability_after_revocation(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id=12345,
        group_id=None,
        is_private=True,
    )
    before = _plugin_context_for(
        app,
        "bot_core",
        user_id=12345,
        principal=principal,
    )
    assert before.capabilities.is_bot_admin is True
    assert before.capabilities.secret_admin is not None

    app._admin_set.clear()
    after = _plugin_context_for(
        app,
        "bot_core",
        user_id=12345,
        principal=principal,
    )
    assert after.capabilities.is_bot_admin is False
    assert after.capabilities.secret_admin is None


@pytest.mark.unit
def test_app_issues_group_role_only_for_matching_sender(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    matching = app.issue_user_principal(
        {"sender": {"user_id": 111, "role": "admin"}},
        user_id=111,
        group_id=222,
        is_private=False,
    )
    mismatched = app.issue_user_principal(
        {"sender": {"user_id": 999, "role": "owner"}},
        user_id=111,
        group_id=222,
        is_private=False,
    )
    private = app.issue_user_principal(
        {"sender": {"user_id": 111, "role": "owner"}},
        user_id=111,
        group_id=None,
        is_private=True,
    )

    assert matching.can_manage_group(222) is True
    assert matching.can_manage_group(333) is False
    assert mismatched.group_role == "unknown"
    assert mismatched.can_manage_group(222) is False
    assert private.group_role == "unknown"
    assert private.can_manage_group(222) is False


@pytest.mark.unit
def test_pendo_config_subscription_is_scoped_and_unsubscribable(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    config = json.loads(json.dumps(app.config))
    config["plugins"] = {
        "pendo": {"web_demo_enabled": True},
        "other": {"private_option": "hidden"},
    }
    secrets = json.loads(json.dumps(app.secrets))
    app.config_manager._replace_snapshot(config, secrets)
    context = _plugin_context_for(app, "pendo")
    subscription = context.capabilities.config_subscription
    assert subscription is not None
    received = []
    unsubscribe = subscription.subscribe(received.append)

    app.config_manager.update_secret("admin_user_ids", [12345])
    unsubscribe()
    unsubscribe()
    app.config_manager.update_secret("admin_user_ids", [12345, 67890])

    assert len(received) == 1
    assert set(received[0]["plugins"]) == {"pendo"}
    assert received[0]["plugins"]["pendo"]["web_demo_enabled"] is True
    assert "other" not in received[0]["plugins"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bot_core_secret_admin_works_with_production_context(temp_app_root: Path):
    from plugins.bot_core import main as bot_core

    app = XiaoQingApp(temp_app_root)
    secrets = {
        "admin_user_ids": [12345],
        "onebot_token": "",
        "inbound_token": "",
        "plugins": {"bot_core": {"managed": "before"}},
    }
    mutable_config = json.loads(json.dumps(app.config))
    app.config_manager._replace_snapshot(mutable_config, secrets)
    app._load_admins(secrets)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id=12345,
        group_id=None,
        is_private=True,
    )
    context = _plugin_context_for(
        app,
        "bot_core",
        user_id=12345,
        principal=principal,
    )

    result = await bot_core.handle(
        "set_secret",
        "plugins.bot_core.managed after",
        {"user_id": 12345, "message_type": "private"},
        context,
    )

    assert "已更新" in result[0]["data"]["text"]
    assert app.config_manager.snapshot().secrets["plugins"]["bot_core"]["managed"] == "after"
    assert "after" not in repr(context.secrets)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_onebot_request_uses_correlated_outbound_transport_only(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    failed_response = {"status": "failed", "retcode": 100, "data": {}}
    app.ws_client = SimpleNamespace(
        connected=lambda: True,
        request_action=AsyncMock(return_value=failed_response),
    )
    app.http_sender = SimpleNamespace(
        http_base="http://onebot",
        request_action=AsyncMock(return_value={"status": "ok", "retcode": 0}),
    )
    app.inbound_manager = SimpleNamespace(broadcast=AsyncMock())

    result = await app._request_onebot_action("get_msg", {"message_id": 42})

    assert result is failed_response
    app.http_sender.request_action.assert_not_awaited()
    app.inbound_manager.broadcast.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_onebot_request_falls_back_to_http_on_ws_transport_failure(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    http_response = {"status": "ok", "retcode": 0, "data": {"message_id": 42}}
    app.ws_client = SimpleNamespace(
        connected=lambda: True,
        request_action=AsyncMock(return_value=None),
    )
    app.http_sender = SimpleNamespace(
        http_base="http://onebot",
        request_action=AsyncMock(return_value=http_response),
    )

    result = await app._request_onebot_action("get_msg", {"message_id": 42})

    assert result is http_response
    app.http_sender.request_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_xiaoqing_media_capability_validates_and_crops_onebot_responses(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app._request_onebot_action = AsyncMock(
        side_effect=[
            {"status": "ok", "retcode": 0, "data": {"message_id": 7, "raw": "ok"}},
            {"status": "ok", "retcode": 0, "data": {"file": "cached.png"}},
            {"status": "failed", "retcode": 100, "data": {"secret": "ignored"}},
        ]
    )
    context = _plugin_context_for(app, "xiaoqing_chat")
    media = context.capabilities.onebot_media
    assert media is not None

    assert await media.get_message(7) == {"message_id": 7, "raw": "ok"}
    assert await media.get_image(file_id="abc") == {"file": "cached.png"}
    assert await media.get_image(file="bad") == {}
    assert app._request_onebot_action.await_args_list[0].args == (
        "get_msg",
        {"message_id": 7},
    )
    assert app._request_onebot_action.await_args_list[1].args == (
        "get_image",
        {"file_id": "abc"},
    )

    with pytest.raises(ValueError):
        await media.get_message(True)
    with pytest.raises(ValueError):
        await media.get_image()
    with pytest.raises(ValueError):
        await media.get_image(file_id="a", file="b")
    with pytest.raises(ValueError):
        await media.get_image(file_id=1)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cross_plugin_call_preserves_signed_principal_and_target_scope(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    secrets = {
        "admin_user_ids": [12345],
        "onebot_token": "",
        "inbound_token": "",
        "plugins": {"source": {"source_key": "s"}, "target": {"target_key": "t"}},
    }
    config = json.loads(json.dumps(app.config))
    config["plugins"] = {"source": {"source_option": 1}, "target": {"target_option": 2}}
    app.config_manager._replace_snapshot(config, secrets)
    app._load_admins(secrets)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id=12345,
        group_id=None,
        is_private=True,
    )
    seen_contexts = []

    async def exported(value, context):
        seen_contexts.append(context)
        return value

    module = ModuleType("plugins.target.main")
    module.exported = exported
    definition = PluginDefinition(
        name="target",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[],
        concurrency="sequential",
    )
    app.plugin_manager._plugins["target"] = LoadedPlugin(
        definition=definition,
        module=module,
        mtime=0.0,
        execution_gate=PluginExecutionGate("sequential", plugin_name="target"),
    )
    source = _plugin_context_for(
        app,
        "source",
        user_id=12345,
        principal=principal,
    )

    assert await source.call_plugin("target", "exported", "first") == "first"
    first = seen_contexts[-1]
    assert first.principal is principal
    assert first.capabilities.is_bot_admin is True
    assert set(first.secrets["plugins"]) == {"target"}
    assert set(first.config["plugins"]) == {"target"}
    assert first.state is app.plugin_manager._plugin_states["target"]

    app._admin_set.clear()
    assert await source.call_plugin("target", "exported", "second") == "second"
    assert seen_contexts[-1].capabilities.is_bot_admin is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_run_job_builds_real_system_capability_context(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    captured = []

    async def handler(context):
        captured.append(context)
        return []

    await app._run_job(handler, "scheduled-test", group_ids=[123, 456])

    assert len(captured) == 1
    context = captured[0]
    assert context.principal.kind == "scheduled_system"
    assert context.principal.user_id is None
    assert context.principal.group_id is None
    assert context.capabilities.is_system is True
    assert context.capabilities.is_bot_admin is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_xiaoqing_provider_scope_uses_production_principal_capabilities(
    temp_app_root: Path,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.helper_utils import _get_llm_secrets
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    app = XiaoQingApp(temp_app_root)
    secrets = json.loads(json.dumps(app.secrets))
    secrets["plugins"] = {
        "xiaoqing_chat": {
            "default": "deepseek",
            "providers": {
                "deepseek": {"model": "deepseek-chat", "api_base": "https://a.example"},
                "glm": {"model": "glm-4", "api_base": "https://b.example"},
            },
        }
    }
    app.config_manager._replace_snapshot(json.loads(json.dumps(app.config)), secrets)
    app._load_admins(secrets)
    group_admin_event = {
        "user_id": 777,
        "group_id": 100,
        "sender": {"user_id": 777, "role": "admin"},
    }
    group_admin = app.issue_user_principal(
        group_admin_event,
        user_id=777,
        group_id=100,
        is_private=False,
    )
    group_context = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id=777,
        group_id=100,
        principal=group_admin,
    )
    state = ChatRuntimeState()

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.helper_utils._state", return_value=state),
    ):
        local_result = await handle_provider("glm", group_admin_event, group_context)
        denied_global = await handle_provider("global glm", group_admin_event, group_context)
        group_a = _get_llm_secrets(group_context, chat_id="g100")
        group_b = _get_llm_secrets(group_context, chat_id="g200")

        bot_admin_event = {
            "user_id": 12345,
            "group_id": 200,
            "sender": {"user_id": 12345, "role": "member"},
        }
        bot_admin = app.issue_user_principal(
            bot_admin_event,
            user_id=12345,
            group_id=200,
            is_private=False,
        )
        bot_context = _plugin_context_for(
            app,
            "xiaoqing_chat",
            user_id=12345,
            group_id=200,
            principal=bot_admin,
        )
        global_result = await handle_provider("global glm", bot_admin_event, bot_context)

    assert "当前会话供应商" in local_result[0]["data"]["text"]
    assert "Bot 全局管理员" in denied_global[0]["data"]["text"]
    assert group_a["_provider_name"] == "glm"
    assert group_b["_provider_name"] == "deepseek"
    assert "全局运行时供应商" in global_result[0]["data"]["text"]
    assert state.global_active_provider == "glm"


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


@pytest.mark.unit
def test_app_apply_config_reuses_dispatcher_semaphore_when_concurrency_unchanged(
    temp_app_root: Path,
):
    """Test _apply_config only replaces dispatcher semaphore when concurrency changes."""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    original = app.dispatcher.semaphore

    same_config = dict(app.config)
    same_config["max_concurrency"] = app._dispatcher_concurrency
    app._apply_config(ConfigSnapshot(config=same_config, secrets=app.secrets))

    assert app.dispatcher.semaphore is original

    changed_config = dict(same_config)
    changed_config["max_concurrency"] = app._dispatcher_concurrency + 1
    app._apply_config(ConfigSnapshot(config=changed_config, secrets=app.secrets))

    assert app.dispatcher.semaphore is not original


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
    app.plugin_manager.build_context.assert_called_once()
    call = app.plugin_manager.build_context.call_args
    assert call.args == ("test_plugin",)
    assert call.kwargs["principal"].kind == "scheduled_system"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_uses_plugin_sequential_gate(temp_app_root: Path):
    """Scheduled calls share the same manifest gate as message handlers."""
    from core.plugin_execution import PluginExecutionGate

    app = XiaoQingApp(temp_app_root)
    gate = PluginExecutionGate("sequential")
    loaded = SimpleNamespace(execution_gate=gate)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def slow_handler(_context):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await release.wait()
        active -= 1
        return []

    app.plugin_manager.get = Mock(return_value=loaded)
    app.plugin_manager.build_context = Mock(return_value=MagicMock())

    first = asyncio.create_task(app._run_job(slow_handler, "stateful"))
    await entered.wait()
    second = asyncio.create_task(app._run_job(slow_handler, "stateful"))
    await asyncio.sleep(0)
    assert max_active == 1

    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_send_action_notifies_xiaoqing_for_external_plugin_text(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    observer = AsyncMock(return_value=[])
    loaded = SimpleNamespace(module=SimpleNamespace(observe_outgoing_action=observer))
    xiaoqing_context = MagicMock()
    app.plugin_manager.get = Mock(return_value=loaded)
    app.plugin_manager.build_context = Mock(return_value=xiaoqing_context)
    app._send_single_action = AsyncMock(return_value=True)

    action = {
        "action": "send_group_msg",
        "params": {
            "group_id": 123,
            "message": [{"type": "text", "data": {"text": "地震播报"}}],
        },
        "_source_plugin": "earthquake",
    }

    await app._send_action(action)

    app.plugin_manager.get.assert_called_once_with("xiaoqing_chat")
    app.plugin_manager.build_context.assert_called_once()
    build_call = app.plugin_manager.build_context.call_args
    assert build_call.args == ("xiaoqing_chat",)
    assert build_call.kwargs["user_id"] is None
    assert build_call.kwargs["group_id"] == 123
    assert build_call.kwargs["principal"].kind == "lifecycle"
    observer.assert_awaited_once_with(
        action,
        xiaoqing_context,
        source_plugin="earthquake",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_send_action_does_not_notify_xiaoqing_for_xiaoqing_source(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.get = Mock()

    await app._send_action(
        {
            "action": "send_group_msg",
            "params": {
                "group_id": 123,
                "message": [{"type": "text", "data": {"text": "小青回复"}}],
            },
            "_source_plugin": "xiaoqing_chat",
        }
    )

    app.plugin_manager.get.assert_not_called()


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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_swallows_cancelled_error(temp_app_root: Path):
    """Test _run_job treats cancellation as normal shutdown."""
    app = XiaoQingApp(temp_app_root)

    async def mock_handler(context):
        raise asyncio.CancelledError()

    app.plugin_manager.build_context = Mock(return_value=MagicMock())

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


@pytest.mark.unit
def test_app_reschedule_skips_manifest_disabled_schedule(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    module = MagicMock()
    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[
            {
                "id": "disabled",
                "handler": "scheduled",
                "cron": {"minute": "*"},
                "enabled": False,
            }
        ],
        concurrency="parallel",
    )
    loaded = LoadedPlugin(definition=definition, module=module, mtime=0.0)
    app.scheduler.clear_prefix = Mock()
    app.scheduler.add_job = Mock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[loaded])

    app._reschedule("startup")

    app.scheduler.add_job.assert_not_called()


@pytest.mark.unit
def test_app_reschedule_preserves_manifest_schedule_description(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    module = MagicMock()
    module.scheduled = AsyncMock()
    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[
            {
                "id": "enabled",
                "handler": "scheduled",
                "cron": {"minute": "*"},
                "description": "visible scheduler metadata",
                "enabled": True,
            }
        ],
        concurrency="parallel",
    )
    loaded = LoadedPlugin(definition=definition, module=module, mtime=0.0)
    app.scheduler.clear_prefix = Mock()
    app.scheduler.add_job = Mock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[loaded])

    app._reschedule("startup")

    app.scheduler.add_job.assert_called_once()
    assert app.scheduler.add_job.call_args.kwargs["description"] == "visible scheduler metadata"


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

    with patch("asyncio.sleep", side_effect=[None, stop_exc]):
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
    _set_app_config(app, default_group_ids=[123, 456])
    
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
    _set_app_config(app, default_group_ids=[])

    # Mock ws_client
    app.ws_client = MagicMock()
    app.ws_client.send_action = AsyncMock()

    # Mock _send_action
    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await app._on_ws_connected()

        # No messages should be sent
        mock_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_throttles_reconnect_notifications(temp_app_root: Path):
    """Test _on_ws_connected suppresses repeated reconnect notifications"""
    app = XiaoQingApp(temp_app_root)
    _set_app_config(
        app,
        default_group_ids=[123],
        connect_notification_min_interval_seconds=300,
    )
    app.ws_client = MagicMock()

    with (
        patch("core.app.time.monotonic", side_effect=[100.0, 120.0, 450.0]),
        patch.object(app, "_send_action", new=AsyncMock()) as mock_send,
    ):
        await app._on_ws_connected()
        await app._on_ws_connected()
        await app._on_ws_connected()

        assert mock_send.call_count == 2
