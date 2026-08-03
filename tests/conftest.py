"""
Pytest configuration and shared fixtures for XiaoQing tests
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid

# Windows 上 torch 必须在 numpy 等依赖之前加载（DLL 搜索顺序问题）
try:
    import torch  # noqa: F401
except ImportError:
    pass
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from tests.helpers.ci_skip_policy import (
    current_platform,
    load_skip_allowances,
    unexpected_skips,
)

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Suppress logging during tests unless explicitly enabled
logging.getLogger().setLevel(logging.WARNING)


def _safe_node_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "tmp"


PROJECT_TMP_ROOT = ROOT / ".pytest_cache"
PROJECT_TMP_BASE = PROJECT_TMP_ROOT / "tmp"
_PROJECT_TMP_RUN_ID = (
    os.environ.get("PYTEST_XDIST_TESTRUNUID") or f"serial-{os.getpid()}-{uuid.uuid4().hex}"
)
_PROJECT_TMP_WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER") or "master"
# xdist 在加载 conftest 前发布 run/worker 身份。tempfile.mkdtemp 也必须直接落到
# 该 worker 根目录，否则它绕过 fixture 后仍会与其他 worker 共用并被提前清理。
PROJECT_TMP_RUN_ROOT = (
    PROJECT_TMP_BASE
    / _safe_node_name(_PROJECT_TMP_RUN_ID)
    / _safe_node_name(_PROJECT_TMP_WORKER_ID)
)


def _remove_project_tmp_run_root(path: Path) -> None:
    """只清理当前 worker 的受控根目录，并尽力移除已经空掉的父目录。"""

    base = PROJECT_TMP_BASE.resolve()
    target = path.resolve()
    if target == base or base not in target.parents:
        raise RuntimeError(f"refusing to remove temp path outside worker root: {target}")
    shutil.rmtree(target, ignore_errors=True)
    for parent in (target.parent, base):
        try:
            parent.rmdir()
        except OSError:
            # 其他 worker 或历史运行仍占用父目录时必须保留。
            pass


def _project_mkdtemp(
    suffix: str | None = None,
    prefix: str | None = None,
    dir: str | os.PathLike[str] | None = None,
) -> str:
    base_dir = Path(dir) if dir else PROJECT_TMP_RUN_ROOT
    base_dir.mkdir(parents=True, exist_ok=True)
    prefix = prefix or "tmp"
    suffix = suffix or ""
    while True:
        candidate = base_dir / f"{prefix}{uuid.uuid4().hex}{suffix}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return str(candidate)
        except FileExistsError:
            continue


# 当前环境里 tempfile.mkdtemp() 创建的目录会立即变成不可访问；测试统一改走项目内目录。
tempfile.mkdtemp = _project_mkdtemp


class ProjectTmpPathFactory:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._counter = 0

    def getbasetemp(self) -> Path:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        return self._base_dir

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        base = self.getbasetemp()
        safe_name = _safe_node_name(basename)
        if numbered:
            while True:
                self._counter += 1
                candidate = base / f"{safe_name}_{self._counter}"
                if not candidate.exists():
                    candidate.mkdir(parents=True, exist_ok=False)
                    return candidate
        candidate = base / safe_name
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate


# ============================================================
# Test Configuration
# ============================================================


def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow tests")
    config.addinivalue_line("markers", "async: Async tests")
    config.addinivalue_line("markers", "asyncio: Asyncio tests")
    config.addinivalue_line("markers", "plugin: Plugin tests")
    config.addinivalue_line("markers", "core: Core module tests")


def pytest_pyfunc_call(pyfuncitem):
    if pyfuncitem.config.pluginmanager.hasplugin("asyncio"):
        return None
    if not asyncio.iscoroutinefunction(pyfuncitem.obj):
        return None

    funcargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(pyfuncitem.obj(**funcargs))
    return True


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


@pytest.fixture(scope="session")
def project_tmp_root() -> Iterator[Path]:
    """返回本次 pytest run 中仅属于当前 xdist worker 的项目内临时根。"""

    PROJECT_TMP_RUN_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        yield PROJECT_TMP_RUN_ROOT
    finally:
        _remove_project_tmp_run_root(PROJECT_TMP_RUN_ROOT)


@pytest.fixture(scope="session")
def tmp_path_factory(project_tmp_root: Path) -> ProjectTmpPathFactory:
    """Project-local replacement for pytest's built-in tmp_path_factory."""
    return ProjectTmpPathFactory(project_tmp_root)


@pytest.fixture
def tmp_path(tmp_path_factory: ProjectTmpPathFactory, request) -> Path:
    """Project-local replacement for pytest's built-in tmp_path fixture."""
    return tmp_path_factory.mktemp(request.node.name)


@pytest.fixture
def temp_dir(tmp_path: Path) -> Iterator[Path]:
    """Create a temporary directory for tests using the project-local tmp_path."""
    yield tmp_path


@pytest.fixture(autouse=True)
def isolate_process_global_import_hooks() -> Iterator[None]:
    """Keep plugin import guards and dependency stubs from leaking between tests."""
    from core import plugin_manager as plugin_manager_module

    original_meta_path = list(sys.meta_path)
    original_owners = dict(plugin_manager_module._PLUGIN_NAMESPACE_OWNERS)
    plugins_package = sys.modules.get("plugins")
    original_plugins_path = (
        list(plugins_package.__path__)
        if plugins_package is not None and hasattr(plugins_package, "__path__")
        else None
    )
    dependency_modules = {name: sys.modules.get(name) for name in ("fastapi", "fastapi.responses")}
    dependency_presence = {name: name in sys.modules for name in dependency_modules}
    try:
        yield
    finally:
        sys.meta_path[:] = original_meta_path
        plugin_manager_module._PLUGIN_NAMESPACE_OWNERS.clear()
        plugin_manager_module._PLUGIN_NAMESPACE_OWNERS.update(original_owners)
        if original_plugins_path is not None and plugins_package is not None:
            plugins_package.__path__[:] = original_plugins_path
        for name, module in dependency_modules.items():
            if dependency_presence[name]:
                sys.modules[name] = module
            else:
                sys.modules.pop(name, None)


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
            get_command_catalog=lambda: (),
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


@pytest.fixture(scope="session")
def event_loop_policy():
    """Provide pytest-asyncio a policy without replacing the global policy."""
    # pytest-asyncio 1.x snapshots the previous loop with get_event_loop().
    # Mark the main thread as having no implicit loop so that snapshotting does
    # not allocate an unowned Proactor loop before the selected policy is active.
    asyncio.set_event_loop(None)
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


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


# ============================================================
# Skip markers
# ============================================================


def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers dynamically"""
    for item in items:
        # Mark async tests
        if asyncio.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.asyncio)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Turn every non-allowlisted CI skip into a failing test session."""

    config = session.config
    if os.environ.get("XIAOQING_CI") != "1" or hasattr(config, "workerinput"):
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        raise RuntimeError("terminal reporter is required for CI skip enforcement")
    reports = reporter.stats.get("skipped", ())
    allowances = load_skip_allowances(ROOT / "tests" / "ci_skip_allowlist.toml")
    violations = unexpected_skips(reports, allowances, platform=current_platform())
    if not violations:
        return
    reporter.write_sep("=", "UNEXPECTED CI SKIPS")
    for violation in violations:
        reporter.write_line(violation, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def reset_xiaoqing_chat_runtime_state(request):
    """Keep xiaoqing_chat singleton runtime state isolated between tests."""
    nodeid = str(getattr(request.node, "nodeid", ""))
    if "xiaoqing_chat" not in nodeid and "reply_checker" not in nodeid:
        yield
        return

    try:
        from plugins.xiaoqing_chat.runtime_state import reset_global_state
    except Exception:
        yield
        return

    reset_global_state()
    try:
        yield
    finally:
        reset_global_state()
