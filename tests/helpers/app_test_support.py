"""应用测试共享 fixture、导入和私有 helper。"""

import asyncio
import copy
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientSession

from core.app import (
    ApplicationLifecycleFatalError,
    InboundReconcileError,
    XiaoQingApp,
    _onebot_credentials,
    current_action_sink,
)
from core.delivery import DeliveryReceipt, DeliverySegments
from core.interfaces import DeliveryTarget, PluginPrincipal
from core.onebot import OneBotActionOutcomeUnknown
from core.plugin_manager import LoadedPlugin, PluginDefinition, PluginServiceDefinition
from core.server import BroadcastResult, InboundManager
from tests.helpers.asyncio_tools import (
    BlockingConcurrencyProbe,
    cancellation_resistant_callback,
    cancellation_then_release_callback,
    resist_cancellation_until_released,
)


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


def _plugin_context_for(
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


def _register_test_loaded_plugin(
    app: XiaoQingApp,
    definition: PluginDefinition,
    module: ModuleType,
) -> None:
    """Create the discovery invariant before bypassing discovery in a unit test."""
    (app.plugins_dir / definition.name).mkdir(parents=True, exist_ok=True)
    app.plugin_manager._register_loaded_plugin(definition, module, 0.0)


__all__ = (
    "Any",
    "ApplicationLifecycleFatalError",
    "AsyncMock",
    "BlockingConcurrencyProbe",
    "BroadcastResult",
    "ClientSession",
    "DeliveryReceipt",
    "DeliverySegments",
    "DeliveryTarget",
    "InboundManager",
    "InboundReconcileError",
    "LoadedPlugin",
    "MagicMock",
    "Mapping",
    "Mock",
    "ModuleType",
    "OneBotActionOutcomeUnknown",
    "Path",
    "PluginDefinition",
    "PluginPrincipal",
    "PluginServiceDefinition",
    "SimpleNamespace",
    "XiaoQingApp",
    "_onebot_credentials",
    "_plugin_context_for",
    "_register_test_loaded_plugin",
    "_set_app_config",
    "asyncio",
    "cancellation_resistant_callback",
    "cancellation_then_release_callback",
    "copy",
    "current_action_sink",
    "json",
    "mock_dependencies",
    "os",
    "patch",
    "pytest",
    "resist_cancellation_until_released",
    "sys",
    "temp_app_root",
    "time",
)
