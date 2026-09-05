"""
配置管理模块

负责加载、监控和热更新配置文件。
"""

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import math
import os
import platform
import re
import stat
import threading
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, TypeVar, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .atomic_store import MISSING_ETAG, keyed_path_lock
from .constants import VALID_PLUGIN_NAME_PATTERN
from .exceptions import ConfigLoadError
from .inbound_policy import validate_inbound_listener

ConfigCallback         = Callable[["ConfigSnapshot"], None] | Callable[["ConfigSnapshot"], Any]
SecurityConfigCallback = Callable[["ConfigSnapshot"], None]
T                      = TypeVar("T")
_SourceStatKey         = tuple[int, int, int, int]

_MAX_CONFIG_SOURCE_BYTES         = 8 * 1024 * 1024
_MAX_CONFIG_TREE_DEPTH           = 64
_MAX_CONFIG_TREE_NODES           = 100_000
_MAX_WATCH_STABILITY_RETRIES     = 3
_REQUIRED_STABLE_SOURCE_READS    = 3
_MAX_STABLE_SOURCE_READS         = 6
_CONFIG_CALLBACK_TIMEOUT_SECONDS = 5.0
_MAX_PENDING_SECRET_CHANGE_PATHS = 30

logger = logging.getLogger(__name__)


class _PluginExecutionPolicySchema(BaseModel):
    """Strict, partial policy shared by global and per-plugin execution limits."""

    model_config = ConfigDict(extra="forbid", strict=True)

    timeout_seconds: float | None = Field(default=None, ge=0, le=86400)
    parallel_limit: int | None = Field(default=None, ge=1, le=1024)
    admission_queue_limit: int | None = Field(default=None, ge=0, le=10000)
    sync_parallel_limit: int | None = Field(default=None, ge=1, le=3)
    sync_queue_limit: int | None = Field(default=None, ge=0, le=10000)
    failure_threshold: int | None = Field(default=None, ge=1, le=10000)
    cooldown_seconds: float | None = Field(default=None, ge=0.1, le=86400)
    drain_timeout_seconds: float | None = Field(default=None, ge=0.1, le=3600)

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout_seconds(cls, value: float | None) -> float:
        if value is None:
            raise ValueError("must not be null; omit the field to inherit its value")
        if value == 0 or value >= 0.1:
            return value
        raise ValueError("must be 0 (disabled) or between 0.1 and 86400 seconds")

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> "_PluginExecutionPolicySchema":
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(
                    f"{field_name} must not be null; omit the field to inherit its value"
                )
        return self


class _PluginExecutionConfigSchema(_PluginExecutionPolicySchema):
    """Top-level execution policy, including process-wide sync admission."""

    global_sync_queue_limit: int | None = Field(default=None, ge=1, le=100000)
    overrides: dict[str, _PluginExecutionPolicySchema] | None = None

    @field_validator("overrides")
    @classmethod
    def _validate_override_names(
        cls,
        value: dict[str, _PluginExecutionPolicySchema] | None,
    ) -> dict[str, _PluginExecutionPolicySchema]:
        if value is None:
            raise ValueError("must not be null; omit the field when no overrides are needed")
        invalid_names = [
            name for name in value if re.fullmatch(VALID_PLUGIN_NAME_PATTERN, name) is None
        ]
        if invalid_names:
            raise ValueError(
                "override keys must be non-empty plugin names containing only "
                "ASCII letters, digits, and underscores"
            )
        return value


class _RuntimeConfigSchema(BaseModel):
    """Validation for core runtime knobs while allowing plugin-specific keys."""

    model_config = ConfigDict(extra="allow")

    max_concurrency: int | None = Field(default=None, ge=1, le=1024)
    session_timeout: float | None = Field(default=None, gt=0, le=604800)
    plugin_poll_interval: float | None = Field(default=None, gt=0, le=86400)
    data_root: StrictStr | None = None
    ws_queue_size: int | None = Field(default=None, ge=1, le=10000)
    inbound_ws_max_workers: int | None = Field(default=None, ge=1, le=128)
    inbound_ws_broadcast_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    timezone: str | None                                  = None
    enable_ws_client: bool | None                         = None
    enable_inbound_server: bool | None                    = None
    inbound_trusted_tls_proxy: StrictBool | None          = None
    onebot_ws_uri: str | None                             = None
    onebot_http_base: str | None                          = None
    inbound_ws_uri: str | None                            = None
    inbound_http_base: str | None                         = None
    plugin_execution: _PluginExecutionConfigSchema | None = None

    @field_validator("data_root")
    @classmethod
    def _validate_data_root(cls, value: str | None) -> str:
        if value is None or not value.strip() or "\x00" in value:
            raise ValueError("must be a non-empty filesystem path")
        return value.strip()

    @field_validator("plugin_execution")
    @classmethod
    def _validate_plugin_execution(
        cls,
        value: _PluginExecutionConfigSchema | None,
    ) -> _PluginExecutionConfigSchema:
        if value is None:
            raise ValueError("must be an object, not null")
        return value

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
        # Pydantic 的插件当前把 model_dump 标成 Any；运行时模型已保证键为字符串。
        return cast(
            dict[str, Any],
            _RuntimeConfigSchema.model_validate(config).model_dump(exclude_none=True),
        )
    except ValidationError as exc:
        raise ConfigLoadError(f"Invalid runtime configuration: {exc}") from exc


def _validate_config_tree(value: Any) -> None:
    """Bound and validate a JSON-like tree before recursive freezing."""

    nodes                        = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_CONFIG_TREE_NODES:
            raise ValueError(f"Config snapshot exceeds {_MAX_CONFIG_TREE_NODES} values")
        if depth > _MAX_CONFIG_TREE_DEPTH:
            raise ValueError(f"Config snapshot exceeds maximum depth {_MAX_CONFIG_TREE_DEPTH}")
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise TypeError("Config snapshot mapping keys must be strings")
                stack.append((child, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
            continue
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("Config snapshot numbers must be finite")
            continue
        raise TypeError(f"Unsupported mutable config snapshot value: {type(current).__name__}")


def _freeze_config_value(value: Any) -> Any:
    """Copy JSON-like data into containers with no mutable builtin backdoor."""

    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("Config snapshot mapping keys must be strings")
            frozen[key] = _freeze_config_value(child)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config_value(child) for child in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Config snapshot numbers must be finite")
        return value
    raise TypeError(f"Unsupported mutable config snapshot value: {type(value).__name__}")


def _freeze_config_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _validate_config_tree(value)
    return cast(Mapping[str, Any], _freeze_config_value(value))


def materialize_snapshot_value(value: Any) -> Any:
    """Return a detached mutable JSON-compatible copy of a frozen value."""

    if isinstance(value, Mapping):
        return {key: materialize_snapshot_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [materialize_snapshot_value(child) for child in value]
    return copy.deepcopy(value)


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
        logger.info("Running on Windows: please ensure %s has appropriate permissions", path)
        return

    try:
        st   = os.stat(path)
        mode = st.st_mode

        # 检查是否对组或其他用户可读
        group_readable  = bool(mode & stat.S_IRGRP)
        others_readable = bool(mode & stat.S_IROTH)

        if group_readable or others_readable:
            logger.warning(
                "Security: %s is readable by group or others. Consider: chmod 600 %s", path, path
            )
        else:
            logger.debug("Secrets file permissions OK: %s", path)
    except OSError as exc:
        logger.warning("Could not check file permissions for %s: %s", path, exc)


class ConfigSourceStatus(str, Enum):
    """Observed state of one on-disk configuration source."""

    VALID        = "valid"
    MISSING      = "missing"
    INVALID      = "invalid"
    UNAVAILABLE  = "unavailable"
    INCONSISTENT = "inconsistent"


class ConfigSnapshot(tuple[Any, ...]):
    """Deeply immutable configuration and source-health snapshot.

    A tuple-backed carrier prevents even ``object.__setattr__`` from replacing
    fields while callbacks share the snapshot.
    """

    __slots__ = ()

    def __new__(
        cls,
        config: Mapping[str, Any],
        secrets: Mapping[str, Any],
        revision: int                      = 0,
        config_status: ConfigSourceStatus  = ConfigSourceStatus.VALID,
        secrets_status: ConfigSourceStatus = ConfigSourceStatus.VALID,
    ) -> "ConfigSnapshot":
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise TypeError("Config snapshot revision must be an integer")
        if revision < 0:
            raise ValueError("Config snapshot revision cannot be negative")
        return tuple.__new__(
            cls,
            (
                _freeze_config_mapping(config),
                _freeze_config_mapping(secrets),
                revision,
                ConfigSourceStatus(config_status),
                ConfigSourceStatus(secrets_status),
            ),
        )

    @property
    def config(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self[0])

    @property
    def secrets(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self[1])

    @property
    def revision(self) -> int:
        return cast(int, self[2])

    @property
    def config_status(self) -> ConfigSourceStatus:
        return cast(ConfigSourceStatus, self[3])

    @property
    def secrets_status(self) -> ConfigSourceStatus:
        return cast(ConfigSourceStatus, self[4])

    def mutable_config(self) -> dict[str, Any]:
        return cast(dict[str, Any], materialize_snapshot_value(self.config))

    def mutable_secrets(self) -> dict[str, Any]:
        return cast(dict[str, Any], materialize_snapshot_value(self.secrets))


@dataclass(frozen=True, slots=True)
class PendingSecretsChange:
    """不携带凭据值的待确认 ``secrets.json`` 变更摘要。"""

    added_paths: tuple[str, ...]
    removed_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    added_count: int
    removed_count: int
    changed_count: int

    @property
    def omitted_count(self) -> int:
        shown = len(self.added_paths) + len(self.removed_paths) + len(self.changed_paths)
        return self.added_count + self.removed_count + self.changed_count - shown


PendingSecretsCallback = Callable[[PendingSecretsChange], Any]


def _secret_change_path(parent: str, key: str) -> str:
    """使用易读且无歧义的形式展示 secret 字段路径。"""

    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return f"{parent}.{key}" if parent else key
    encoded = json.dumps(key, ensure_ascii=False)
    return f"{parent}[{encoded}]" if parent else f"[{encoded}]"


def _summarize_secret_changes(
    active: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> PendingSecretsChange:
    """比较字段结构和值，只保留路径和计数，避免通知泄露凭据。"""

    samples: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}
    counts                        = {"added": 0, "removed": 0, "changed": 0}

    def record(kind: str, path: str) -> None:
        counts[kind] += 1
        shown = sum(len(paths) for paths in samples.values())
        if shown < _MAX_PENDING_SECRET_CHANGE_PATHS:
            samples[kind].append(path or "<root>")

    def walk(current: Any, replacement: Any, parent: str = "") -> None:
        if current == replacement:
            return
        if isinstance(current, Mapping) and isinstance(replacement, Mapping):
            for key in sorted(set(current) | set(replacement)):
                path = _secret_change_path(parent, key)
                if key not in current:
                    record("added", path)
                elif key not in replacement:
                    record("removed", path)
                else:
                    walk(current[key], replacement[key], path)
            return
        record("changed", parent)

    walk(active, candidate)
    return PendingSecretsChange(
        added_paths   = tuple(samples["added"]),
        removed_paths = tuple(samples["removed"]),
        changed_paths = tuple(samples["changed"]),
        added_count   = counts["added"],
        removed_count = counts["removed"],
        changed_count = counts["changed"],
    )


@dataclass(frozen=True, slots=True)
class _SourceRead:
    status: ConfigSourceStatus
    etag: str
    value: dict[str, Any] | None    = None
    error: ConfigLoadError | None   = None
    identity: str                   = ""
    stat_key: _SourceStatKey | None = None

    @property
    def signature(self) -> tuple[ConfigSourceStatus, str, str]:
        return self.status, self.etag, self.identity


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


def _payload_etag(payload: bytes | None) -> str:
    return hashlib.sha256(payload).hexdigest() if payload is not None else MISSING_ETAG


def _source_identity(source_stat: os.stat_result) -> str:
    """Track atomic replacement generations even when bytes are identical."""

    return ":".join(
        str(value)
        for value in (
            source_stat.st_dev,
            source_stat.st_ino,
        )
    )


def _source_stat_key(source_stat: os.stat_result) -> _SourceStatKey:
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )


@dataclass
class _ConfigNotification:
    revision: int
    snapshot: ConfigSnapshot
    callbacks: tuple[ConfigCallback, ...]


def _write_secret_payload(handle: BinaryIO, payload: bytes) -> None:
    """Overwrite one already verified inode and durably flush it."""

    handle.seek(0)
    remaining = memoryview(payload)
    while remaining:
        written = handle.write(remaining)
        if not written:
            raise OSError("short write while persisting secrets")
        remaining = remaining[written:]
    handle.truncate(len(payload))
    handle.flush()
    os.fsync(handle.fileno())


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_path: Path, secrets_path: Path) -> None:
        self.config_path = Path(config_path)
        self.secrets_path = Path(secrets_path)
        self._config: dict[str, Any] = {}
        self._secrets: dict[str, Any] = {}
        self._config_view: Mapping[str, Any] = _freeze_config_mapping({})
        self._secrets_view: Mapping[str, Any] = _freeze_config_mapping({})
        self._config_source = _SourceRead(ConfigSourceStatus.MISSING, MISSING_ETAG)
        self._secrets_source = _SourceRead(ConfigSourceStatus.MISSING, MISSING_ETAG)
        self._paired_config_signature = self._config_source.signature
        self._paired_secrets_signature = self._secrets_source.signature
        self._pending_secrets_source: _SourceRead | None = None
        self._pending_notice_signature: tuple[ConfigSourceStatus, str, str] | None = None
        self._source_generation = 0
        self._callbacks: list[ConfigCallback] = []
        self._security_callbacks: list[SecurityConfigCallback] = []
        self._pending_secrets_callbacks: list[PendingSecretsCallback] = []
        self._revision = 0
        # Claim the first revision before scheduling its consumer so a second
        # synchronous reload cannot overwrite it before the event loop runs.
        # Among later not-yet-started revisions, subscribers only need the
        # latest immutable snapshot.
        self._notification_current: _ConfigNotification | None = None
        self._notification_queue: deque[_ConfigNotification] = deque(maxlen=1)
        self._notification_draining                           = False
        self._callback_loop: asyncio.AbstractEventLoop | None = None
        self._callback_timeout_seconds                        = _CONFIG_CALLBACK_TIMEOUT_SECONDS
        self._last_notified_revision                          = 0
        self._parsed_source_cache: dict[Path, _SourceRead]    = {}
        self._parsed_source_cache_lock                        = threading.Lock()
        self._lock                                            = threading.RLock()
        self._initial_load()

    def _initial_load(self) -> None:
        """Load each source independently without exposing stale credentials."""

        with self._lock:
            config_read, secrets_read = self._read_stable_sources()
            published_secrets, paired = self._prepare_confirmed_secrets_locked(
                config_read,
                secrets_read,
                confirm_config=True,
            )
            self._apply_source_reads_locked(
                config_read,
                published_secrets,
                force         = True,
                bump_revision = False,
                notify        = False,
            )
            if paired:
                self._mark_sources_paired_locked(config_read, secrets_read)
            else:
                self._paired_config_signature = config_read.signature
        self._log_source_problem("config", config_read)
        self._log_source_problem("secrets", published_secrets)
        logger.info("Config loaded")
        _check_secrets_file_permissions(self.secrets_path)

    @property
    def config(self) -> Mapping[str, Any]:
        with self._lock:
            return self._config_view

    @property
    def secrets(self) -> Mapping[str, Any]:
        with self._lock:
            return self._secrets_view

    def _snapshot_locked(self) -> ConfigSnapshot:
        return ConfigSnapshot(
            config         = self._config_view,
            secrets        = self._secrets_view,
            revision       = self._revision,
            config_status  = self._config_source.status,
            secrets_status = self._secrets_source.status,
        )

    def _enqueue_notification_locked(self, snapshot: ConfigSnapshot) -> _ConfigNotification:
        notification = _ConfigNotification(
            revision  = snapshot.revision,
            snapshot  = snapshot,
            callbacks = tuple(self._callbacks),
        )
        if self._notification_current is None and not self._notification_draining:
            self._notification_current = notification
        else:
            self._notification_queue.append(notification)
        return notification

    def reload(self, *, notify: bool = False) -> ConfigSnapshot:
        """Strictly reload both sources and atomically publish their health state."""

        with self._lock:
            config_read, secrets_read = self._read_stable_sources()
            published_secrets, paired = self._prepare_confirmed_secrets_locked(
                config_read,
                secrets_read,
                confirm_config=True,
            )
            self._clear_pending_sources_locked(bump_generation=True)
            snapshot, notification, _changed = self._apply_source_reads_locked(
                config_read,
                published_secrets,
                force         = True,
                bump_revision = True,
                notify        = notify,
            )
            if paired:
                self._mark_sources_paired_locked(config_read, secrets_read)
            else:
                self._paired_config_signature = config_read.signature
        if notification is not None:
            self._start_notification_consumer()
        self._log_source_problem("config", config_read)
        self._log_source_problem("secrets", published_secrets)
        logger.info("Config reloaded")
        _check_secrets_file_permissions(self.secrets_path)
        if config_read.status in {
            ConfigSourceStatus.INVALID,
            ConfigSourceStatus.UNAVAILABLE,
        }:
            assert config_read.error is not None
            raise config_read.error
        return snapshot

    def _mark_sources_paired_locked(
        self,
        config_read: _SourceRead,
        secrets_read: _SourceRead,
    ) -> None:
        self._paired_config_signature  = config_read.signature
        self._paired_secrets_signature = secrets_read.signature

    def _pending_read_hint_locked(self) -> tuple[_SourceRead, _SourceRead]:
        """返回最新磁盘候选，减少等待确认期间的重复解析。"""

        secrets_read = self._pending_secrets_source or self._secrets_source
        return self._config_source, secrets_read

    def _clear_pending_sources_locked(self, *, bump_generation: bool = False) -> None:
        """清除待确认候选，并按需使并发中的旧 watcher 读失效。"""

        had_pending_source             = self._pending_secrets_source is not None
        self._pending_secrets_source   = None
        self._pending_notice_signature = None
        if had_pending_source and bump_generation:
            self._source_generation += 1

    def _pending_secrets_notice_locked(self) -> PendingSecretsChange | None:
        """为当前尚未通知的合法候选生成不含值的字段摘要。"""

        secrets_read = self._pending_secrets_source
        if (
            secrets_read is None
            or secrets_read.status is not ConfigSourceStatus.VALID
            or secrets_read.value is None
        ):
            return None
        signature = secrets_read.signature
        if signature == self._pending_notice_signature:
            return None
        self._pending_notice_signature = signature
        return _summarize_secret_changes(self._secrets, secrets_read.value)

    def _stage_unconfirmed_secret_change_locked(
        self,
        config_read: _SourceRead,
        secrets_read: _SourceRead,
    ) -> tuple[bool, bool]:
        """暂存合法的 secrets-only 外部改动，同时保留当前可信代。

        仅当磁盘配置仍与当前已确认配置完全一致时保留旧凭据。这样 QQ
        WebSocket 可以继续接收 ``/reload``，而候选 secrets 在显式确认前
        不会进入插件、管理员或网络认证视图。配置来源也发生变化时继续走
        原有 fail-closed 路径，避免把旧 token 绑定到新的网络端点。
        """

        active_generation_is_trusted = (
            self._config_source.status is ConfigSourceStatus.VALID
            and self._secrets_source.status is ConfigSourceStatus.VALID
            and self._config_source.signature == self._paired_config_signature
            and self._secrets_source.signature == self._paired_secrets_signature
        )
        is_secrets_only_candidate = (
            config_read.status is ConfigSourceStatus.VALID
            and secrets_read.status is ConfigSourceStatus.VALID
            and config_read.signature == self._config_source.signature
            and secrets_read.signature != self._secrets_source.signature
        )
        if not active_generation_is_trusted or not is_secrets_only_candidate:
            return False, False

        changed = (
            self._pending_secrets_source is None
            or secrets_read.signature != self._pending_secrets_source.signature
        )
        self._pending_secrets_source = secrets_read
        if changed:
            # 使已经离开锁、仍在读取旧候选的 watcher 失效；当前可信快照和
            # revision 保持不变，不触发任何授权回调或网络轮换。
            self._source_generation += 1
        return True, changed

    def _guard_unconfirmed_secrets_locked(
        self,
        config_read: _SourceRead,
        secrets_read: _SourceRead,
    ) -> _SourceRead:
        """Keep externally introduced credentials revoked until explicit confirmation."""

        if secrets_read.status is not ConfigSourceStatus.VALID:
            return secrets_read
        secret_is_confirmed  = secrets_read.signature == self._paired_secrets_signature
        config_is_compatible = (
            config_read.status is not ConfigSourceStatus.VALID
            or config_read.signature == self._paired_config_signature
        )
        if secret_is_confirmed and config_is_compatible:
            return secrets_read
        error = ConfigLoadError(
            "external secrets change is pending explicit reload confirmation; "
            "runtime credentials remain revoked"
        )
        return _SourceRead(
            ConfigSourceStatus.INCONSISTENT,
            f"pending:{config_read.status.value}:{config_read.etag}:{secrets_read.etag}",
            error    = error,
            identity = secrets_read.identity,
        )

    def _prepare_confirmed_secrets_locked(
        self,
        config_read: _SourceRead,
        secrets_read: _SourceRead,
        *,
        confirm_config: bool,
    ) -> tuple[_SourceRead, bool]:
        """Prevent a new secret generation from pairing with an unrelated LKG config."""

        if secrets_read.status is not ConfigSourceStatus.VALID:
            return secrets_read, True
        config_can_authorize_new_secrets = config_read.status is ConfigSourceStatus.VALID and (
            confirm_config or config_read.signature == self._paired_config_signature
        )
        if config_can_authorize_new_secrets:
            return secrets_read, True
        guarded = self._guard_unconfirmed_secrets_locked(config_read, secrets_read)
        return guarded, guarded is secrets_read

    def _replace_snapshot(
        self,
        config: Mapping[str, Any],
        secrets: Mapping[str, Any],
    ) -> None:
        """Install detached internal copies; callers cannot retain a mutable alias."""

        config_view  = _freeze_config_mapping(config)
        secrets_view = _freeze_config_mapping(secrets)
        config_copy  = cast(dict[str, Any], materialize_snapshot_value(config_view))
        secrets_copy = cast(dict[str, Any], materialize_snapshot_value(secrets_view))
        # Commit only after every copy and validation step succeeds.
        self._config       = config_copy
        self._secrets      = secrets_copy
        self._config_view  = config_view
        self._secrets_view = secrets_view

    def _apply_source_reads_locked(
        self,
        config_read: _SourceRead,
        secrets_read: _SourceRead,
        *,
        force: bool,
        bump_revision: bool,
        notify: bool,
    ) -> tuple[ConfigSnapshot, _ConfigNotification | None, bool]:
        """Publish config LKG and fail-closed secrets as one coherent revision."""

        source_changed = (
            config_read.signature != self._config_source.signature
            or secrets_read.signature != self._secrets_source.signature
        )
        next_config = (
            config_read.value
            if config_read.status is ConfigSourceStatus.VALID and config_read.value is not None
            else self._config
        )
        next_secrets = (
            secrets_read.value
            if secrets_read.status is ConfigSourceStatus.VALID and secrets_read.value is not None
            else {}
        )
        data_changed = next_config != self._config or next_secrets != self._secrets
        changed      = force or source_changed or data_changed

        if not changed:
            self._config_source  = config_read
            self._secrets_source = secrets_read
            return self._snapshot_locked(), None, False

        self._replace_snapshot(next_config, next_secrets)
        self._config_source  = config_read
        self._secrets_source = secrets_read
        if bump_revision:
            self._revision += 1
        self._source_generation += 1
        snapshot = self._snapshot_locked()
        # Trusted authorization holders publish synchronously and independently
        # of the ordinary callback queue.  This method is always called with the
        # manager's re-entrant lock held, so hooks must remain bounded and must
        # never perform blocking I/O.
        self._dispatch_security_update(snapshot)
        notification = self._enqueue_notification_locked(snapshot) if notify else None
        return snapshot, notification, True

    def _commit_secrets_mutation(
        self,
        mutate: Callable[[dict[str, Any]], tuple[bool, T]],
    ) -> T:
        """Mutate the strict on-disk value and publish only after durable commit."""

        notification: _ConfigNotification | None = None
        failure: BaseException | None            = None
        result                                   = cast(T, None)
        with self._lock:
            with keyed_path_lock(self.secrets_path):
                current = self._read_source_unlocked(self.secrets_path)
                if current.status is not ConfigSourceStatus.VALID or current.value is None:
                    self._clear_pending_sources_locked(bump_generation=True)
                    snapshot, notification, _changed = self._apply_source_reads_locked(
                        self._config_source,
                        current,
                        force         = False,
                        bump_revision = True,
                        notify        = True,
                    )
                    self._mark_sources_paired_locked(self._config_source, current)
                    failure = self._secret_source_error(current)
                elif (
                    self._secrets_source.status is not ConfigSourceStatus.VALID
                    or current.etag != self._secrets_source.etag
                ):
                    staged, _pending_changed = self._stage_unconfirmed_secret_change_locked(
                        self._config_source,
                        current,
                    )
                    if staged:
                        snapshot = self._snapshot_locked()
                    else:
                        self._clear_pending_sources_locked(bump_generation=True)
                        guarded = self._guard_unconfirmed_secrets_locked(
                            self._config_source,
                            current,
                        )
                        snapshot, notification, _changed = self._apply_source_reads_locked(
                            self._config_source,
                            guarded,
                            force         = False,
                            bump_revision = True,
                            notify        = True,
                        )
                    failure = ConfigLoadError(
                        "secrets changed on disk; reload the latest source and retry the mutation"
                    )
                else:
                    candidate = copy.deepcopy(current.value)
                    try:
                        changed, result = mutate(candidate)
                        committed = (
                            self._write_secrets_unlocked(
                                candidate,
                                expected_etag=current.etag,
                            )
                            if changed
                            else current
                        )
                    except BaseException as exc:
                        # A write can fail after partially changing the primary.
                        # Reconcile it before propagating so live auth never keeps
                        # credentials contradicted by the observed disk state.
                        observed = self._read_source_unlocked(self.secrets_path)
                        staged, _pending_changed = self._stage_unconfirmed_secret_change_locked(
                            self._config_source,
                            observed,
                        )
                        if staged:
                            snapshot = self._snapshot_locked()
                        else:
                            self._clear_pending_sources_locked(bump_generation=True)
                            guarded = self._guard_unconfirmed_secrets_locked(
                                self._config_source,
                                observed,
                            )
                            snapshot, notification, _published = self._apply_source_reads_locked(
                                self._config_source,
                                guarded,
                                force         = False,
                                bump_revision = True,
                                notify        = True,
                            )
                            if observed.status is not ConfigSourceStatus.VALID:
                                self._mark_sources_paired_locked(self._config_source, observed)
                        failure = exc
                    else:
                        published, paired = self._prepare_confirmed_secrets_locked(
                            self._config_source,
                            committed,
                            confirm_config=False,
                        )
                        snapshot, notification, _published = self._apply_source_reads_locked(
                            self._config_source,
                            published,
                            force         = False,
                            bump_revision = True,
                            notify        = True,
                        )
                        if paired:
                            self._mark_sources_paired_locked(self._config_source, committed)
                        self._clear_pending_sources_locked(bump_generation=True)
        if notification is not None:
            self._start_notification_consumer()
        if failure is not None:
            raise failure
        logger.info("Secrets transaction committed at revision %d", snapshot.revision)
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
                    raise KeyError(f"路径不存在: {'.'.join(keys[: i + 1])}")
                if not isinstance(current[key], dict):
                    raise ValueError(f"路径 {'.'.join(keys[: i + 1])} 不是字典类型")
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
                    child         = {}
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
                with suppress(ValueError):
                    self._callbacks.remove(guarded)

        return unsubscribe

    def on_security_update(
        self,
        callback: SecurityConfigCallback,
    ) -> Callable[[], None]:
        """Register a trusted synchronous authorization publication hook.

        Core authorization holders use this channel so a slow ordinary reload
        subscriber cannot delay credential revocation.  Hooks must only perform
        bounded in-memory work and must not return an awaitable.
        """

        if not callable(callback):
            raise TypeError("security config callback must be callable")
        active = True

        def guarded(snapshot: ConfigSnapshot) -> None:
            with self._lock:
                enabled = active
            if enabled:
                callback(snapshot)

        with self._lock:
            self._security_callbacks.append(guarded)

        def unsubscribe() -> None:
            nonlocal active
            with self._lock:
                if not active:
                    return
                active = False
                with suppress(ValueError):
                    self._security_callbacks.remove(guarded)

        return unsubscribe

    def on_pending_secrets_change(
        self,
        callback: PendingSecretsCallback,
    ) -> Callable[[], None]:
        """订阅合法外部 secret 候选，回调只接收不含凭据值的字段摘要。"""

        if not callable(callback):
            raise TypeError("pending secrets callback must be callable")
        active = True

        def guarded(change: PendingSecretsChange) -> Any:
            with self._lock:
                enabled = active
            if not enabled:
                return None
            return callback(change)

        with self._lock:
            self._pending_secrets_callbacks.append(guarded)

        def unsubscribe() -> None:
            nonlocal active
            with self._lock:
                if not active:
                    return
                active = False
                with suppress(ValueError):
                    self._pending_secrets_callbacks.remove(guarded)

        return unsubscribe

    def _dispatch_security_update(self, snapshot: ConfigSnapshot) -> None:
        """Apply trusted authorization updates outside the ordinary queue."""

        with self._lock:
            callbacks = tuple(self._security_callbacks)
        for callback in callbacks:
            try:
                result = callback(snapshot)
                if result is not None:
                    if asyncio.iscoroutine(result):
                        result.close()
                    raise TypeError("security config callback must be synchronous")
            except BaseException as exc:
                logger.exception("Security config callback failed: %s", exc)

    async def _dispatch_pending_secrets_change(self, change: PendingSecretsChange) -> None:
        """通知控制面合法候选；慢通知受与普通配置回调相同的时限约束。"""

        with self._lock:
            callbacks = tuple(self._pending_secrets_callbacks)
        for callback in callbacks:
            try:
                result = callback(change)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(
                        result,
                        timeout=self._callback_timeout_seconds,
                    )
            except TimeoutError:
                logger.warning("Pending secrets callback timed out")
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                logger.exception("Pending secrets callback failed: %s", exc)

    def snapshot(self) -> ConfigSnapshot:
        with self._lock:
            return self._snapshot_locked()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    async def watch(self, interval: float = 2.0) -> None:
        """Poll strict content outcomes; worker threads never publish state."""

        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(float(interval))
            or interval <= 0
        ):
            raise ValueError("config watch interval must be positive and finite")
        interval = float(interval)
        with self._lock:
            self._callback_loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(interval)
            try:
                await self._watch_reconcile_once()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                logger.exception("Config watcher iteration failed", exc_info=exc)
                self._fail_closed_watch_error(exc)

    async def _watch_reconcile_once(self) -> None:
        notification: _ConfigNotification | None          = None
        changed                                           = False
        pending_external_change                           = False
        pending_candidate_changed                         = False
        pending_notice: PendingSecretsChange | None       = None
        published_config: _SourceRead | None              = None
        published_secrets: _SourceRead | None             = None
        read_hint: tuple[_SourceRead, _SourceRead] | None = None
        for _attempt in range(_MAX_WATCH_STABILITY_RETRIES):
            with self._lock:
                generation = self._source_generation
                if read_hint is None:
                    read_hint = self._pending_read_hint_locked()
            candidate = await asyncio.to_thread(
                self._read_sources,
                read_hint,
                allow_stat_reuse=True,
            )
            # Cancellation delivered while the worker was reading must win before
            # this coroutine is allowed to publish its result.
            await asyncio.sleep(0)
            with self._lock:
                if generation != self._source_generation:
                    return
            verified = await asyncio.to_thread(
                self._read_sources,
                candidate,
                allow_stat_reuse=True,
            )
            await asyncio.sleep(0)
            with self._lock:
                if generation != self._source_generation:
                    return
            if tuple(item.signature for item in candidate) != tuple(
                item.signature for item in verified
            ):
                read_hint = verified
                continue
            # A full final read remains the publication linearization point.  It
            # catches same-size edits whose mtime was deliberately restored,
            # while the two earlier observations avoid redundant parsing.
            final = await asyncio.to_thread(self._read_sources)
            await asyncio.sleep(0)
            with self._lock:
                if generation != self._source_generation:
                    return
                if tuple(item.signature for item in verified) != tuple(
                    item.signature for item in final
                ):
                    read_hint = final
                    continue
                config_read, secrets_read = final
                staged, pending_candidate_changed = self._stage_unconfirmed_secret_change_locked(
                    config_read, secrets_read
                )
                if staged:
                    pending_external_change = True
                    pending_notice          = self._pending_secrets_notice_locked()
                    break
                self._clear_pending_sources_locked(bump_generation=True)
                config_read, secrets_read, paired = self._prepare_watched_sources_locked(*final)
                _snapshot, notification, changed = self._apply_source_reads_locked(
                    config_read,
                    secrets_read,
                    force         = False,
                    bump_revision = True,
                    notify        = True,
                )
                if paired:
                    self._mark_sources_paired_locked(*final)
                published_config  = config_read
                published_secrets = secrets_read
                break
        else:
            self._fail_closed_watch_error(
                RuntimeError("configuration sources did not stabilize before publication")
            )
            return

        if pending_external_change:
            if pending_candidate_changed:
                logger.warning(
                    "External secrets change is pending explicit reload confirmation; "
                    "the active trusted credentials remain in service"
                )
            if pending_notice is not None:
                await self._dispatch_pending_secrets_change(pending_notice)
            _check_secrets_file_permissions(self.secrets_path)
            return

        if not changed or published_config is None or published_secrets is None:
            return
        self._log_source_problem("config", published_config)
        self._log_source_problem("secrets", published_secrets)
        if notification is not None:
            self._start_notification_consumer()
        if published_secrets.status is ConfigSourceStatus.VALID:
            _check_secrets_file_permissions(self.secrets_path)

    def _prepare_watched_sources_locked(
        self,
        config_read: _SourceRead,
        secrets_read: _SourceRead,
    ) -> tuple[_SourceRead, _SourceRead, bool]:
        """Apply config LKG while never authorizing an unconfirmed secret edit.

        Plain files carry no shared generation or transactional envelope.  A
        watcher therefore cannot prove that two externally staged writes belong
        together.  New valid secret bytes remain fail-closed until ``reload()``
        explicitly confirms them; previously confirmed bytes may recover after
        a transient watcher failure.
        """

        if secrets_read.status is not ConfigSourceStatus.VALID:
            # Revoked/malformed source states cannot grant authority, so they
            # are safe to confirm as the new baseline.  Recreating even
            # byte-identical old credentials will then require explicit reload.
            return config_read, secrets_read, True
        guarded_secrets = self._guard_unconfirmed_secrets_locked(config_read, secrets_read)
        paired          = (
            guarded_secrets is secrets_read
            and secrets_read.status is ConfigSourceStatus.VALID
            and secrets_read.signature == self._paired_secrets_signature
            and (
                config_read.status is not ConfigSourceStatus.VALID
                or config_read.signature == self._paired_config_signature
            )
        )
        return config_read, guarded_secrets, paired

    def _fail_closed_watch_error(self, exc: BaseException) -> None:
        error       = ConfigLoadError(f"configuration watcher failed: {type(exc).__name__}: {exc}")
        unavailable = _SourceRead(
            ConfigSourceStatus.UNAVAILABLE,
            f"watch-error:{type(exc).__name__}",
            error=error,
        )
        with self._lock:
            self._clear_pending_sources_locked(bump_generation=True)
            _snapshot, notification, changed = self._apply_source_reads_locked(
                self._config_source,
                unavailable,
                force         = False,
                bump_revision = True,
                notify        = True,
            )
        if changed:
            self._log_source_problem("secrets", unavailable)
            if notification is not None:
                self._start_notification_consumer()

    def _start_notification_consumer(self) -> None:
        """启动唯一有序消费者；通知已先入队，内容只从 `_next_notification` 读取。"""

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        start_sync                                      = False
        schedule_loop: asyncio.AbstractEventLoop | None = None
        with self._lock:
            callback_loop = self._callback_loop
            if callback_loop is not None and callback_loop.is_closed():
                self._callback_loop = None
                callback_loop       = None
            if running_loop is not None and callback_loop is None:
                callback_loop       = running_loop
                self._callback_loop = running_loop

            if not self._notification_draining:
                self._notification_draining = True
                if callback_loop is not None and callback_loop.is_running():
                    schedule_loop = callback_loop
                elif running_loop is not None:
                    schedule_loop = running_loop
                else:
                    start_sync = True

        if schedule_loop is not None:
            if running_loop is schedule_loop:
                task = schedule_loop.create_task(self._drain_notifications_async())
                task.add_done_callback(self._consume_notification_task_error)
            else:
                asyncio.run_coroutine_threadsafe(
                    self._drain_notifications_async(),
                    schedule_loop,
                )
        elif start_sync:
            self._drain_notifications_sync()

    @staticmethod
    def _consume_notification_task_error(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except BaseException:
            logger.exception("Config notification consumer failed")

    def _next_notification(self) -> _ConfigNotification | None:
        with self._lock:
            if self._notification_current is not None:
                notification               = self._notification_current
                self._notification_current = None
                return notification
            if self._notification_queue:
                return self._notification_queue.popleft()
            self._notification_draining = False
            return None

    def _complete_notification(self, notification: _ConfigNotification) -> None:
        with self._lock:
            self._last_notified_revision = max(
                self._last_notified_revision,
                notification.revision,
            )

    def _run_notification_sync(self, notification: _ConfigNotification) -> None:
        for callback in notification.callbacks:
            try:
                result = callback(notification.snapshot)
                if inspect.isawaitable(result):
                    asyncio.run(
                        asyncio.wait_for(
                            result,
                            timeout=self._callback_timeout_seconds,
                        )
                    )
            except TimeoutError:
                logger.warning(
                    "Config callback timed out at revision %d",
                    notification.revision,
                )
            except asyncio.CancelledError:
                logger.warning("Config callback cancelled at revision %d", notification.revision)
            except BaseException as exc:
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
            self._callback_loop = asyncio.get_running_loop()
        while True:
            notification = self._next_notification()
            if notification is None:
                return
            try:
                for callback in notification.callbacks:
                    try:
                        result = callback(notification.snapshot)
                        if inspect.isawaitable(result):
                            await asyncio.wait_for(
                                result,
                                timeout=self._callback_timeout_seconds,
                            )
                    except TimeoutError:
                        logger.warning(
                            "Config callback timed out at revision %d",
                            notification.revision,
                        )
                    except asyncio.CancelledError:
                        logger.warning(
                            "Config callback cancelled at revision %d",
                            notification.revision,
                        )
                    except BaseException as exc:
                        logger.exception("Config callback failed: %s", exc)
            finally:
                self._complete_notification(notification)

    def _cached_parsed_source(
        self,
        path: Path,
        *,
        etag: str,
        identity: str,
        stat_key: _SourceStatKey,
    ) -> _SourceRead | None:
        with self._parsed_source_cache_lock:
            cached = self._parsed_source_cache.get(path)
        if cached is None or cached.etag != etag:
            return None
        return _SourceRead(
            cached.status,
            etag,
            value    = cached.value,
            error    = cached.error,
            identity = identity,
            stat_key = stat_key,
        )

    def _remember_parsed_source(self, path: Path, source: _SourceRead) -> None:
        if source.status not in {ConfigSourceStatus.VALID, ConfigSourceStatus.INVALID}:
            return
        with self._parsed_source_cache_lock:
            self._parsed_source_cache[path] = source

    @staticmethod
    def _reuse_unchanged_source(path: Path, previous: _SourceRead) -> _SourceRead | None:
        if previous.status is ConfigSourceStatus.MISSING:
            try:
                path.stat()
            except FileNotFoundError:
                return previous
            except OSError:
                return None
            return None
        if previous.status not in {ConfigSourceStatus.VALID, ConfigSourceStatus.INVALID}:
            return None
        try:
            current_stat = path.stat()
        except OSError:
            return None
        if previous.stat_key == _source_stat_key(current_stat):
            return previous
        return None

    def _read_source_unlocked(self, path: Path) -> _SourceRead:
        """Read one exact primary file without backup recovery."""

        try:
            with path.open("rb") as handle:
                initial_stat  = os.fstat(handle.fileno())
                observed_size = initial_stat.st_size
                raw           = handle.read(_MAX_CONFIG_SOURCE_BYTES + 1)
                final_stat    = os.fstat(handle.fileno())
                path_stat     = path.stat()
        except FileNotFoundError:
            return _SourceRead(ConfigSourceStatus.MISSING, MISSING_ETAG)
        except OSError as exc:
            error = ConfigLoadError(path, exc)
            token = f"unavailable:{type(exc).__name__}:{getattr(exc, 'errno', None)}"
            return _SourceRead(ConfigSourceStatus.UNAVAILABLE, token, error=error)

        changed_during_read = (
            not os.path.samestat(final_stat, path_stat)
            or initial_stat.st_size != final_stat.st_size
            or initial_stat.st_mtime_ns != final_stat.st_mtime_ns
            or (final_stat.st_size <= _MAX_CONFIG_SOURCE_BYTES and len(raw) != final_stat.st_size)
        )
        if changed_during_read:
            token = f"volatile:{final_stat.st_size}:{final_stat.st_mtime_ns}:{_payload_etag(raw)}"
            error = ConfigLoadError(f"Configuration at {path} changed while it was being read")
            return _SourceRead(ConfigSourceStatus.UNAVAILABLE, token, error=error)

        identity = _source_identity(final_stat)
        stat_key = _source_stat_key(final_stat)
        if len(raw) > _MAX_CONFIG_SOURCE_BYTES:
            token = f"oversize:{observed_size}:{_payload_etag(raw)}"
            error = ConfigLoadError(
                f"Configuration at {path} exceeds {_MAX_CONFIG_SOURCE_BYTES} bytes"
            )
            return _SourceRead(
                ConfigSourceStatus.INVALID,
                token,
                error    = error,
                identity = identity,
                stat_key = stat_key,
            )

        etag   = _payload_etag(raw)
        cached = self._cached_parsed_source(
            path,
            etag     = etag,
            identity = identity,
            stat_key = stat_key,
        )
        if cached is not None:
            return cached
        try:
            decoded = raw.decode("utf-8")
            value = json.loads(decoded, parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise ValueError("top-level JSON value must be an object")
            if path == self.config_path:
                value = _validate_runtime_config(value)
            # JSON's exponent syntax can decode to infinity without invoking
            # parse_constant.  Round-trip through the strict tree validator so
            # every nested value is finite and supported before it is VALID.
            value = cast(
                dict[str, Any],
                materialize_snapshot_value(_freeze_config_mapping(value)),
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            RecursionError,
            ConfigLoadError,
        ) as exc:
            error  = exc if isinstance(exc, ConfigLoadError) else ConfigLoadError(path, exc)
            result = _SourceRead(
                ConfigSourceStatus.INVALID,
                etag,
                error    = error,
                identity = identity,
                stat_key = stat_key,
            )
            self._remember_parsed_source(path, result)
            return result
        result = _SourceRead(
            ConfigSourceStatus.VALID,
            etag,
            value    = value,
            identity = identity,
            stat_key = stat_key,
        )
        self._remember_parsed_source(path, result)
        return result

    def _read_sources(
        self,
        previous: tuple[_SourceRead, _SourceRead] | None = None,
        *,
        allow_stat_reuse: bool = False,
    ) -> tuple[_SourceRead, _SourceRead]:
        # Holding both local path locks prevents another managed writer from
        # interleaving generations between the two reads.  Canonical ordering
        # also avoids deadlock if two managers are accidentally wired in reverse.
        unique_paths = {
            self.config_path.expanduser().resolve(strict=False),
            self.secrets_path.expanduser().resolve(strict=False),
        }
        ordered_paths = sorted(unique_paths, key=lambda item: os.path.normcase(str(item)))
        with ExitStack() as locks:
            for path in ordered_paths:
                locks.enter_context(keyed_path_lock(path))
            reads: list[_SourceRead] = []
            for index, path in enumerate((self.config_path, self.secrets_path)):
                reused = (
                    self._reuse_unchanged_source(path, previous[index])
                    if allow_stat_reuse and previous is not None
                    else None
                )
                reads.append(reused or self._read_source_unlocked(path))
            return reads[0], reads[1]

    def _read_stable_sources(self) -> tuple[_SourceRead, _SourceRead]:
        """Require three identical pair reads before explicit confirmation.

        The final matching read is the publication linearization point.  This
        detects ordinary torn/staged saves but cannot synchronize with a
        non-cooperating process that starts another write after that point.
        """

        previous_reads = (self._config_source, self._secrets_source)
        previous_signatures: tuple[tuple[ConfigSourceStatus, str, str], ...] | None = None
        stable_count = 0
        last_signatures: tuple[tuple[ConfigSourceStatus, str, str], ...] = ()
        for _attempt in range(_MAX_STABLE_SOURCE_READS):
            require_content_read = stable_count >= _REQUIRED_STABLE_SOURCE_READS - 1
            current              = self._read_sources(
                previous_reads,
                allow_stat_reuse=not require_content_read,
            )
            signatures = tuple(item.signature for item in current)
            if signatures == previous_signatures:
                stable_count += 1
            else:
                previous_signatures = signatures
                stable_count        = 1
            last_signatures = signatures
            previous_reads  = current
            if stable_count >= _REQUIRED_STABLE_SOURCE_READS:
                return current

        token = _payload_etag(repr(last_signatures).encode("utf-8"))
        error = ConfigLoadError(
            "config and secrets did not remain stable long enough for explicit confirmation"
        )
        return (
            _SourceRead(ConfigSourceStatus.UNAVAILABLE, f"unstable-config:{token}", error=error),
            _SourceRead(ConfigSourceStatus.UNAVAILABLE, f"unstable-secrets:{token}", error=error),
        )

    def _write_secrets_unlocked(
        self,
        candidate: Mapping[str, Any],
        *,
        expected_etag: str,
    ) -> _SourceRead:
        """Write a verified inode without recreating a deleted/replaced primary.

        Managed writers are serialized by the keyed lock.  Pre/post identity and
        content checks make an external replace, delete, or observed same-inode
        overwrite fail closed.  No portable advisory lock can stop a deliberately
        non-cooperating process from writing after the final check; the watcher
        treats any later external secret content as unconfirmed and revokes it.
        A crash may leave this file invalid, which the strict reader also handles
        fail closed.
        """

        _validate_config_tree(candidate)
        payload = json.dumps(
            candidate,
            indent       = "\t",
            ensure_ascii = False,
            allow_nan    = False,
        )
        payload_bytes = payload.encode("utf-8")
        if len(payload_bytes) > _MAX_CONFIG_SOURCE_BYTES:
            raise ValueError(f"secrets exceed {_MAX_CONFIG_SOURCE_BYTES} bytes")
        canonical = json.loads(payload, parse_constant=_reject_json_constant)
        if not isinstance(canonical, dict):
            raise TypeError("secrets must be a JSON object")
        _validate_config_tree(canonical)

        try:
            handle = self.secrets_path.open("r+b", buffering=0)
        except OSError as exc:
            raise ConfigLoadError(self.secrets_path, exc) from exc
        with handle:
            current_payload = handle.read(_MAX_CONFIG_SOURCE_BYTES + 1)
            if len(current_payload) > _MAX_CONFIG_SOURCE_BYTES:
                raise ConfigLoadError("secrets changed on disk before commit")
            if _payload_etag(current_payload) != expected_etag:
                raise ConfigLoadError("secrets changed on disk before commit")
            handle_stat = os.fstat(handle.fileno())
            try:
                path_stat = self.secrets_path.stat()
            except OSError as exc:
                raise ConfigLoadError("secrets changed on disk before commit") from exc
            if not os.path.samestat(handle_stat, path_stat):
                raise ConfigLoadError("secrets changed on disk before commit")

            _write_secret_payload(handle, payload_bytes)

            try:
                committed_stat = self.secrets_path.stat()
            except OSError as exc:
                raise ConfigLoadError("secrets changed on disk during commit") from exc
            if not os.path.samestat(handle_stat, committed_stat):
                raise ConfigLoadError("secrets changed on disk during commit")
            handle.seek(0)
            observed_payload = handle.read(_MAX_CONFIG_SOURCE_BYTES + 1)
            if observed_payload != payload_bytes:
                raise ConfigLoadError("secrets changed on disk during commit")
            final_handle_stat = os.fstat(handle.fileno())
            if not os.path.samestat(handle_stat, final_handle_stat):
                raise ConfigLoadError("secrets changed on disk during commit")
            try:
                final_path_stat = self.secrets_path.stat()
            except OSError as exc:
                raise ConfigLoadError("secrets changed on disk during commit") from exc
            if not os.path.samestat(final_handle_stat, final_path_stat):
                raise ConfigLoadError("secrets changed on disk during commit")
        try:
            closed_path_stat = self.secrets_path.stat()
        except OSError as exc:
            raise ConfigLoadError("secrets changed on disk during commit") from exc
        if not os.path.samestat(final_handle_stat, closed_path_stat):
            raise ConfigLoadError("secrets changed on disk during commit")
        result = _SourceRead(
            ConfigSourceStatus.VALID,
            _payload_etag(payload_bytes),
            value    = canonical,
            identity = _source_identity(closed_path_stat),
            stat_key = _source_stat_key(closed_path_stat),
        )
        self._remember_parsed_source(self.secrets_path, result)
        return result

    @staticmethod
    def _secret_source_error(source: _SourceRead) -> ConfigLoadError:
        if source.error is not None:
            return source.error
        return ConfigLoadError(
            f"Refusing to mutate secrets while primary source is {source.status.value}"
        )

    @staticmethod
    def _log_source_problem(name: str, source: _SourceRead) -> None:
        if source.status is ConfigSourceStatus.VALID:
            return
        if source.status is ConfigSourceStatus.MISSING:
            logger.warning("%s source is missing", name)
            return
        logger.error("%s source is %s: %s", name, source.status.value, source.error)


__all__ = [
    "ConfigLoadError",
    "ConfigManager",
    "ConfigSnapshot",
    "ConfigSourceStatus",
    "materialize_snapshot_value",
]
