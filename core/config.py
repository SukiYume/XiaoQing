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
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

ConfigCallback = Union[Callable[["ConfigSnapshot"], None], Callable[["ConfigSnapshot"], Any]]

from .exceptions import ConfigLoadError
from .inbound_policy import validate_inbound_listener
from .plugin_base import atomic_write_text, load_json

logger = logging.getLogger(__name__)


class _RuntimeConfigSchema(BaseModel):
    """Validation for core runtime knobs while allowing plugin-specific keys."""

    model_config = ConfigDict(extra="allow")

    max_concurrency: int | None = Field(default=None, ge=1, le=1024)
    session_timeout: float | None = Field(default=None, gt=0, le=604800)
    plugin_poll_interval: float | None = Field(default=None, gt=0, le=86400)
    ws_queue_size: int | None = Field(default=None, ge=1, le=10000)
    inbound_ws_max_workers: int | None = Field(default=None, ge=1, le=128)
    timezone: str | None = None
    enable_ws_client: bool | None = None
    enable_inbound_server: bool | None = None
    inbound_trusted_tls_proxy: StrictBool | None = None
    onebot_ws_uri: str | None = None
    onebot_http_base: str | None = None
    inbound_ws_uri: str | None = None
    inbound_http_base: str | None = None

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("must be a valid IANA timezone") from exc
        return value

    @field_validator("onebot_ws_uri")
    @classmethod
    def _validate_ws_uri(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"ws", "wss"} or not parts.hostname:
            raise ValueError("must be an absolute ws:// or wss:// URL")
        return value

    @field_validator("onebot_http_base")
    @classmethod
    def _validate_http_uri(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("must be an absolute http:// or https:// URL")
        return value

    @model_validator(mode="after")
    def _validate_enabled_ws_client(self) -> "_RuntimeConfigSchema":
        if self.enable_ws_client is True and not self.onebot_ws_uri:
            raise ValueError("onebot_ws_uri is required when enable_ws_client is true")
        trusted_tls_proxy = self.inbound_trusted_tls_proxy is True
        validate_inbound_listener(
            self.inbound_http_base,
            "http",
            trusted_tls_proxy=trusted_tls_proxy,
        )
        validate_inbound_listener(
            self.inbound_ws_uri,
            "ws",
            trusted_tls_proxy=trusted_tls_proxy,
        )
        return self


def _validate_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return _RuntimeConfigSchema.model_validate(config).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ConfigLoadError(f"Invalid runtime configuration: {exc}") from exc


class _FrozenConfigDict(dict[str, Any]):
    """dict-compatible read-only config snapshot."""

    def _readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("Config snapshots are read-only")

    __setitem__ = _readonly
    __delitem__ = _readonly
    clear = _readonly
    pop = _readonly
    popitem = _readonly
    setdefault = _readonly
    update = _readonly


class _FrozenConfigList(list[Any]):
    """list-compatible read-only config snapshot."""

    def _readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("Config snapshots are read-only")

    __setitem__ = _readonly
    __delitem__ = _readonly
    append = _readonly
    clear = _readonly
    extend = _readonly
    insert = _readonly
    pop = _readonly
    remove = _readonly
    reverse = _readonly
    sort = _readonly


def _freeze_config_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenConfigDict(
            {key: _freeze_config_value(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return _FrozenConfigList([_freeze_config_value(child) for child in value])
    return value


def _freeze_config_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return _freeze_config_value(value)


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
        self._config_view: dict[str, Any] = _freeze_config_mapping({})
        self._secrets_view: dict[str, Any] = _freeze_config_mapping({})
        self._callbacks: list[ConfigCallback] = []
        self._last_config_mtime: float = 0
        self._last_secrets_mtime: float = 0
        self._lock = threading.Lock()
        self._initial_load()

    def _initial_load(self) -> None:
        """初始加载配置（不触发回调）"""
        with self._lock:
            try:
                self._replace_snapshot(
                    self._load(self.config_path),
                    self._load(self.secrets_path),
                )
            except ConfigLoadError as exc:
                logger.error("%s", exc)
                self._replace_snapshot({}, {})
        logger.info("Config loaded")
        _check_secrets_file_permissions(self.secrets_path)

    @property
    def config(self) -> dict[str, Any]:
        with self._lock:
            return self._config_view

    @property
    def secrets(self) -> dict[str, Any]:
        with self._lock:
            return self._secrets_view

    def reload(self) -> None:
        """重新加载配置"""
        config = self._load(self.config_path)
        secrets = self._load(self.secrets_path)
        with self._lock:
            self._replace_snapshot(config, secrets)
        self._update_mtime()
        logger.info("Config reloaded")
        _check_secrets_file_permissions(self.secrets_path)

    def _replace_snapshot(self, config: dict[str, Any], secrets: dict[str, Any]) -> None:
        self._config = config
        self._secrets = secrets
        self._config_view = _freeze_config_mapping(config)
        self._secrets_view = _freeze_config_mapping(secrets)

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
        with self._lock:
            original_secrets = copy.deepcopy(self._secrets)
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
            self._secrets_view = _freeze_config_mapping(self._secrets)

        try:
            self.save_secrets()
        except Exception:
            with self._lock:
                self._secrets = original_secrets
                self._secrets_view = _freeze_config_mapping(self._secrets)
            raise

        self._update_mtime()
        self._notify_callbacks_sync(self.snapshot())

    @staticmethod
    def _secret_path_parts(path: str) -> list[str]:
        parts = path.split(".")
        if not parts or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            raise ValueError("invalid secret path")
        return parts

    def get_plugin_secret(self, plugin_name: str, path: str) -> Any:
        parts = self._secret_path_parts(path)
        with self._lock:
            current: Any = self._secrets.get("plugins", {}).get(plugin_name, {})
            for part in parts:
                if not isinstance(current, dict) or part not in current:
                    return None
                current = current[part]
            return copy.deepcopy(current)

    def set_plugin_secret(self, plugin_name: str, path: str, value: Any) -> None:
        parts = self._secret_path_parts(path)
        with self._lock:
            original_secrets = copy.deepcopy(self._secrets)
            plugins = self._secrets.setdefault("plugins", {})
            current = plugins.setdefault(plugin_name, {})
            for part in parts[:-1]:
                child = current.get(part)
                if not isinstance(child, dict):
                    child = {}
                    current[part] = child
                current = child
            current[parts[-1]] = value
            self._secrets_view = _freeze_config_mapping(self._secrets)
        try:
            self.save_secrets()
        except Exception:
            with self._lock:
                self._secrets = original_secrets
                self._secrets_view = _freeze_config_mapping(self._secrets)
            raise
        self._update_mtime()

    def delete_plugin_secret(self, plugin_name: str, path: str) -> bool:
        parts = self._secret_path_parts(path)
        with self._lock:
            original_secrets = copy.deepcopy(self._secrets)
            current: Any = self._secrets.get("plugins", {}).get(plugin_name, {})
            for part in parts[:-1]:
                if not isinstance(current, dict) or part not in current:
                    return False
                current = current[part]
            if not isinstance(current, dict) or parts[-1] not in current:
                return False
            del current[parts[-1]]
            self._secrets_view = _freeze_config_mapping(self._secrets)
        try:
            self.save_secrets()
        except Exception:
            with self._lock:
                self._secrets = original_secrets
                self._secrets_view = _freeze_config_mapping(self._secrets)
            raise
        self._update_mtime()
        return True

    def on_reload(self, callback: ConfigCallback) -> Callable[[], None]:
        """Register a reload callback and return an idempotent unsubscribe."""

        if not callable(callback):
            raise TypeError("config reload callback must be callable")
        active = True

        def guarded(snapshot: ConfigSnapshot) -> Any:
            with self._lock:
                enabled = active
            if not enabled:
                return None
            return callback(snapshot)

        with self._lock:
            self._callbacks.append(guarded)

        def unsubscribe() -> None:
            nonlocal active
            with self._lock:
                if not active:
                    return
                active = False
                try:
                    self._callbacks.remove(guarded)
                except ValueError:
                    pass

        return unsubscribe

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

    def _notify_callbacks_sync(self, snapshot: ConfigSnapshot) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks)
        for cb in callbacks:
            try:
                result = cb(snapshot)
                if asyncio.iscoroutine(result):
                    self._run_callback_coroutine_sync(result)
            except Exception as exc:
                logger.exception("Config callback failed: %s", exc)

    @staticmethod
    def _run_callback_coroutine_sync(result: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(result)
        else:
            loop.create_task(result)

    async def _notify_callbacks_async(self, snapshot: ConfigSnapshot) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks)
        for cb in callbacks:
            try:
                result = cb(snapshot)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.exception("Config callback failed: %s", exc)

    def _load(self, path: Path) -> dict[str, Any]:
        """加载 JSON 文件"""
        try:
            payload = load_json(path, raise_on_error=True)
        except json.JSONDecodeError as exc:
            raise ConfigLoadError(path, exc) from exc
        if not isinstance(payload, dict):
            raise ConfigLoadError(f"Configuration at {path} must be a JSON object")
        if path == self.config_path and payload:
            return _validate_runtime_config(payload)
        return payload

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
