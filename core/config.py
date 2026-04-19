"""
配置管理模块

负责加载、监控和热更新配置文件。
"""

import asyncio
import copy
import json
import logging
import os
import platform
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

ConfigCallback = Union[Callable[["ConfigSnapshot"], None], Callable[["ConfigSnapshot"], Any]]

from .plugin_base import atomic_write_text, load_json

logger = logging.getLogger(__name__)


class ConfigLoadError(RuntimeError):
    """Raised when config/secrets cannot be parsed into a valid snapshot."""

    def __init__(self, path: Path, original: Exception):
        super().__init__(f"Failed to load config from {path}: {original}")
        self.path = path
        self.original = original

def _check_secrets_file_permissions(path: Path) -> None:
    """
    检查 secrets 文件权限（Unix-like 系统）

    Args:
        path: secrets.json 文件路径

    Note:
        - Unix: 检查文件是否对所有者可读，但不应对组/其他人可读
        - Windows: 记录警告（Windows 文件权限模型不同）
        - 如果文件不存在则跳过检查
    """
    if not path.exists():
        return

    if platform.system() == "Windows":
        # Windows 使用 ACL，标准 Unix 权限检查不适用
        # 记录提醒，建议用户手动检查
        logger.info(
            "Running on Windows: please ensure %s has appropriate permissions",
            path
        )
        return

    try:
        st = os.stat(path)
        mode = st.st_mode

        # 检查是否对组或其他用户可读
        group_readable = bool(mode & stat.S_IRGRP)
        others_readable = bool(mode & stat.S_IROTH)

        if group_readable or others_readable:
            logger.warning(
                "Security: %s is readable by group or others. "
                "Consider: chmod 600 %s",
                path, path
            )
        else:
            logger.debug("Secrets file permissions OK: %s", path)
    except OSError as exc:
        logger.warning("Could not check file permissions for %s: %s", path, exc)

@dataclass
class ConfigSnapshot:
    """配置快照"""
    config: dict[str, Any]
    secrets: dict[str, Any]

class ConfigManager:
    """配置管理器"""

    def __init__(self, config_path: Path, secrets_path: Path) -> None:
        self.config_path = config_path
        self.secrets_path = secrets_path
        self._config: dict[str, Any] = {}
        self._secrets: dict[str, Any] = {}
        self._callbacks: list[ConfigCallback] = []
        self._last_config_mtime: float = 0
        self._last_secrets_mtime: float = 0
        self._lock = threading.Lock()
        self._initial_load()

    def _initial_load(self) -> None:
        """初始加载配置（不触发回调）"""
        with self._lock:
            try:
                self._config = self._load(self.config_path)
                self._secrets = self._load(self.secrets_path)
            except ConfigLoadError as exc:
                logger.error("%s", exc)
                self._config = {}
                self._secrets = {}
        logger.info("Config loaded")
        _check_secrets_file_permissions(self.secrets_path)

    @property
    def config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._config)

    @property
    def secrets(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._secrets)

    def reload(self) -> None:
        """重新加载配置"""
        config = self._load(self.config_path)
        secrets = self._load(self.secrets_path)
        with self._lock:
            self._config = config
            self._secrets = secrets
        self._update_mtime()
        logger.info("Config reloaded")
        _check_secrets_file_permissions(self.secrets_path)

    def save_secrets(self) -> None:
        """保存 secrets 配置到文件"""
        try:
            with self._lock:
                payload = json.dumps(self._secrets, indent="\t", ensure_ascii=False)
            atomic_write_text(self.secrets_path, payload)
            logger.info("Secrets saved to %s", self.secrets_path)
        except Exception as exc:
            logger.error("Failed to save secrets: %s", exc)
            raise

    def update_secret(self, path: str, value: Any) -> None:
        """
        更新 secrets 中的某个值（仅更新已存在的键）

        Args:
            path: 点分隔的路径，如 "plugins.signin.yingshijufeng.sid"
            value: 新值

        Raises:
            KeyError: 如果路径不存在
            ValueError: 如果路径中的某个键不是字典类型
        """
        keys = path.split(".")
        original_secrets = self.secrets
        with self._lock:
            current = self._secrets

            for i, key in enumerate(keys[:-1]):
                if key not in current:
                    raise KeyError(f"路径不存在: {'.'.join(keys[:i+1])}")
                if not isinstance(current[key], dict):
                    raise ValueError(f"路径 {'.'.join(keys[:i+1])} 不是字典类型")
                current = current[key]

            final_key = keys[-1]
            if final_key not in current:
                raise KeyError(f"键不存在: {path}")

            current[final_key] = value

        try:
            self.save_secrets()
        except Exception:
            with self._lock:
                self._secrets = original_secrets
            raise

        self._update_mtime()
        self._notify_callbacks_sync(self.snapshot())

    def on_reload(self, callback: ConfigCallback) -> None:
        """注册配置重载回调（支持同步和异步回调）"""
        self._callbacks.append(callback)

    def snapshot(self) -> ConfigSnapshot:
        with self._lock:
            return ConfigSnapshot(
                config=copy.deepcopy(self._config),
                secrets=copy.deepcopy(self._secrets),
            )

    async def watch(self, interval: float = 2.0) -> None:
        """监控配置文件变化"""
        self._update_mtime()
        while True:
            await asyncio.sleep(interval)
            if await asyncio.to_thread(self._changed):
                try:
                    await asyncio.to_thread(self.reload)
                except ConfigLoadError as exc:
                    logger.error("Config reload skipped, keeping last valid snapshot: %s", exc)
                    await asyncio.to_thread(self._update_mtime)
                    continue
                snapshot = await asyncio.to_thread(self.snapshot)
                await self._notify_callbacks_async(snapshot)
                await asyncio.to_thread(self._update_mtime)

    def _notify_callbacks_sync(self, snapshot: ConfigSnapshot) -> None:
        for cb in self._callbacks:
            try:
                result = cb(snapshot)
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        asyncio.run(result)
                    else:
                        loop.create_task(result)
            except Exception as exc:
                logger.exception("Config callback failed: %s", exc)

    async def _notify_callbacks_async(self, snapshot: ConfigSnapshot) -> None:
        for cb in self._callbacks:
            try:
                result = cb(snapshot)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.exception("Config callback failed: %s", exc)

    def _load(self, path: Path) -> dict[str, Any]:
        """加载 JSON 文件"""
        try:
            return load_json(path, raise_on_error=True)
        except json.JSONDecodeError as exc:
            raise ConfigLoadError(path, exc) from exc

    def _update_mtime(self) -> None:
        """更新文件修改时间"""
        if self.config_path.exists():
            self._last_config_mtime = self.config_path.stat().st_mtime
        if self.secrets_path.exists():
            self._last_secrets_mtime = self.secrets_path.stat().st_mtime

    def _changed(self) -> bool:
        """检查文件是否变化"""
        if self.config_path.exists() and self.config_path.stat().st_mtime != self._last_config_mtime:
            return True
        if self.secrets_path.exists() and self.secrets_path.stat().st_mtime != self._last_secrets_mtime:
            return True
        return False

__all__ = ["ConfigManager", "ConfigSnapshot", "ConfigLoadError"]
