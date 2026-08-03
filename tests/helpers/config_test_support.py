"""配置测试共享 fixture、导入和私有 helper。"""

import asyncio
import json
import logging
import os
import platform
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from core.config import (
    _MAX_CONFIG_SOURCE_BYTES,
    _MAX_CONFIG_TREE_DEPTH,
    _MAX_CONFIG_TREE_NODES,
    ConfigLoadError,
    ConfigManager,
    ConfigSnapshot,
    ConfigSourceStatus,
    _check_secrets_file_permissions,
    _validate_runtime_config,
    materialize_snapshot_value,
)


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """创建临时配置目录"""
    return tmp_path


@pytest.fixture
def config_file(temp_config_dir: Path) -> Path:
    """创建配置文件"""
    config_path = temp_config_dir / "config.json"
    config_data = {
        "bot_name": "测试机器人",
        "command_prefixes": ["/", "!"],
        "require_bot_name_in_group": True,
        "plugins": {
            "echo": {"enabled": True},
            "choice": {"enabled": False},
        },
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
    return config_path


@pytest.fixture
def secrets_file(temp_config_dir: Path) -> Path:
    """创建密钥文件"""
    secrets_path = temp_config_dir / "secrets.json"
    secrets_data = {
        "admin_user_ids": [12345, 67890],
        "plugins": {
            "echo": {"api_key": "test_key"},
            "choice": {},
        },
    }
    with open(secrets_path, "w", encoding="utf-8") as f:
        json.dump(secrets_data, f, indent=2, ensure_ascii=False)
    return secrets_path


@pytest.fixture
def config_manager(config_file: Path, secrets_file: Path) -> ConfigManager:
    """创建 ConfigManager 实例"""
    return ConfigManager(config_file, secrets_file)


__all__ = (
    "Any",
    "ConfigLoadError",
    "ConfigManager",
    "ConfigSnapshot",
    "ConfigSourceStatus",
    "Mapping",
    "Path",
    "_MAX_CONFIG_SOURCE_BYTES",
    "_MAX_CONFIG_TREE_DEPTH",
    "_MAX_CONFIG_TREE_NODES",
    "_check_secrets_file_permissions",
    "_validate_runtime_config",
    "asyncio",
    "config_file",
    "config_manager",
    "json",
    "logging",
    "materialize_snapshot_value",
    "os",
    "platform",
    "pytest",
    "secrets_file",
    "temp_config_dir",
    "threading",
    "time",
)
