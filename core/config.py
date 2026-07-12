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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar, Union
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
T = TypeVar("T")

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
    revision: int = 0


@dataclass
class _ConfigNotification:
    revision: int
    snapshot: ConfigSnapshot
    callbacks: tuple[ConfigCallback, ...]
    completed: threading.Event

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
        self._revision = 0
        self._notification_queue: deque[_ConfigNotification] = deque()
        self._notification_draining = False
        self._notification_thread_id: int | None = None
        self._callback_loop: asyncio.AbstractEventLoop | None = None
        self._last_notified_revision = 0
        self._last_config_mtime: float = 0
        self._last_secrets_mtime: float = 0
        self._lock = threading.RLock()
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

    def _snapshot_locked(self) -> ConfigSnapshot:
        return ConfigSnapshot(
            config=copy.deepcopy(self._config),
            secrets=copy.deepcopy(self._secrets),
            revision=self._revision,
        )

    def _enqueue_notification_locked(self, snapshot: ConfigSnapshot) -> _ConfigNotification:
        notification = _ConfigNotification(
            revision=snapshot.revision,
            snapshot=snapshot,
            callbacks=tuple(self._callbacks),
            completed=threading.Event(),
        )
        self._notification_queue.append(notification)
        return notification

    def reload(self, *, notify: bool = False) -> ConfigSnapshot:
        """Serialize disk read, validation and snapshot publication."""

        with self._lock:
            config = self._load(self.config_path)
            secrets = self._load(self.secrets_path)
            self._replace_snapshot(config, secrets)
            self._revision += 1
            self._update_mtime_locked()
            snapshot = self._snapshot_locked()
            notification = self._enqueue_notification_locked(snapshot) if notify else None
        if notification is not None:
            self._dispatch_notification(notification)
        logger.info("Config reloaded")
        _check_secrets_file_permissions(self.secrets_path)
        return snapshot

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
                self._update_secrets_mtime_locked()
            logger.info("Secrets saved to %s", self.secrets_path)
        except Exception as exc:
            logger.error("Failed to save secrets: %s", exc)
            raise

    def _commit_secrets_mutation(
        self,
        mutate: Callable[[dict[str, Any]], tuple[bool, T]],
    ) -> T:
        """Persist a candidate before publishing it to live readers."""

        with self._lock:
            candidate = copy.deepcopy(self._secrets)
            changed, result = mutate(candidate)
            if not changed:
                return result
            payload = json.dumps(candidate, indent="\t", ensure_ascii=False)
            candidate_view = _freeze_config_mapping(candidate)
            atomic_write_text(self.secrets_path, payload)
            self._secrets = candidate
            self._secrets_view = candidate_view
            self._revision += 1
            self._update_secrets_mtime_locked()
            snapshot = self._snapshot_locked()
            notification = self._enqueue_notification_locked(snapshot)
        logger.info("Secrets transaction committed at revision %d", snapshot.revision)
        self._dispatch_notification(notification)
        return result

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
        keys = self._secret_path_parts(path)

        def mutate(candidate: dict[str, Any]) -> tuple[bool, None]:
            current = candidate
            for i, key in enumerate(keys[:-1]):
                if key not in current:
                    raise KeyError(f"路径不存在: {'.'.join(keys[:i+1])}")
                if not isinstance(current[key], dict):
                    raise ValueError(f"路径 {'.'.join(keys[:i+1])} 不是字典类型")
                current = current[key]

            final_key = keys[-1]
            if final_key not in current:
                raise KeyError(f"键不存在: {path}")

            current[final_key] = copy.deepcopy(value)
            return True, None

        self._commit_secrets_mutation(mutate)

    @staticmethod
    def _secret_path_parts(path: str) -> list[str]:
        parts = path.split(".")
        if not parts or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            raise ValueError("invalid secret path")
        return parts

    def get_plugin_secret(self, plugin_name: str, path: str) -> Any:
        parts = self._secret_path_parts(path)
        self._secret_path_parts(plugin_name)
        with self._lock:
            plugins = self._secrets.get("plugins", {})
            if not isinstance(plugins, dict):
                return None
            current: Any = plugins.get(plugin_name, {})
            for part in parts:
                if not isinstance(current, dict) or part not in current:
                    return None
                current = current[part]
            return copy.deepcopy(current)

    def set_plugin_secret(self, plugin_name: str, path: str, value: Any) -> None:
        parts = self._secret_path_parts(path)
        self._secret_path_parts(plugin_name)

        def mutate(candidate: dict[str, Any]) -> tuple[bool, None]:
            plugins = candidate.setdefault("plugins", {})
            if not isinstance(plugins, dict):
                raise ValueError("secrets.plugins must be a mapping")
            current = plugins.setdefault(plugin_name, {})
            if not isinstance(current, dict):
                raise ValueError(f"plugin secret namespace is not a mapping: {plugin_name}")
            for part in parts[:-1]:
                child = current.get(part)
                if not isinstance(child, dict):
                    child = {}
                    current[part] = child
                current = child
            current[parts[-1]] = copy.deepcopy(value)
            return True, None

        self._commit_secrets_mutation(mutate)

    def delete_plugin_secret(self, plugin_name: str, path: str) -> bool:
        parts = self._secret_path_parts(path)
        self._secret_path_parts(plugin_name)

        def mutate(candidate: dict[str, Any]) -> tuple[bool, bool]:
            plugins = candidate.get("plugins", {})
            if not isinstance(plugins, dict):
                return False, False
            current_candidate: Any = plugins.get(plugin_name, {})
            for part in parts[:-1]:
                if not isinstance(current_candidate, dict) or part not in current_candidate:
                    return False, False
                current_candidate = current_candidate[part]
            if not isinstance(current_candidate, dict) or parts[-1] not in current_candidate:
                return False, False
            del current_candidate[parts[-1]]
            return True, True

        return self._commit_secrets_mutation(mutate)

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

        try:
            callback_loop = asyncio.get_running_loop()
        except RuntimeError:
            callback_loop = None
        with self._lock:
            self._callbacks.append(guarded)
            if callback_loop is not None:
                self._callback_loop = callback_loop

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
            return self._snapshot_locked()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def last_notified_revision(self) -> int:
        with self._lock:
            return self._last_notified_revision

    async def watch(self, interval: float = 2.0) -> None:
        """监控配置文件变化"""
        with self._lock:
            self._callback_loop = asyncio.get_running_loop()
        self._update_mtime()
        while True:
            await asyncio.sleep(interval)
            if await asyncio.to_thread(self._changed):
                try:
                    await asyncio.to_thread(self.reload, notify=True)
                except ConfigLoadError as exc:
                    logger.error("Config reload skipped, keeping last valid snapshot: %s", exc)
                    await asyncio.to_thread(self._update_mtime)
                    continue

    def _dispatch_notification(self, notification: _ConfigNotification) -> None:
        """Start one ordered callback consumer without holding the data lock."""

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        start_sync = False
        schedule_loop: asyncio.AbstractEventLoop | None = None
        wait_for_completion = False
        with self._lock:
            callback_loop = self._callback_loop
            if callback_loop is not None and callback_loop.is_closed():
                self._callback_loop = None
                callback_loop = None
            if running_loop is not None and callback_loop is None:
                callback_loop = running_loop
                self._callback_loop = running_loop

            if not self._notification_draining:
                self._notification_draining = True
                if callback_loop is not None and callback_loop.is_running():
                    schedule_loop = callback_loop
                elif running_loop is not None:
                    schedule_loop = running_loop
                else:
                    self._notification_thread_id = threading.get_ident()
                    start_sync = True
            elif running_loop is None and self._notification_thread_id != threading.get_ident():
                wait_for_completion = True

        if schedule_loop is not None:
            if running_loop is schedule_loop:
                task = schedule_loop.create_task(self._drain_notifications_async())
                task.add_done_callback(self._consume_notification_task_error)
            else:
                asyncio.run_coroutine_threadsafe(
                    self._drain_notifications_async(),
                    schedule_loop,
                )
                wait_for_completion = True
        elif start_sync:
            self._drain_notifications_sync()

        if wait_for_completion:
            notification.completed.wait()

    @staticmethod
    def _consume_notification_task_error(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Config notification consumer failed")

    def _next_notification(self) -> _ConfigNotification | None:
        with self._lock:
            if self._notification_queue:
                return self._notification_queue.popleft()
            self._notification_draining = False
            self._notification_thread_id = None
            return None

    def _complete_notification(self, notification: _ConfigNotification) -> None:
        with self._lock:
            self._last_notified_revision = max(
                self._last_notified_revision,
                notification.revision,
            )
        notification.completed.set()

    def _run_notification_sync(self, notification: _ConfigNotification) -> None:
        for callback in notification.callbacks:
            try:
                result = callback(notification.snapshot)
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
            except asyncio.CancelledError:
                logger.warning("Config callback cancelled at revision %d", notification.revision)
            except Exception as exc:
                logger.exception("Config callback failed: %s", exc)

    def _drain_notifications_sync(self) -> None:
        while True:
            notification = self._next_notification()
            if notification is None:
                return
            try:
                self._run_notification_sync(notification)
            finally:
                self._complete_notification(notification)

    async def _drain_notifications_async(self) -> None:
        with self._lock:
            self._notification_thread_id = threading.get_ident()
            self._callback_loop = asyncio.get_running_loop()
        while True:
            notification = self._next_notification()
            if notification is None:
                return
            try:
                for callback in notification.callbacks:
                    try:
                        result = callback(notification.snapshot)
                        if asyncio.iscoroutine(result):
                            await result
                    except asyncio.CancelledError:
                        logger.warning(
                            "Config callback cancelled at revision %d",
                            notification.revision,
                        )
                    except Exception as exc:
                        logger.exception("Config callback failed: %s", exc)
            finally:
                self._complete_notification(notification)

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
        with self._lock:
            self._update_mtime_locked()

    def _update_mtime_locked(self) -> None:
        if self.config_path.exists():
            self._last_config_mtime = self.config_path.stat().st_mtime
        self._update_secrets_mtime_locked()

    def _update_secrets_mtime_locked(self) -> None:
        if self.secrets_path.exists():
            self._last_secrets_mtime = self.secrets_path.stat().st_mtime

    def _changed(self) -> bool:
        """检查文件是否变化"""
        with self._lock:
            if self.config_path.exists() and self.config_path.stat().st_mtime != self._last_config_mtime:
                return True
            if self.secrets_path.exists() and self.secrets_path.stat().st_mtime != self._last_secrets_mtime:
                return True
            return False

__all__ = ["ConfigManager", "ConfigSnapshot", "ConfigLoadError"]
