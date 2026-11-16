"""应用测试共享 fixture、导入和私有 helper。"""

import json
import shutil
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import ClientSession

from core.app import (
    ApplicationLifecycleFatalError,
    InboundReconcileError,
    XiaoQingApp,
)
from core.app_support import current_action_sink
from core.delivery import DeliveryReceipt, DeliverySegments
from core.interfaces import DeliveryTarget, PluginPrincipal
from core.onebot import OneBotActionOutcomeUnknown
from core.plugin_manager_support import LoadedPlugin
from core.server import BroadcastResult, InboundManager
from tests.helpers.asyncio_tools import (
    BlockingConcurrencyProbe,
    cancellation_resistant_callback,
    cancellation_then_release_callback,
    resist_cancellation_until_released,
)
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.plugin_manager_test_support import (
    _isolate_process_global_plugin_import_state,
    detach_repository_plugin_modules,
    load_plugin_dir,
)
from tests.helpers.plugin_probe import PROBE


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
        "bot_name"             : "小青",
        "command_prefixes"     : ["/"],
        "onebot_http_base"     : "",
        "enable_ws_client"     : False,
        "enable_inbound_server": False,
        "max_concurrency"      : 5,
        "enable_plugin_watcher": False,
        "session_timeout"      : 300,
        "timezone"             : "Asia/Shanghai",
        "default_group_ids"    : [],
        "admin_user_ids"       : [],
        "plugins"              : {},
    }
    with open(config_file, "w") as f:
        json.dump(config_data, f, indent=2)

    # Create secrets.json
    secrets_file = config_dir / "secrets.json"
    secrets_data = {
        "admin_user_ids": [12345, 67890],
        "onebot_token"  : "",
        "inbound_token" : "",
    }
    with open(secrets_file, "w") as f:
        json.dump(secrets_data, f, indent=2)

    # Create plugins directory
    plugins_dir = temp_dir / "plugins"
    plugins_dir.mkdir()

    # Create logs directory
    logs_dir = temp_dir / "logs"
    logs_dir.mkdir()

    # 测试插件的记录器是进程内对象：每条用例从空的开始
    PROBE.reset()

    # Patch setup_logging to avoid file locks
    with patch("core.app.setup_logging") as mock_setup:
        mock_setup.return_value = MagicMock()
        yield temp_dir


def set_app_config(app: XiaoQingApp, **updates: Any) -> None:
    config = dict(app.config_manager.config)
    config.update(updates)
    app.config_manager._replace_snapshot(config, app.config_manager._secrets)


def plugin_context_for(
    app: XiaoQingApp,
    plugin_name: str,
    *,
    user_id: int | None                   = None,
    group_id: int | None                  = None,
    principal: PluginPrincipal | None     = None,
    manifest_capabilities: frozenset[str] = frozenset(),
    uses_services: frozenset[str]         = frozenset(),
):
    plugin_dir = app.root / "plugins" / plugin_name
    data_dir   = app.root / "data" / plugin_name
    data_dir.mkdir(parents=True, exist_ok=True)
    return app._build_plugin_context(
        plugin_name,
        plugin_dir,
        data_dir,
        {},
        user_id,
        group_id,
        "capability-test",
        principal,
        manifest_capabilities,
        uses_services,
    )


def command_declaration(
    trigger: str,
    *,
    contexts: tuple[str, ...] = ("private", "group"),
    permission: str           = "public",
) -> dict[str, Any]:
    """``plugin.json`` 里的一条命令声明；命令名、触发词和帮助都取最简值。"""

    return {
        "name"      : trigger,
        "triggers"  : [trigger],
        "help"      : "测试命令",
        "permission": permission,
        "contexts"  : list(contexts),
    }


def service_declaration(
    name: str,
    callback: str,
    callers: tuple[str, ...],
    *,
    required_capability: str | None = None,
) -> dict[str, Any]:
    """``plugin.json`` 里导出的一项插件服务；``callers`` 必须是 Core 为该服务固定的调用方。"""

    declaration: dict[str, Any] = {"name": name, "callback": callback, "callers": list(callers)}
    if required_capability is not None:
        declaration["required_capability"] = required_capability
    return declaration


async def load_test_plugin(
    app: XiaoQingApp,
    name: str,
    source: str,
    **manifest: Any,
) -> LoadedPlugin:
    """在 ``app.plugins_dir`` 下写一个真实的插件目录，经插件管理器的真实加载路径装入。

    ``source`` 是 ``main.py`` 的内容；``manifest`` 里的字段（``commands``、``services``、
    ``schedule``、``capabilities``、``uses_services``、``concurrency``、``enabled`` 等）写进
    ``plugin.json``。同名插件再调一次就是改文件后的热重载。源码里要把观察结果交回测试时，
    ``from tests.helpers.plugin_probe import PROBE``。
    """

    plugin_dir = app.plugins_dir / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "entry": "main.py", **manifest}),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(textwrap.dedent(source), encoding="utf-8")
    detach_repository_plugin_modules(app.plugin_manager, name)
    await load_plugin_dir(app.plugin_manager, plugin_dir)
    loaded = app.plugin_manager.get(name)
    assert loaded is not None, f"测试插件 {name} 没有加载成功（加载失败只写日志）"
    return loaded


async def load_shipped_plugin(app: XiaoQingApp, name: str) -> LoadedPlugin:
    """把仓库里随发行版提供的插件整个复制进 ``app.plugins_dir``，经同一条真实路径加载。"""

    plugin_dir = app.plugins_dir / name
    shutil.copytree(
        REPOSITORY_ROOT / "plugins" / name,
        plugin_dir,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    detach_repository_plugin_modules(app.plugin_manager, name)
    await load_plugin_dir(app.plugin_manager, plugin_dir)
    loaded = app.plugin_manager.get(name)
    assert loaded is not None, f"插件 {name} 没有加载成功（加载失败只写日志）"
    return loaded


UNUSED_COMMAND_SOURCE = """
async def handle(_command, _args, _event, _context):
    raise AssertionError("published test command is not executed")
"""

_ECHO_COMMAND_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle(_command, _args, event, _context):
    PROBE.log("echo").append(event)
    return [{"type": "text", "data": {"text": "test"}}]
"""


async def load_text_command_plugin(app: XiaoQingApp) -> list[dict[str, Any]]:
    """装入提供命令 ``/echo`` 的插件，它回一段文字；返回的列表按顺序收录命令收到的事件。"""

    await load_test_plugin(
        app, "echo", _ECHO_COMMAND_SOURCE, commands=[command_declaration("echo")]
    )
    return PROBE.log("echo")


def user_principal(
    app: XiaoQingApp,
    user_id: int,
    group_id: int | None = None,
    role: str            = "",
):
    """按真实入口给某个发送者签发身份：不给 ``group_id`` 是私聊，``role`` 是群内角色。"""

    sender = {"user_id": user_id, **({"role": role} if role else {})}
    return app.issue_user_principal(
        {"sender": sender},
        user_id    = user_id,
        group_id   = group_id,
        is_private = group_id is None,
    )


__all__ = (
    "Any",
    "ApplicationLifecycleFatalError",
    "BlockingConcurrencyProbe",
    "BroadcastResult",
    "ClientSession",
    "DeliveryReceipt",
    "DeliverySegments",
    "DeliveryTarget",
    "InboundManager",
    "InboundReconcileError",
    "MagicMock",
    "Mapping",
    "OneBotActionOutcomeUnknown",
    "Path",
    "PluginPrincipal",
    "XiaoQingApp",
    "command_declaration",
    "load_shipped_plugin",
    "load_test_plugin",
    "load_text_command_plugin",
    "plugin_context_for",
    "service_declaration",
    "set_app_config",
    "cancellation_resistant_callback",
    "cancellation_then_release_callback",
    "current_action_sink",
    "json",
    "patch",
    "pytest",
    "resist_cancellation_until_released",
    "temp_app_root",
    "user_principal",
    "PROBE",
    "_isolate_process_global_plugin_import_state",
    "UNUSED_COMMAND_SOURCE",
)
