"""配置测试共享 fixture、导入和私有 helper。"""

import json
import platform
from collections.abc import Mapping
from pathlib import Path

import pytest

from core.config import (
    ConfigLoadError,
    ConfigManager,
    ConfigSnapshot,
    ConfigSourceStatus,
    PendingSecretsChange,
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
        "bot_name"                 : "测试机器人",
        "command_prefixes"         : ["/", "!"],
        "require_bot_name_in_group": True,
        "plugins"                  : {
            "echo"  : {"enabled": True},
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
        "plugins"       : {
            "echo"  : {"api_key": "test_key"},
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


def last_notified_revision(manager: ConfigManager) -> int:
    """加锁读取通知进度，避免为测试等待逻辑扩张生产接口。"""

    with manager._lock:
        return manager._last_notified_revision


__all__ = (
    "ConfigLoadError",
    "ConfigManager",
    "ConfigSnapshot",
    "ConfigSourceStatus",
    "PendingSecretsChange",
    "Mapping",
    "Path",
    "last_notified_revision",
    "config_file",
    "config_manager",
    "json",
    "materialize_snapshot_value",
    "platform",
    "pytest",
    "secrets_file",
    "temp_config_dir",
)
