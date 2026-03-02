"""
Pytest configuration and shared fixtures for XiaoQing tests
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Suppress logging during tests unless explicitly enabled
logging.getLogger().setLevel(logging.WARNING)

# ============================================================
# Test Configuration
# ============================================================

def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow tests")
    config.addinivalue_line("markers", "async: Async tests")
    config.addinivalue_line("markers", "plugin: Plugin tests")
    config.addinivalue_line("markers", "core: Core module tests")

# ============================================================
# Path Fixtures
# ============================================================

@pytest.fixture
def project_root() -> Path:
    """Get the project root directory"""
    return ROOT


@pytest.fixture
def tests_dir() -> Path:
    """Get the tests directory"""
    return ROOT / "tests"


@pytest.fixture
def plugins_dir() -> Path:
    """Get the plugins directory"""
    return ROOT / "plugins"


@pytest.fixture
def config_dir() -> Path:
    """Get the config directory"""
    return ROOT / "config"


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    """Create a temporary directory for tests"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)

# ============================================================
# Config Fixtures
# ============================================================

@pytest.fixture
def sample_config() -> dict[str, Any]:
    """Sample configuration data"""
    return {
        "bot_name": "小青",
        "command_prefixes": ["/"],
        "require_bot_name_in_group": True,
        "random_reply_rate": 0.05,
        "enable_ws_client": False,
        "enable_inbound_server": True,
        "inbound_server_port": 8080,
        "plugins": {
            "smalltalk_provider": "smalltalk",
            "echo": {"enabled": True},
            "choice": {"enabled": True},
        },
    }


@pytest.fixture
def sample_secrets() -> dict[str, Any]:
    """Sample secrets configuration"""
    return {
        "admin_user_ids": [12345, 67890],
        "plugins": {
            "echo": {},
            "choice": {},
        },
    }


@pytest.fixture
def temp_config_file(temp_dir: Path, sample_config: dict[str, Any]) -> Path:
    """Create a temporary config file"""
    config_path = temp_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(sample_config, f, indent=2, ensure_ascii=False)
    return config_path


@pytest.fixture
def temp_secrets_file(temp_dir: Path, sample_secrets: dict[str, Any]) -> Path:
    """Create a temporary secrets file"""
    secrets_path = temp_dir / "secrets.json"
    with open(secrets_path, "w", encoding="utf-8") as f:
        json.dump(sample_secrets, f, indent=2, ensure_ascii=False)
    return secrets_path

# ============================================================
# Event Fixtures
# ============================================================

@pytest.fixture
def sample_group_event() -> dict[str, Any]:
    """Sample group message event"""
    return {
        "post_type": "message",
        "message_type": "group",
        "time": 1234567890,
        "self_id": 11111,
        "user_id": 12345,
        "group_id": 67890,
        "message": [
            {"type": "text", "data": {"text": "/echo hello"}},
        ],
        "raw_message": "/echo hello",
        "font": 0,
        "sender": {
            "user_id": 12345,
            "nickname": "TestUser",
            "card": "",
            "sex": "unknown",
            "age": 0,
            "area": "",
            "level": "",
            "role": "member",
            "title": "",
        },
        "message_id": 1,
        "message_seq": 1,
    }


@pytest.fixture
def sample_private_event() -> dict[str, Any]:
    """Sample private message event"""
    return {
        "post_type": "message",
        "message_type": "private",
        "time": 1234567890,
        "self_id": 11111,
        "user_id": 12345,
        "message": "你好",
        "raw_message": "你好",
        "font": 0,
        "sender": {
            "user_id": 12345,
            "nickname": "TestUser",
            "sex": "unknown",
            "age": 0,
        },
        "message_id": 1,
    }


@pytest.fixture
def sample_at_event() -> dict[str, Any]:
    """Sample event with @ mention"""
    return {
        "post_type": "message",
        "message_type": "group",
        "time": 1234567890,
        "self_id": 11111,
        "user_id": 12345,
        "group_id": 67890,
        "message": [
            {"type": "at", "data": {"qq": "11111"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
        "raw_message": "[@11111] 你好",
        "sender": {
            "user_id": 12345,
            "nickname": "TestUser",
        },
    }

# ============================================================
# Mock Fixtures
# ============================================================

@pytest.fixture
def mock_http_session():
    """Mock HTTP session"""
    session = AsyncMock()
    session.get = AsyncMock()
    session.post = AsyncMock()
    return session


@pytest.fixture
def mock_send_action():
    """Mock send_action function"""
    return AsyncMock()


@pytest.fixture
def mock_context_factory():
    """Mock context factory"""
    def _factory(
        name: str,
        plugin_dir: Path,
        data_dir: Path,
        state: dict[str, Any] | None = None,
        user_id: int | None = None,
        group_id: int | None = None,
        request_id: str | None = None,
    ):
        from core.context import PluginContext
        return PluginContext(
            config={"bot_name": "测试"},
            secrets={"plugins": {}},
            plugin_name=name,
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            http_session=None,
            send_action=lambda x: None,
            reload_config=lambda: None,
            reload_plugins=lambda: None,
            list_commands=lambda: ["help: 查看帮助"],
            list_plugins=lambda: ["core", "echo"],
            current_user_id=user_id,
            current_group_id=group_id,
            request_id=request_id,
            state=state or {},
        )
    return _factory


@pytest.fixture
def mock_admin_check():
    """Mock admin check"""
    mock = MagicMock()
    mock.is_admin = Mock(side_effect=lambda user_id: user_id in [12345, 67890])
    return mock


@pytest.fixture
def mock_plugin_registry():
    """Mock plugin registry"""
    mock = MagicMock()
    mock.get = Mock(return_value=None)
    return mock


@pytest.fixture
def mock_aiohttp_session():
    """Mock aioresponses for HTTP testing"""
    try:
        from aioresponses import aioresponses
        with aioresponses() as m:
            yield m
    except ImportError:
        # 如果没有安装aioresponses，使用基本mock
        from unittest.mock import AsyncMock, MagicMock

        session = MagicMock()
        session.get = AsyncMock()
        session.post = AsyncMock()
        session.put = AsyncMock()
        session.delete = AsyncMock()
        yield session


@pytest.fixture
def mock_websocket():
    """Mock WebSocket connection"""
    from unittest.mock import AsyncMock

    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    ws.recv = AsyncMock(return_value='{"data": {}}')
    return ws


@pytest.fixture
def temp_db_file(temp_dir: Path) -> Path:
    """创建临时数据库文件"""
    db_path = temp_dir / "test.db"
    return db_path

# ============================================================
# Async Fixtures
# ============================================================

@pytest.fixture
def event_loop_policy():
    """Event loop policy for async tests"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.get_event_loop_policy()


@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for each test case"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

# ============================================================
# Router Fixture
# ============================================================

@pytest.fixture
def empty_router():
    """Create an empty CommandRouter"""
    from core.router import CommandRouter
    return CommandRouter()


@pytest.fixture
def sample_router(empty_router):
    """Create a router with sample commands"""
    from core.router import CommandSpec

    async def dummy_handler(name, args, event, context):
        return [{"type": "text", "data": {"text": f"{name}: {args}"}}]

    specs = [
        CommandSpec(
            plugin="echo",
            name="echo",
            triggers=["echo", "回显"],
            help_text="回显消息",
            admin_only=False,
            handler=dummy_handler,
            priority=0,
        ),
        CommandSpec(
            plugin="help",
            name="help",
            triggers=["help", "帮助"],
            help_text="查看帮助",
            admin_only=False,
            handler=dummy_handler,
            priority=0,
        ),
        CommandSpec(
            plugin="admin",
            name="reload",
            triggers=["reload"],
            help_text="重载配置",
            admin_only=True,
            handler=dummy_handler,
            priority=10,
        ),
    ]

    for spec in specs:
        empty_router.register(spec)

    return empty_router

# ============================================================
# Session Manager Fixture
# ============================================================

@pytest.fixture
def session_manager():
    """Create a SessionManager for testing"""
    from core.session import SessionManager
    return SessionManager(default_timeout=300.0)

# ============================================================
# Plugin Manager Fixture
# ============================================================

@pytest.fixture
def plugin_manager(plugins_dir: Path):
    """Create a PluginManager for testing"""
    from core.plugin_manager import PluginManager
    from core.router import CommandRouter

    router = CommandRouter()

    def dummy_context_factory(*args, **kwargs):
        return None

    manager = PluginManager(plugins_dir, router, dummy_context_factory)
    return manager

# ============================================================
# Scheduler Fixture
# ============================================================

@pytest.fixture
def scheduler_manager():
    """Create a SchedulerManager for testing"""
    from core.scheduler import SchedulerManager
    manager = SchedulerManager()
    yield manager
    # Cleanup
    if manager.scheduler:
        manager.scheduler.shutdown()

# ============================================================
# Utility Functions
# ============================================================

@pytest.fixture
def async_wait():
    """Helper to wait for async operations"""
    async def _wait(seconds: float = 0.1):
        await asyncio.sleep(seconds)
    return _wait


def make_async_mock(return_value=None):
    """Create an async mock function"""
    async def _mock(*args, **kwargs):
        return return_value
    return _mock


# ============================================================
# Skip markers
# ============================================================

def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers dynamically"""
    for item in items:
        # Mark async tests
        if asyncio.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.asyncio)
