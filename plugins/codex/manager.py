"""管理 Codex 会话、串行队列、执行许可、历史和结果投递。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import time
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from core.atomic_store import AtomicJsonStore
from core.clock import now_in_configured_timezone
from core.dispatcher import AdjustableSemaphore
from core.interfaces import ACTION_BYPASS_SINK_KEY, DeliveryTarget, PluginContextProtocol
from core.plugin_base import (
    build_action,
    image,
    segments,
    split_message_segments,
)

from .artifacts import (
    ArtifactLimits,
    CodexImageArtifact,
    collect_image_artifacts,
)
from .config import (
    LABEL_PATTERN,
    CodexPluginConfig,
    load_plugin_config,
    load_plugin_config_snapshot,
)
from .paths import CwdError, normalize_cwd
from .runner import CodexRunner, CodexRunResult, terminate_process_tree

logger = logging.getLogger(__name__)
SESSION_STATE_VERSION = 1
MAX_SESSION_STATE_BYTES = 8 * 1024 * 1024
MAX_PERSISTED_SESSIONS = 10_000


def _warn_dangerous_config(config: CodexPluginConfig) -> None:
    if config.sandbox == "danger-full-access":
        logger.warning(
            "Codex sandbox=danger-full-access grants the CLI unrestricted local filesystem "
            "and process authority; use only with a trusted administrator configuration"
        )


def _public_path_name(value: str | Path) -> str:
    name = Path(value).name
    return name or "unnamed"


MAX_PERSISTED_COUNTER = 2**63 - 1
_SESSION_FIELDS = frozenset(
    {
        "label",
        "cwd",
        "owner_user_id",
        "target_group_id",
        "thread_id",
        "created_at",
        "updated_at",
        "total_jobs",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
    }
)


def _persisted_optional_id(raw: dict[str, Any], name: str) -> int | None:
    value = raw[name]
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise TypeError(f"Invalid persisted {name}")
    return value


def _persisted_counter(raw: dict[str, Any], name: str) -> int:
    value = raw.get(name, 0)
    if type(value) is not int or not 0 <= value <= MAX_PERSISTED_COUNTER:
        raise TypeError(f"Invalid persisted {name}")
    return value


def _persisted_timestamp(raw: dict[str, Any], name: str) -> float:
    value = raw.get(name, time.time())
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Invalid persisted {name}")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"Invalid persisted {name}")
    return timestamp


def _safe_usage_counter(usage: dict[str, Any], name: str) -> int:
    value = usage.get(name, 0)
    return value if type(value) is int and 0 <= value <= MAX_PERSISTED_COUNTER else 0


def _optional_positive_id(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer or None")
    return value


def _load_config_generation(
    context: PluginContextProtocol,
) -> tuple[CodexPluginConfig, int | None]:
    """同时读取配置及生成 config/secrets 两棵设置树的版本号。"""
    settings = context.get_settings_snapshot()
    revision = getattr(settings, "revision", None)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("Codex settings snapshot revision must be a non-negative integer")
    return load_plugin_config_snapshot(settings, data_dir=context.data_dir), revision


@dataclass
class CodexSession:
    label: str
    cwd: str
    owner_user_id: int | None
    target_group_id: int | None
    thread_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    total_jobs: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RuntimeJob:
    job_id: int
    label: str
    prompt: str
    user_id: int | None
    group_id: int | None
    context: PluginContextProtocol
    delivery_targets: tuple[DeliveryTarget, ...] | None = None
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: CodexRunResult | None = None
    image_artifacts: list[CodexImageArtifact] = field(default_factory=list)
    artifact_dropped_count: int = 0
    artifact_drop_reasons: dict[str, int] = field(default_factory=dict)
    artifact_scan_truncated: bool = False
    process: asyncio.subprocess.Process | None = None
    cancel_requested: bool = False
    prompt_started: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    spawn_handoff: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    finished_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_config: CodexPluginConfig | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _JobStart:
    config: CodexPluginConfig
    runner: Any
    cwd: Path


class CodexQueueManager:
    def __init__(
        self,
        context: PluginContextProtocol,
        *,
        config: CodexPluginConfig | None = None,
        runner: CodexRunner | None = None,
        settings_revision: int | None = None,
    ) -> None:
        self.context = context
        self.config = config or load_plugin_config(context)
        _warn_dangerous_config(self.config)
        self.data_dir = Path(getattr(context, "data_dir", Path("data") / "codex"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_path = self.data_dir / "sessions.json"
        self.output_dir = self.data_dir / "outputs"
        self.session_root = self.data_dir / "session"
        self.deleted_session_root = self.data_dir / "deleted_sessions"
        self.session_root.mkdir(parents=True, exist_ok=True)
        self._owns_runner = runner is None
        self.runner = runner or CodexRunner(self.config, self.output_dir)
        self.settings_revision = settings_revision
        self.sessions: dict[str, CodexSession] = {}
        self.queues: dict[str, deque[RuntimeJob]] = {}
        self.running: dict[str, RuntimeJob] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.tombstones: set[str] = set()
        self.shutting_down = False
        self.lock = asyncio.Lock()
        self.global_sem = AdjustableSemaphore(self.config.max_parallel_jobs)
        self._load()

    async def reconfigure(
        self,
        context: PluginContextProtocol,
        config: CodexPluginConfig,
        *,
        settings_revision: int | None = None,
    ) -> bool:
        """原子发布只对尚未启动任务生效的新配置。

        运行中任务继续持有启动时捕获的 runner/config 对；单例 manager、会话、队列和活动
        进程保持不变，只有之后启动的任务使用替换后的 runner 和策略快照。
        """

        async with self.lock:
            if self.shutting_down:
                raise RuntimeError("Codex manager is shutting down")
            current_revision = self.settings_revision
            if (
                settings_revision is not None
                and current_revision is not None
                and settings_revision < current_revision
            ):
                logger.info(
                    "Ignoring stale Codex settings revision: incoming=%d current=%d",
                    settings_revision,
                    current_revision,
                )
                return False
            if (
                settings_revision is not None
                and settings_revision == current_revision
                and config != self.config
            ):
                raise RuntimeError("conflicting Codex settings for the same revision")

            if config == self.config:
                self.context = context
                if settings_revision is not None:
                    self.settings_revision = settings_revision
                return False

            normalize_cwd(None, config)
            next_runner = CodexRunner(config, self.output_dir) if self._owns_runner else self.runner
            self.global_sem.resize(config.max_parallel_jobs)
            _warn_dangerous_config(config)
            self.context = context
            if settings_revision is not None:
                self.settings_revision = settings_revision
            self.config = config
            self.runner = next_runner
            return True

    async def refresh_from_settings_reader(self) -> bool:
        """排队任务启动前，从同一代受限设置快照刷新配置。

        队列可在没有新 Codex 消息时自行推进，因此只在 ``get_manager`` 刷新会使排队任务继续
        使用过期的公开安全策略。context reader 只暴露同一原子版本中本插件的 config/secrets，
        不暴露原始 ConfigManager。
        """

        context = self.context
        settings_reader = getattr(context, "get_settings_snapshot", None)
        if not callable(settings_reader):
            return False
        config, revision = _load_config_generation(context)
        return await self.reconfigure(
            context,
            config,
            settings_revision=revision,
        )

    def _load(self) -> None:
        source_path = self.sessions_path
        recovered_from_backup = False
        try:
            data = self._read_state_file(source_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._quarantine_state(source_path)
            source_path = self.sessions_path.with_name(f"{self.sessions_path.name}.bak")
            try:
                data = self._read_state_file(source_path)
                recovered_from_backup = True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self.sessions = {}
                self._rewrite_state_with_backup()
                return

        try:
            sessions, migrated, invalid_record = self._decode_state(data)
        except (TypeError, ValueError, CwdError):
            self._quarantine_state(source_path)
            self.sessions = {}
            self._rewrite_state_with_backup()
            return

        self.sessions = sessions
        if invalid_record:
            self._quarantine_state(source_path)
        if recovered_from_backup or migrated or invalid_record:
            self._rewrite_state_with_backup()

    @staticmethod
    def _read_state_file(path: Path) -> Any:
        payload = path.read_bytes()
        if len(payload) > MAX_SESSION_STATE_BYTES:
            raise ValueError("Codex session state exceeds the safe byte limit")
        return json.loads(payload.decode("utf-8"))

    def _quarantine_state(self, source: Path) -> Path | None:
        if not source.is_file() or source.is_symlink():
            return None
        quarantine_dir = self.data_dir / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / f"sessions-{time.time_ns()}.json"
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            logger.warning(
                "Failed to quarantine Codex session state error_type=%s",
                type(exc).__name__,
            )
            return None
        logger.warning("Quarantined invalid Codex session state name=%s", target.name)
        return target

    def _decode_state(self, data: Any) -> tuple[dict[str, CodexSession], bool, bool]:
        if not isinstance(data, dict):
            raise TypeError("Codex state root must be an object")
        if set(data) - {"schema_version", "sessions"}:
            raise ValueError("Codex state root contains unknown fields")
        version = data.get("schema_version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or version not in {0, 1}:
            raise ValueError("Unsupported Codex state schema version")
        raw_sessions = data.get("sessions", {})
        if not isinstance(raw_sessions, dict) or len(raw_sessions) > MAX_PERSISTED_SESSIONS:
            raise TypeError("Codex sessions must be a bounded object")

        sessions: dict[str, CodexSession] = {}
        invalid_record = False
        for label, raw_session in raw_sessions.items():
            try:
                session = self._decode_session(label, raw_session)
            except (TypeError, ValueError, CwdError):
                invalid_record = True
                continue
            sessions[label] = session
        return sessions, version == 0, invalid_record

    def _decode_session(self, label: Any, raw: Any) -> CodexSession:
        if not isinstance(label, str) or re.fullmatch(LABEL_PATTERN, label) is None:
            raise ValueError("Invalid persisted session label")
        if not isinstance(raw, dict) or set(raw) - _SESSION_FIELDS:
            raise TypeError("Invalid persisted session object")
        required = {"label", "cwd", "owner_user_id", "target_group_id"}
        if not required.issubset(raw) or raw["label"] != label:
            raise ValueError("Persisted session identity mismatch")

        cwd = raw["cwd"]
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd or len(cwd) > 32_768:
            raise ValueError("Invalid persisted session cwd")
        normalized_cwd = normalize_cwd(cwd, self.config)

        thread_id = raw.get("thread_id")
        if thread_id is not None and (
            not isinstance(thread_id, str) or not thread_id or len(thread_id) > 512
        ):
            raise TypeError("Invalid persisted thread_id")
        return CodexSession(
            label=label,
            cwd=str(normalized_cwd),
            owner_user_id=_persisted_optional_id(raw, "owner_user_id"),
            target_group_id=_persisted_optional_id(raw, "target_group_id"),
            thread_id=thread_id,
            created_at=_persisted_timestamp(raw, "created_at"),
            updated_at=_persisted_timestamp(raw, "updated_at"),
            total_jobs=_persisted_counter(raw, "total_jobs"),
            input_tokens=_persisted_counter(raw, "input_tokens"),
            cached_input_tokens=_persisted_counter(raw, "cached_input_tokens"),
            output_tokens=_persisted_counter(raw, "output_tokens"),
        )

    def _save(self) -> None:
        AtomicJsonStore(self.sessions_path).write(self._session_state_payload())

    def _session_state_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_STATE_VERSION,
            "sessions": {label: asdict(session) for label, session in self.sessions.items()},
        }

    def _rewrite_state_with_backup(self) -> None:
        """替换不可信状态，并用干净副本重建崩溃恢复备份。"""
        AtomicJsonStore(self.sessions_path).write_with_backup(self._session_state_payload())

    def _session_dir(self, label: str) -> Path:
        path = self.session_root / label
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _archive_session_dir_locked(self, label: str) -> Path | None:
        session_dir = self.session_root / label
        if not session_dir.exists():
            return None

        self.deleted_session_root.mkdir(parents=True, exist_ok=True)
        stamp = now_in_configured_timezone(self.context).strftime("%Y%m%d-%H%M%S")
        base_name = f"{label}-{stamp}"
        target = self.deleted_session_root / base_name
        suffix = 2
        while target.exists():
            target = self.deleted_session_root / f"{base_name}-{suffix}"
            suffix += 1
        session_dir.rename(target)
        return target

    def _conversation_path(self, label: str) -> Path:
        return self._session_dir(label) / "conversation.jsonl"

    def _session_images_dir(self, label: str) -> Path:
        path = self._session_dir(label) / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _job_artifact_path(self, label: str, job_id: int) -> Path:
        return self.session_root / label / "jobs" / f"job-{job_id:04d}" / "artifacts"

    def _job_artifact_dir(self, label: str, job_id: int) -> Path:
        path = self._job_artifact_path(label, job_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _artifact_limits(self, config: CodexPluginConfig | None = None) -> ArtifactLimits:
        config = config or self.config
        return ArtifactLimits(
            scan_max_entries=config.artifact_scan_max_entries,
            scan_max_depth=config.artifact_scan_max_depth,
            max_artifacts=config.max_image_artifacts,
            max_single_bytes=config.max_image_bytes,
            max_total_bytes=config.max_image_total_bytes,
            max_pixels=config.max_image_pixels,
            max_frames=config.max_image_frames,
        )

    def _disk_usage_bytes(self, *, max_bytes: int | None = None) -> int:
        total = 0
        for item in self.data_dir.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
                    if max_bytes is not None and total >= max_bytes:
                        return total
            except OSError:
                continue
        return total

    async def _measure_disk_usage_bytes(self, *, max_bytes: int | None = None) -> int:
        """在线程中扫描插件数据目录，避免阻塞事件循环和 manager 锁。"""

        return await asyncio.to_thread(self._disk_usage_bytes, max_bytes=max_bytes)

    def _prune_completed_job_dirs(
        self,
        *,
        cutoff: float,
        active_jobs: set[tuple[str, int]],
    ) -> None:
        try:
            session_dirs = tuple(self.session_root.iterdir())
        except OSError:
            return
        for session_dir in session_dirs:
            jobs_dir = session_dir / "jobs"
            if (
                not session_dir.is_dir()
                or session_dir.is_symlink()
                or not jobs_dir.is_dir()
                or jobs_dir.is_symlink()
            ):
                continue
            for job_dir in jobs_dir.iterdir():
                match = re.fullmatch(r"job-([0-9]{1,19})", job_dir.name)
                identity = (session_dir.name, int(match.group(1))) if match else None
                if identity not in active_jobs and self._is_expired_path(job_dir, cutoff):
                    self._remove_owned_path(job_dir, jobs_dir)
            try:
                jobs_dir.rmdir()
            except OSError:
                pass

    def _prune_retention_paths(self, cutoff: float) -> None:
        if not self.running:
            self._prune_expired_children(self.output_dir, cutoff)
        self._prune_expired_children(self.data_dir / "quarantine", cutoff)
        self._prune_expired_children(self.deleted_session_root, cutoff)
        for temp_path in self.data_dir.glob(f".{self.sessions_path.name}.*"):
            if self._is_expired_path(temp_path, cutoff):
                self._remove_owned_path(temp_path, self.data_dir)

    def _expired_session_labels(self, cutoff: float) -> list[str]:
        return [
            label
            for label, session in self.sessions.items()
            if session.updated_at < cutoff
            and not self._is_protected_session(label)
            and label not in self.running
            and not self.queues.get(label)
        ]

    def _expire_session_locked(self, label: str) -> asyncio.Task[None] | None:
        self.tombstones.add(label)
        worker = self.workers.pop(label, None)
        self._append_history(label, {"type": "session.expired", "label": label})
        self._archive_session_dir_locked(label)
        self.sessions.pop(label, None)
        self.queues.pop(label, None)
        if worker is not None and not worker.done():
            worker.cancel()
            return worker
        return None

    async def maintenance(self) -> None:
        """在 manager 锁内确定所有权，只清理已过期且非活动的自有路径。"""

        now = time.time()
        cancelled_workers: list[asyncio.Task[None]] = []
        async with self.lock:
            retention_days = self.config.artifact_retention_days
            if retention_days > 0:
                cutoff = now - retention_days * 86400
                active_jobs = {
                    (job.label, job.job_id)
                    for job in (
                        *self.running.values(),
                        *(queued for queue in self.queues.values() for queued in queue),
                    )
                }
                self._prune_completed_job_dirs(cutoff=cutoff, active_jobs=active_jobs)
                self._prune_retention_paths(cutoff)

            session_ttl_days = self.config.session_ttl_days
            if session_ttl_days > 0:
                expired_labels = self._expired_session_labels(now - session_ttl_days * 86400)
                for label in expired_labels:
                    if worker := self._expire_session_locked(label):
                        cancelled_workers.append(worker)
                if expired_labels:
                    self._save()
        if cancelled_workers:
            await asyncio.gather(*cancelled_workers, return_exceptions=True)

    @staticmethod
    def _is_expired_path(path: Path, cutoff: float) -> bool:
        try:
            return path.stat(follow_symlinks=False).st_mtime < cutoff
        except OSError:
            return False

    @staticmethod
    def _remove_owned_path(path: Path, root: Path) -> None:
        try:
            root_resolved = root.resolve(strict=False)
            if path.parent.resolve(strict=False) != root_resolved:
                raise ValueError("Refusing to remove a path outside its owned root")
            is_junction = getattr(path, "is_junction", lambda: False)
            if is_junction():
                raise ValueError("Refusing to recurse into a directory junction")
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Failed to prune Codex lifecycle path name=%s error_type=%s",
                path.name,
                type(exc).__name__,
            )

    def _prune_expired_children(self, root: Path, cutoff: float) -> None:
        try:
            children = tuple(root.iterdir())
        except (FileNotFoundError, NotADirectoryError, OSError):
            return
        for child in children:
            if self._is_expired_path(child, cutoff):
                self._remove_owned_path(child, root)

    def _append_history(self, label: str, payload: dict[str, Any]) -> None:
        """追加一条有界事件，并保持 manager 事务中的事件顺序。

        顺序敏感的调用方已经持有 ``self.lock``；这里仅做很短的本地追加，保持同步
        可以避免把同一事务拆散后交给线程执行器。
        """

        event = {"ts": time.time(), **payload}
        line = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with self._conversation_path(label).open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    def _is_protected_session(self, label: str) -> bool:
        return label in self.config.protected_sessions

    def _create_session_record_locked(
        self,
        label: str,
        cwd: Path,
        *,
        user_id: int | None,
        group_id: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> CodexSession:
        user_id = _optional_positive_id(user_id, "user_id")
        group_id = _optional_positive_id(group_id, "group_id")
        self.tombstones.discard(label)
        session = CodexSession(
            label=label,
            cwd=str(cwd),
            owner_user_id=user_id,
            target_group_id=group_id,
        )
        self.sessions[label] = session
        self._save()
        payload: dict[str, Any] = {
            "type": "session.created",
            "label": label,
            "cwd": str(cwd),
            "owner_user_id": user_id,
            "target_group_id": group_id,
        }
        if metadata:
            payload["metadata"] = metadata
        self._append_history(label, payload)
        return session

    def _enqueue_job_locked(
        self,
        session: CodexSession,
        prompt: str,
        *,
        user_id: int | None,
        group_id: int | None,
        context: PluginContextProtocol,
        metadata: dict[str, Any] | None = None,
        delivery_targets: tuple[DeliveryTarget, ...] | None = None,
        disk_usage_bytes: int,
    ) -> tuple[RuntimeJob, int]:
        user_id = _optional_positive_id(user_id, "user_id")
        group_id = _optional_positive_id(group_id, "group_id")
        if delivery_targets is not None and (
            not isinstance(delivery_targets, tuple)
            or any(not isinstance(target, DeliveryTarget) for target in delivery_targets)
        ):
            raise TypeError("delivery_targets must be a tuple of DeliveryTarget values")
        capabilities = getattr(context, "capabilities", None)
        if getattr(capabilities, "is_system", False) and delivery_targets is None:
            raise RuntimeError("system Codex jobs require explicit delivery targets")
        if self.shutting_down or session.label in self.tombstones:
            raise RuntimeError("Codex manager 正在关闭，不能接收新任务。")
        queue = self.queues.setdefault(session.label, deque())
        job_metadata = dict(metadata or {})
        queued_count = sum(1 for queued in queue if not queued.metadata.get("queue_overhead"))
        if queued_count >= self.config.emergency_queue_limit:
            raise RuntimeError(
                f"会话 `{session.label}` 达到进程保护紧急队列上限 "
                f"{self.config.emergency_queue_limit}。"
            )
        if disk_usage_bytes >= self.config.emergency_disk_bytes:
            raise RuntimeError("Codex 数据目录达到紧急磁盘保护阈值，请清理制品或提高配置。")
        if queued_count >= self.config.per_session_queue_limit:
            raise RuntimeError(
                f"会话 `{session.label}` 已达到队列上限 {self.config.per_session_queue_limit}。"
            )
        if len(prompt) > self.config.max_prompt_chars:
            raise RuntimeError(f"任务内容超过 {self.config.max_prompt_chars} 字符上限。")
        previous_total_jobs = session.total_jobs
        previous_updated_at = session.updated_at
        session.total_jobs += 1
        session.updated_at = time.time()
        tasks_ahead = len(queue) + (1 if session.label in self.running else 0)
        job = RuntimeJob(
            job_id=session.total_jobs,
            label=session.label,
            prompt=prompt,
            user_id=user_id,
            group_id=group_id,
            context=context,
            delivery_targets=delivery_targets,
            metadata=job_metadata,
        )
        state_saved = False
        try:
            self._save()
            state_saved = True
            self._append_history(
                session.label,
                {
                    "type": "message",
                    "role": "user",
                    "job_id": job.job_id,
                    "content": prompt,
                    "user_id": user_id,
                    "group_id": group_id,
                    "delivery_targets": (
                        [asdict(target) for target in delivery_targets]
                        if delivery_targets is not None
                        else None
                    ),
                    "status": "queued",
                    "metadata": job.metadata,
                },
            )
            # ``create_task`` 不会在当前同步临界区内执行 worker；先确保 worker
            # 可创建，再发布队列元素，失败时不会留下无人管理的任务。
            self._ensure_worker_locked(session.label)
            queue.append(job)
        except Exception as exc:
            session.total_jobs = previous_total_jobs
            session.updated_at = previous_updated_at
            with suppress(ValueError):
                queue.remove(job)
            if state_saved:
                try:
                    self._save()
                except Exception as rollback_exc:
                    logger.error(
                        "Codex enqueue rollback persistence failed: label=%s error_type=%s",
                        session.label,
                        type(rollback_exc).__name__,
                    )
            logger.error(
                "Codex enqueue persistence failed: label=%s error_type=%s",
                session.label,
                type(exc).__name__,
            )
            raise RuntimeError("Codex 任务保存失败，未加入执行队列。") from exc

        logger.info(
            "Codex job queued: label=%s job_id=%s queue_ahead=%s metadata_keys=%s prompt_chars=%d",
            session.label,
            job.job_id,
            tasks_ahead,
            sorted(str(key)[:64] for key in job.metadata)[:16],
            len(prompt),
        )
        return job, tasks_ahead

    async def ensure_default_cwd(self) -> None:
        normalize_cwd(None, self.config)

    async def create_session(
        self,
        label: str,
        cwd_text: str | None,
        *,
        user_id: int | None,
        group_id: int | None,
    ) -> str:
        label = label.strip()
        if re.fullmatch(LABEL_PATTERN, label) is None:
            return "会话名只能使用 1-32 位字母、数字、下划线或横线。"
        try:
            cwd = normalize_cwd(cwd_text, self.config)
        except CwdError as exc:
            return str(exc)

        async with self.lock:
            if label in self.sessions:
                return f"Codex 会话已存在: {label}"
            self._create_session_record_locked(
                label=label,
                cwd=cwd,
                user_id=user_id,
                group_id=group_id,
            )
        return f"已创建 Codex 会话 `{label}`\n工作目录名称: {_public_path_name(cwd)}"

    async def enqueue(
        self,
        label: str,
        prompt: str,
        *,
        user_id: int | None,
        group_id: int | None,
        context: PluginContextProtocol,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        prompt = prompt.strip()
        if not prompt:
            return "任务内容不能为空。"

        async with self.lock:
            if label not in self.sessions:
                return f"Codex 会话不存在: {label}\n先用 /codex create {label} 创建。"
        disk_usage_bytes = await self._measure_disk_usage_bytes(
            max_bytes=self.config.emergency_disk_bytes
        )

        async with self.lock:
            session = self.sessions.get(label)
            if not session:
                return f"Codex 会话不存在: {label}\n先用 /codex create {label} 创建。"
            try:
                job, tasks_ahead = self._enqueue_job_locked(
                    session,
                    prompt,
                    user_id=user_id,
                    group_id=group_id,
                    context=context,
                    metadata=metadata,
                    disk_usage_bytes=disk_usage_bytes,
                )
            except RuntimeError as exc:
                return str(exc)
            if tasks_ahead:
                return (
                    f"已加入 Codex 队列: `{label}` #{job.job_id}\n前面还有 {tasks_ahead} 个任务。"
                )
            return f"已收到 Codex 任务: `{label}` #{job.job_id}\n开始后台执行。"

    def _ensure_worker_locked(self, label: str) -> None:
        if self.shutting_down or label in self.tombstones:
            return
        worker = self.workers.get(label)
        if worker is None or worker.done():
            self.workers[label] = asyncio.create_task(self._worker(label))

    async def _acquire_execution_slot(self, job: RuntimeJob) -> bool:
        """等待全局执行许可，同时允许任务在创建进程前取消。"""

        while not job.cancel_event.is_set():
            try:
                await asyncio.wait_for(self.global_sem.acquire(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if job.cancel_event.is_set():
                self.global_sem.release()
                return False
            return True
        return False

    async def _register_spawned_process(
        self,
        label: str,
        job: RuntimeJob,
        process: asyncio.subprocess.Process | None,
    ) -> bool:
        """把已创建进程原子移交给取消流程管理。"""

        async with self.lock:
            if process is not None:
                job.process = process
            may_continue = (
                not self.shutting_down
                and label not in self.tombstones
                and not job.cancel_requested
                and self.running.get(label) is job
            )
            job.status = "starting" if may_continue else "cancelling"
            job.spawn_handoff.set()
            return may_continue

    async def _authorize_job_prompt(self, label: str, job: RuntimeJob) -> bool:
        """在 manager 生命周期锁内提交“允许发送 prompt”的状态转换。"""

        async with self.lock:
            may_send = (
                not self.shutting_down
                and label not in self.tombstones
                and not job.cancel_requested
                and self.running.get(label) is job
                and job.spawn_handoff.is_set()
            )
            if may_send:
                job.prompt_started = True
                job.status = "running"
            else:
                job.status = "cancelling"
            return may_send

    @staticmethod
    def _cancelled_result(session: CodexSession, message: str) -> CodexRunResult:
        return CodexRunResult(
            exit_code=None,
            thread_id=session.thread_id,
            final_text=message,
            stdout_tail="",
            stderr_tail="",
            cancelled=True,
        )

    @staticmethod
    def _failed_execution_result(
        session: CodexSession,
        exc: BaseException,
    ) -> CodexRunResult:
        return CodexRunResult(
            exit_code=1,
            thread_id=session.thread_id,
            final_text=f"Codex 执行异常（{type(exc).__name__}）。",
            stdout_tail="",
            stderr_tail="",
        )

    @staticmethod
    def _mark_job_cancelled(job: RuntimeJob) -> None:
        job.cancel_requested = True
        job.cancel_event.set()
        job.status = "cancelled"
        job.finished_at = time.time()
        job.spawn_handoff.set()
        job.finished_event.set()

    async def _claim_next_job(
        self,
        label: str,
    ) -> tuple[RuntimeJob, CodexSession] | None:
        while True:
            async with self.lock:
                queue = self.queues.setdefault(label, deque())
                if self.shutting_down or label in self.tombstones or not queue:
                    self.workers.pop(label, None)
                    return None
                job = queue.popleft()
                session = self.sessions.get(label)
                if session is None or job.cancel_requested:
                    self._mark_job_cancelled(job)
                    continue
                job.status = "queued"
                self.running[label] = job
                return job, session

    async def _prepare_job_start(
        self,
        label: str,
        session: CodexSession,
        job: RuntimeJob,
    ) -> tuple[_JobStart | None, CodexRunResult | None, bool]:
        await self.refresh_from_settings_reader()
        while True:
            slot_acquired = await self._acquire_execution_slot(job)
            if not slot_acquired:
                return (
                    None,
                    self._cancelled_result(session, "任务在等待执行容量时已取消。"),
                    False,
                )

            try:
                # 等待容量期间配置可能再次变化；拿到许可后必须重新读取同一代策略。
                await self.refresh_from_settings_reader()
                async with self.lock:
                    retry_after_shrink = self.global_sem.over_capacity()
                    may_start = (
                        not retry_after_shrink
                        and not self.shutting_down
                        and label not in self.tombstones
                        and not job.cancel_requested
                        and self.running.get(label) is job
                    )
                    if may_start:
                        job.status = "starting"
                        job.started_at = time.time()
                        execution_config = self.config
                        execution_runner = self.runner
                        job.execution_config = execution_config

                if retry_after_shrink:
                    # 已运行任务沿用旧上限；尚未启动的任务释放旧许可后按新上限重排。
                    self.global_sem.release()
                    continue
                if not may_start:
                    job.cancel_requested = True
                    job.cancel_event.set()
                    return (
                        None,
                        self._cancelled_result(session, "任务在进程启动前已取消。"),
                        True,
                    )
                execution_cwd = normalize_cwd(session.cwd, execution_config)
            except CwdError as exc:
                return (
                    None,
                    CodexRunResult(
                        exit_code=1,
                        thread_id=session.thread_id,
                        final_text=f"Codex 工作目录策略已变更: {exc}",
                        stdout_tail="",
                        stderr_tail="",
                    ),
                    True,
                )
            except BaseException:
                self.global_sem.release()
                raise
            return (
                _JobStart(execution_config, execution_runner, execution_cwd),
                None,
                True,
            )

    async def _execute_job(
        self,
        label: str,
        session: CodexSession,
        job: RuntimeJob,
        artifact_dir: Path,
    ) -> CodexRunResult:
        slot_acquired = False
        try:
            start, result, slot_acquired = await self._prepare_job_start(label, session, job)
            if result is not None:
                return result
            if start is None:
                raise RuntimeError("Codex execution plan is missing")

            logger.info(
                "Codex job started: label=%s job_id=%s cwd_name=%s metadata_keys=%s prompt_chars=%d",
                label,
                job.job_id,
                start.cwd.name,
                sorted(str(key)[:64] for key in job.metadata)[:16],
                len(job.prompt),
            )

            async def process_handoff(
                process: asyncio.subprocess.Process | None,
            ) -> bool:
                return await self._register_spawned_process(label, job, process)

            async def prompt_handoff() -> bool:
                return await self._authorize_job_prompt(label, job)

            run_result = await start.runner.run(
                cwd=start.cwd,
                prompt=job.prompt,
                thread_id=session.thread_id,
                job=job,
                artifact_dir=artifact_dir,
                process_handoff=process_handoff,
                prompt_handoff=prompt_handoff,
            )
            if not isinstance(run_result, CodexRunResult):
                raise TypeError("Codex runner returned an invalid result")
            return run_result
        except asyncio.CancelledError:
            return await self._settle_cancelled_execution(session, job)
        except Exception as exc:  # 单个任务失败不能终止同标签的后续队列。
            logger.error(
                "Codex job execution failed: label=%s job_id=%s error_type=%s",
                label,
                job.job_id,
                type(exc).__name__,
            )
            return self._failed_execution_result(session, exc)
        finally:
            if slot_acquired:
                self.global_sem.release()

    async def _settle_cancelled_execution(
        self,
        session: CodexSession,
        job: RuntimeJob,
    ) -> CodexRunResult:
        job.cancel_requested = True
        job.cancel_event.set()
        process = job.process
        if process is not None:
            try:
                await asyncio.shield(terminate_process_tree(process))
            except Exception as exc:  # 取消收尾仍要返回统一取消结果。
                logger.warning(
                    "Codex worker cancellation cleanup failed: error_type=%s",
                    type(exc).__name__,
                )
        return self._cancelled_result(session, "Codex worker 已取消任务。")

    async def _commit_job_result(
        self,
        label: str,
        job: RuntimeJob,
        result: CodexRunResult,
    ) -> None:
        """在同一 manager 锁内发布运行结果、累计用量并持久化。

        runner 和制品扫描都在锁外完成；这里只提交仍属于该 label 的运行槽，
        防止取消/删除与迟到结果交错后复活已移除任务。
        """

        async with self.lock:
            if self.running.get(label) is job:
                self.running.pop(label, None)
            job.process = None
            job.spawn_handoff.set()
            stored_session = self.sessions.get(label)
            if stored_session is None:
                return
            if result.thread_id:
                stored_session.thread_id = result.thread_id
            stored_session.updated_at = time.time()
            usage = result.usage or {}
            stored_session.input_tokens += _safe_usage_counter(usage, "input_tokens")
            stored_session.cached_input_tokens += _safe_usage_counter(
                usage,
                "cached_input_tokens",
            )
            stored_session.output_tokens += _safe_usage_counter(usage, "output_tokens")
            self._save()

    def _collect_job_artifacts(
        self,
        label: str,
        session: CodexSession,
        job: RuntimeJob,
        result: CodexRunResult,
        artifact_dir: Path,
    ) -> None:
        try:
            if job.status != "cancelled" and label not in self.tombstones:
                collection = collect_image_artifacts(
                    result.final_text,
                    referenced_paths=result.image_paths,
                    cwd=Path(session.cwd),
                    artifact_dir=artifact_dir,
                    session_dir=self._session_dir(label),
                    images_dir=self._session_images_dir(label),
                    job_id=job.job_id,
                    limits=self._artifact_limits(job.execution_config),
                )
                job.image_artifacts = collection.artifacts
                job.artifact_dropped_count = collection.dropped_count
                job.artifact_drop_reasons = collection.reasons
                job.artifact_scan_truncated = collection.scan_truncated
                self._append_artifact_notice(result, job)
        except Exception as exc:  # 制品失败不能阻断文本结果和后续任务。
            logger.error(
                "Codex artifact collection failed: label=%s job_id=%s error_type=%s",
                label,
                job.job_id,
                type(exc).__name__,
            )
            job.artifact_dropped_count += 1
            reason = f"collector_error:{type(exc).__name__}"
            job.artifact_drop_reasons[reason] = job.artifact_drop_reasons.get(reason, 0) + 1
            self._append_artifact_notice(result, job)
        finally:
            self._remove_owned_path(artifact_dir, artifact_dir.parent)

    async def _finalize_job(
        self,
        label: str,
        session: CodexSession,
        job: RuntimeJob,
        result: CodexRunResult,
        artifact_dir: Path,
    ) -> None:
        job.finished_at = time.time()
        cancelled = job.cancel_requested or result.cancelled
        job.status = (
            "cancelled"
            if cancelled
            else "failed"
            if result.timed_out or result.exit_code != 0
            else "done"
        )
        job.result = result
        logger.info(
            "Codex job finished: label=%s job_id=%s status=%s exit_code=%s timed_out=%s",
            label,
            job.job_id,
            job.status,
            result.exit_code,
            result.timed_out,
        )
        try:
            try:
                await self._commit_job_result(label, job, result)
            except Exception as exc:
                logger.error(
                    "Codex job state commit failed: label=%s job_id=%s error_type=%s",
                    label,
                    job.job_id,
                    type(exc).__name__,
                )
            self._collect_job_artifacts(label, session, job, result, artifact_dir)
            if label not in self.tombstones:
                try:
                    self._append_job_history(session, job, result)
                except Exception as exc:
                    logger.error(
                        "Codex job history write failed: label=%s job_id=%s error_type=%s",
                        label,
                        job.job_id,
                        type(exc).__name__,
                    )
        finally:
            job.spawn_handoff.set()
            job.finished_event.set()

    async def _worker(self, label: str) -> None:
        while claimed := await self._claim_next_job(label):
            job, session = claimed
            artifact_dir = self._job_artifact_path(label, job.job_id)
            try:
                artifact_dir = self._job_artifact_dir(label, job.job_id)
                result = await self._execute_job(label, session, job, artifact_dir)
            except Exception as exc:  # claim 后的本地准备失败也必须进入统一终态。
                logger.error(
                    "Codex claimed job preparation failed: label=%s job_id=%s error_type=%s",
                    label,
                    job.job_id,
                    type(exc).__name__,
                )
                result = self._failed_execution_result(session, exc)
            await self._finalize_job(label, session, job, result, artifact_dir)
            if label not in self.tombstones and not self.shutting_down:
                await self._send_job_result(session, job, result)

    def _append_artifact_notice(self, result: CodexRunResult, job: RuntimeJob) -> None:
        config = job.execution_config or self.config
        notices: list[str] = []
        if job.artifact_dropped_count:
            reasons = "; ".join(
                f"{reason}={count}" for reason, count in list(job.artifact_drop_reasons.items())[:3]
            )
            detail = f"（{reasons}）" if reasons else ""
            notices.append(f"拒绝/忽略 {job.artifact_dropped_count} 个图片产物{detail}")
        if job.artifact_scan_truncated:
            notices.append("产物目录扫描达到硬上限，已提前停止")
        unsent = max(0, len(job.image_artifacts) - config.max_qq_images)
        if unsent:
            notices.append(f"另有 {unsent} 张合格图片仅归档、未通过 QQ 投递")
        if not notices:
            return
        notice = "\n\n[Codex 产物审计] " + "；".join(notices)
        available = max(0, config.max_qq_text_chars - len(notice))
        result.final_text = f"{result.final_text[:available].rstrip()}{notice}"

    def _append_job_history(
        self,
        session: CodexSession,
        job: RuntimeJob,
        result: CodexRunResult | None,
    ) -> None:
        if result is None:
            content = ""
            exit_code = None
            thread_id = session.thread_id
            timed_out = False
            cancelled = job.cancel_requested
            stderr_tail = ""
        else:
            content = result.final_text
            exit_code = result.exit_code
            thread_id = result.thread_id
            timed_out = result.timed_out
            cancelled = result.cancelled
            stderr_tail = result.stderr_tail
        self._append_history(
            job.label,
            {
                "type": "message",
                "role": "assistant",
                "job_id": job.job_id,
                "content": content,
                "status": "cancelled" if cancelled else job.status,
                "thread_id": thread_id,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "stderr_tail": stderr_tail,
                "output_limited": bool(result and result.output_limited),
                "output_path": result.output_path if result else None,
                "images": [artifact.as_record() for artifact in job.image_artifacts],
                "artifact_dropped_count": job.artifact_dropped_count,
                "artifact_drop_reasons": job.artifact_drop_reasons,
                "artifact_scan_truncated": job.artifact_scan_truncated,
                "metadata": job.metadata,
            },
        )

    def _result_message_batches(
        self,
        content: str,
        image_artifacts: list[CodexImageArtifact],
        *,
        config: CodexPluginConfig | None = None,
    ) -> list[list[dict[str, Any]]]:
        config = config or self.config
        image_segments = [
            image(artifact.absolute_path) for artifact in image_artifacts[: config.max_qq_images]
        ]
        if not image_segments:
            return [cast(list[dict[str, Any]], segments(content))]

        text_batches = cast(
            list[list[dict[str, Any]]],
            split_message_segments(segments(content)),
        )
        if len(text_batches) == 1:
            return [text_batches[0] + image_segments]
        return text_batches + [image_segments]

    def _metadata_failure_message(self, job: RuntimeJob, *, reason: str, detail: str) -> str | None:
        raw_title = job.metadata.get("failure_title")
        if not isinstance(raw_title, str):
            return None
        title = raw_title.strip()
        if not title or len(title) > 200 or any(ord(char) < 32 for char in title):
            return None
        return f"[codex:{job.label} #{job.job_id}] {title}失败{reason}\n{detail}"

    def _delivery_targets(
        self,
        session: CodexSession,
        job: RuntimeJob,
    ) -> tuple[DeliveryTarget, ...]:
        if job.delivery_targets is not None:
            return job.delivery_targets
        if job.group_id is not None:
            return (DeliveryTarget("group", job.group_id),)
        if job.user_id is not None:
            return (DeliveryTarget("private", job.user_id),)
        if session.target_group_id is not None:
            return (DeliveryTarget("group", session.target_group_id),)
        if session.owner_user_id is not None:
            return (DeliveryTarget("private", session.owner_user_id),)
        return ()

    def _job_result_content(
        self,
        job: RuntimeJob,
        result: CodexRunResult | None,
    ) -> str:
        if result is None:
            return f"[codex:{job.label} #{job.job_id}] 无执行结果。"
        if result.cancelled:
            return f"[codex:{job.label} #{job.job_id}] 已取消。"
        detail = result.final_text.strip()
        if result.timed_out:
            return (
                self._metadata_failure_message(
                    job,
                    reason="：执行超时。",
                    detail=detail,
                )
                or f"[codex:{job.label} #{job.job_id}] 执行超时。\n{detail}"
            )
        if result.exit_code not in (0, None):
            if result.stderr_tail:
                detail = f"{detail}\n\nstderr:\n{result.stderr_tail.strip()}"
            return (
                self._metadata_failure_message(
                    job,
                    reason=f"，退出码 {result.exit_code}。",
                    detail=detail,
                )
                or f"[codex:{job.label} #{job.job_id}] 执行失败，退出码 {result.exit_code}。\n{detail}"
            )
        return f"[codex:{job.label} #{job.job_id}] 完成:\n{detail}"

    async def _send_job_result(
        self,
        session: CodexSession,
        job: RuntimeJob,
        result: CodexRunResult | None,
    ) -> None:
        if job.metadata.get("suppress_delivery"):
            return

        content = self._job_result_content(job, result)

        if result is not None and result.output_path:
            output_name = _public_path_name(result.output_path)
            if output_name not in content:
                content = f"{content}\n\n受控输出文件: {output_name}"

        for target in self._delivery_targets(session, job):
            for batch in self._result_message_batches(
                content,
                job.image_artifacts,
                config=job.execution_config,
            ):
                action = build_action(
                    batch,
                    target.user_id,
                    target.group_id,
                )
                if action and hasattr(job.context, "send_action"):
                    action[ACTION_BYPASS_SINK_KEY] = True
                    try:
                        await job.context.send_action(action)
                    except Exception as exc:  # 一个目标失败不能阻断其他目标和后续队列。
                        logger.error(
                            "Codex result delivery failed: label=%s job_id=%s target_kind=%s "
                            "error_type=%s",
                            job.label,
                            job.job_id,
                            target.kind,
                            type(exc).__name__,
                        )

    async def list_sessions(self) -> str:
        async with self.lock:
            if not self.sessions:
                return "还没有 Codex 会话。用 /codex create <name> 创建。"
            lines = ["Codex 会话列表:"]
            for label in sorted(self.sessions):
                session = self.sessions[label]
                running = self.running.get(label)
                queue_len = len(self.queues.get(label, ()))
                state = f"运行 #{running.job_id}" if running else "空闲"
                if queue_len:
                    state += f"，排队 {queue_len}"
                thread = "已有上下文" if session.thread_id else "未开始"
                token_total = session.input_tokens + session.output_tokens
                age_days = int(max(0, time.time() - session.updated_at) // 86400)
                ttl_warning = (
                    "，⚠️超过建议保留期"
                    if self.config.session_ttl_days and age_days >= self.config.session_ttl_days
                    else ""
                )
                lines.append(
                    f"- {label}: {state}，{thread}，tokens≈{token_total}，"
                    f"闲置{age_days}天{ttl_warning}，cwd={_public_path_name(session.cwd)}"
                )
            return "\n".join(lines)

    async def status(self, label: str | None = None) -> str:
        async with self.lock:
            labels = [label] if label else sorted(self.sessions)
            if not labels:
                return "还没有 Codex 会话。"
        disk_usage = await self._measure_disk_usage_bytes()
        async with self.lock:
            lines: list[str] = []
            for item in labels:
                session = self.sessions.get(item)
                if not session:
                    lines.append(f"Codex 会话不存在: {item}")
                    continue
                running = self.running.get(item)
                queue = self.queues.get(item, deque())
                lines.append(f"`{item}` cwd={_public_path_name(session.cwd)}")
                lines.append(f"上下文: {'已创建' if session.thread_id else '未开始'}")
                lines.append(f"运行中: {'#' + str(running.job_id) if running else '无'}")
                lines.append(f"排队数: {len(queue)}")
                lines.append(
                    f"Token: input={session.input_tokens} "
                    f"cached={session.cached_input_tokens} output={session.output_tokens}"
                )
                lines.append(f"Codex 数据目录: {disk_usage} bytes")
            return "\n".join(lines)

    async def _cancel_runtime_job(self, job: RuntimeJob) -> bool:
        """取消选中的排队或运行任务，并等待生命周期状态收敛。"""

        config = job.execution_config or self.config
        async with self.lock:
            if job.finished_event.is_set():
                return True
            job.cancel_requested = True
            job.cancel_event.set()
            if job.status in {"starting", "running"}:
                job.status = "cancelling"
            process = job.process
            waiting_for_spawn = process is None and not job.spawn_handoff.is_set()

        if waiting_for_spawn:
            try:
                await asyncio.wait_for(
                    job.spawn_handoff.wait(),
                    timeout=config.spawn_timeout_seconds + 1,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Codex spawn handoff did not converge during cancellation: label=%s job_id=%s",
                    job.label,
                    job.job_id,
                )

        async with self.lock:
            process = job.process
        if process is not None:
            try:
                termination = await terminate_process_tree(process)
                if termination is not None and (
                    not getattr(termination, "tree_confirmed", False)
                    or not getattr(termination, "parent_reaped", False)
                ):
                    logger.error(
                        "Codex cancellation could not confirm full process-tree cleanup: "
                        "label=%s job_id=%s result=%s",
                        job.label,
                        job.job_id,
                        termination,
                    )
            except Exception as exc:  # 终止失败后仍需等待工作进程完成收尾。
                logger.warning(
                    "Codex process cancellation failed: label=%s job_id=%s error=%s",
                    job.label,
                    job.job_id,
                    exc,
                )

        try:
            await asyncio.wait_for(
                job.finished_event.wait(),
                timeout=max(10, config.spawn_timeout_seconds + 5),
            )
        except asyncio.TimeoutError:
            return False
        return True

    async def cancel(self, label: str, job_id: int | None = None) -> str:
        async with self.lock:
            if label not in self.sessions:
                return f"Codex 会话不存在: {label}"
            queue = self.queues.get(label, deque())
            if job_id is not None:
                for queued in list(queue):
                    if queued.job_id == job_id:
                        queue.remove(queued)
                        self._mark_job_cancelled(queued)
                        self._append_history(
                            label,
                            {
                                "type": "job.cancelled",
                                "job_id": job_id,
                                "status": "queued",
                            },
                        )
                        return f"已移除排队任务: `{label}` #{job_id}"
            job = self.running.get(label)
            if job_id is not None and (job is None or job.job_id != job_id):
                return f"没有找到任务: `{label}` #{job_id}"
            if job is None:
                return f"`{label}` 当前没有运行中的任务。"

        settled = await self._cancel_runtime_job(job)
        if settled:
            return f"已取消 Codex 任务: `{label}` #{job.job_id}"
        return f"已请求取消 Codex 任务: `{label}` #{job.job_id}，任务仍在收敛中。"

    async def clear_queue(self, label: str) -> str:
        async with self.lock:
            if label not in self.sessions:
                return f"Codex 会话不存在: {label}"
            queue = self.queues.get(label, deque())
            count = len(queue)
            for queued in list(queue):
                self._mark_job_cancelled(queued)
                self._append_history(
                    label,
                    {
                        "type": "job.cancelled",
                        "job_id": queued.job_id,
                        "status": "queued",
                    },
                )
            queue.clear()
            return f"已清空 `{label}` 的排队任务 {count} 个。"

    async def delete_session(
        self,
        label: str,
        *,
        force: bool = False,
        allow_protected: bool = False,
    ) -> str:
        async with self.lock:
            if label not in self.sessions:
                return f"Codex 会话不存在: {label}"
            if self._is_protected_session(label) and not (force and allow_protected):
                return f"`{label}` 是受保护的 Codex 会话，删除需同时使用 --force --protected。"
            running = self.running.get(label)
            if running and not force:
                return f"`{label}` 有任务运行中，先 /codex cancel {label}，或使用 /codex delete {label} --force。"
            queued = len(self.queues.get(label, ()))
            self.tombstones.add(label)
            queue = self.queues.get(label, deque())
            for queued_job in queue:
                self._mark_job_cancelled(queued_job)
            queue.clear()
            worker = self.workers.get(label)

        if running and force:
            await self.cancel(label)

        if worker is not None and worker is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # shield 会阻止 wait_for 自动取消 worker，因此超时分支必须显式取消，
                # 再等待 worker 完成自己的资源收尾。
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        async with self.lock:
            self.sessions.pop(label, None)
            self.queues.pop(label, None)
            self._save()
            self._append_history(
                label,
                {
                    "type": "session.deleted",
                    "label": label,
                    "force": force,
                },
            )
            archive_path = self._archive_session_dir_locked(label)
        suffix = f"\n历史已归档文件夹: {archive_path.name}" if archive_path else ""
        return f"已删除 Codex 会话 `{label}`，同时丢弃排队任务 {queued} 个。{suffix}"

    async def shutdown(self) -> None:
        async with self.lock:
            self.shutting_down = True
            jobs = list(self.running.values())
            tasks = list(self.workers.values())
            for queue in self.queues.values():
                for job in queue:
                    self._mark_job_cancelled(job)
                queue.clear()
        if jobs:
            await asyncio.gather(
                *(self._cancel_runtime_job(job) for job in jobs),
                return_exceptions=True,
            )
        if tasks:
            pending = [task for task in tasks if not task.done()]
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=max(10, self.config.spawn_timeout_seconds + 5),
                    )
                except asyncio.TimeoutError:
                    logger.error("Codex workers did not converge during shutdown")
                    for task in pending:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
        async with self.lock:
            self.workers.clear()
            self.running.clear()
            self._save()


_MANAGER: CodexQueueManager | None = None
_MANAGER_LOCK: asyncio.Lock | None = None


def _manager_lock() -> asyncio.Lock:
    global _MANAGER_LOCK
    if _MANAGER_LOCK is None:
        _MANAGER_LOCK = asyncio.Lock()
    return _MANAGER_LOCK


async def get_manager(context: PluginContextProtocol) -> CodexQueueManager:
    global _MANAGER
    data_dir = Path(getattr(context, "data_dir", Path("data") / "codex")).resolve()
    async with _manager_lock():
        # 在单例发布锁内读取设置；下方版本栅栏拒绝迟到的旧调用，也避免调用者拿着已经过期的
        # 快照排队等待锁，而中间一次重新加载尚未由其他调用发布。
        config, settings_revision = _load_config_generation(context)
        if _MANAGER is not None:
            if _MANAGER.shutting_down:
                raise RuntimeError("Codex manager is shutting down")
            if _MANAGER.data_dir.resolve() != data_dir:
                raise RuntimeError("Codex manager is already bound to a different data directory")
            changed = await _MANAGER.reconfigure(
                context,
                config,
                settings_revision=settings_revision,
            )
            if changed:
                await _MANAGER.maintenance()
            return _MANAGER

        manager = CodexQueueManager(
            context,
            config=config,
            settings_revision=settings_revision,
        )
        try:
            await manager.ensure_default_cwd()
            await manager.maintenance()
        except BaseException:
            await manager.shutdown()
            raise
        _MANAGER = manager
        return manager


async def shutdown_existing_manager() -> bool:
    """仅关闭已经存在的单例，不为 shutdown 额外构造 manager。"""
    global _MANAGER
    async with _manager_lock():
        manager = _MANAGER
        if manager is None:
            return False
        await manager.shutdown()
        if _MANAGER is manager:
            _MANAGER = None
        return True
