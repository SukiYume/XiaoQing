from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.plugin_base import (
    build_action,
    image,
    load_json,
    segments,
    split_message_segments,
    write_json,
)

from .artifacts import (
    ArtifactLimits,
    CodexImageArtifact,
    collect_image_artifacts,
    default_generated_images_dir,
)
from .config import LABEL_PATTERN, CodexPluginConfig, load_plugin_config
from .paths import CwdError, normalize_cwd
from .runner import CodexRunner, CodexRunResult, terminate_process_tree

logger = logging.getLogger(__name__)


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
    context: Any
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


class CodexQueueManager:
    def __init__(
        self,
        context: Any,
        *,
        config: CodexPluginConfig | None = None,
        runner: CodexRunner | None = None,
    ) -> None:
        self.context = context
        self.config = config or load_plugin_config(context)
        self.data_dir = Path(getattr(context, "data_dir", Path("data") / "codex"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_path = self.data_dir / "sessions.json"
        self.output_dir = self.data_dir / "outputs"
        self.session_root = self.data_dir / "session"
        self.deleted_session_root = self.data_dir / "deleted_sessions"
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.runner = runner or CodexRunner(self.config, self.output_dir)
        self.sessions: dict[str, CodexSession] = {}
        self.queues: dict[str, deque[RuntimeJob]] = {}
        self.running: dict[str, RuntimeJob] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.tombstones: set[str] = set()
        self.shutting_down = False
        self.lock = asyncio.Lock()
        self.global_sem = asyncio.Semaphore(self.config.max_parallel_jobs)
        self._load()

    def _load(self) -> None:
        data = load_json(self.sessions_path, default={})
        raw_sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        self.sessions = {
            label: CodexSession(**session_data)
            for label, session_data in raw_sessions.items()
            if isinstance(session_data, dict)
        }

    def _save(self) -> None:
        payload = {"sessions": {label: asdict(session) for label, session in self.sessions.items()}}
        write_json(self.sessions_path, payload)

    def _session_dir(self, label: str) -> Path:
        path = self.session_root / label
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _archive_session_dir_locked(self, label: str) -> Path | None:
        session_dir = self.session_root / label
        if not session_dir.exists():
            return None

        self.deleted_session_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
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

    def _job_artifact_dir(self, label: str, job_id: int) -> Path:
        path = self._session_dir(label) / "jobs" / f"job-{job_id:04d}" / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _generated_images_dir(self) -> Path:
        return default_generated_images_dir()

    def _artifact_limits(self) -> ArtifactLimits:
        return ArtifactLimits(
            scan_max_entries=self.config.artifact_scan_max_entries,
            scan_max_depth=self.config.artifact_scan_max_depth,
            max_artifacts=self.config.max_image_artifacts,
            max_single_bytes=self.config.max_image_bytes,
            max_total_bytes=self.config.max_image_total_bytes,
            max_pixels=self.config.max_image_pixels,
            max_frames=self.config.max_image_frames,
        )

    def _disk_usage_bytes(self) -> int:
        total = 0
        for item in self.data_dir.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    async def maintenance(self) -> None:
        retention_days = self.config.artifact_retention_days
        if retention_days <= 0:
            return
        cutoff = time.time() - retention_days * 86400
        async with self.lock:
            for artifacts in self.session_root.glob("*/jobs/*/artifacts"):
                try:
                    if artifacts.is_dir() and artifacts.stat().st_mtime < cutoff:
                        shutil.rmtree(artifacts)
                except OSError:
                    logger.warning("Failed to prune Codex artifacts path=%s", artifacts)

    def _append_history(self, label: str, payload: dict[str, Any]) -> None:
        event = {"ts": time.time(), **payload}
        line = json.dumps(event, ensure_ascii=False)
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
        context: Any,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[RuntimeJob, int]:
        queue = self.queues.setdefault(session.label, deque())
        job_metadata = dict(metadata or {})
        queued_count = sum(1 for queued in queue if not queued.metadata.get("queue_overhead"))
        if queued_count >= self.config.emergency_queue_limit:
            raise RuntimeError(
                f"会话 `{session.label}` 达到进程保护紧急队列上限 "
                f"{self.config.emergency_queue_limit}。"
            )
        if self._disk_usage_bytes() >= self.config.emergency_disk_bytes:
            raise RuntimeError("Codex 数据目录达到紧急磁盘保护阈值，请清理制品或提高配置。")
        if queued_count >= self.config.per_session_queue_limit:
            job_metadata["soft_limit_warning"] = True
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
            metadata=job_metadata,
        )
        queue.append(job)
        logger.info(
            "Codex job queued: label=%s job_id=%s queue_ahead=%s metadata=%s prompt_chars=%d",
            session.label,
            job.job_id,
            tasks_ahead,
            job.metadata,
            len(prompt),
        )
        self._save()
        self._append_history(
            session.label,
            {
                "type": "message",
                "role": "user",
                "job_id": job.job_id,
                "content": prompt,
                "user_id": user_id,
                "group_id": group_id,
                "status": "queued",
                "metadata": job.metadata,
            },
        )
        self._ensure_worker_locked(session.label)
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
        if not re.match(LABEL_PATTERN, label):
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
        return f"已创建 Codex 会话 `{label}`\n工作目录: {cwd}"

    async def enqueue(
        self,
        label: str,
        prompt: str,
        *,
        user_id: int | None,
        group_id: int | None,
        context: Any,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        prompt = prompt.strip()
        if not prompt:
            return "任务内容不能为空。"

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
                )
            except RuntimeError as exc:
                return str(exc)
            if tasks_ahead:
                warning = (
                    f"\n⚠️ 已超过软队列提示值 {self.config.per_session_queue_limit}，"
                    "管理员任务仍会照常执行。"
                    if job.metadata.get("soft_limit_warning")
                    else ""
                )
                return (
                    f"已加入 Codex 队列: `{label}` #{job.job_id}\n"
                    f"前面还有 {tasks_ahead} 个任务。{warning}"
                )
            return f"已收到 Codex 任务: `{label}` #{job.job_id}\n开始后台执行。"

    def _ensure_worker_locked(self, label: str) -> None:
        if self.shutting_down or label in self.tombstones:
            return
        worker = self.workers.get(label)
        if worker is None or worker.done():
            self.workers[label] = asyncio.create_task(self._worker(label))

    async def _acquire_execution_slot(self, job: RuntimeJob) -> bool:
        """Wait for global capacity while allowing pre-spawn cancellation."""

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
        """Atomically hand a spawned process to the cancellation authority."""

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
        """Commit the prompt-send transition under the manager lifecycle lock."""

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

    async def _worker(self, label: str) -> None:
        while True:
            async with self.lock:
                queue = self.queues.setdefault(label, deque())
                if self.shutting_down or label in self.tombstones or not queue:
                    self.workers.pop(label, None)
                    return
                job = queue.popleft()
                session = self.sessions.get(label)
                if session is None or job.cancel_requested:
                    job.cancel_requested = True
                    job.cancel_event.set()
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    job.spawn_handoff.set()
                    job.finished_event.set()
                    continue
                job.status = "queued"
                self.running[label] = job

            result: CodexRunResult | None = None
            artifact_dir = self._job_artifact_dir(label, job.job_id)
            slot_acquired = False
            try:
                slot_acquired = await self._acquire_execution_slot(job)
                if not slot_acquired:
                    result = self._cancelled_result(session, "任务在等待执行容量时已取消。")
                else:
                    async with self.lock:
                        may_start = (
                            not self.shutting_down
                            and label not in self.tombstones
                            and not job.cancel_requested
                            and self.running.get(label) is job
                        )
                        if may_start:
                            job.status = "starting"
                            job.started_at = time.time()
                    if not may_start:
                        job.cancel_requested = True
                        job.cancel_event.set()
                        result = self._cancelled_result(
                            session,
                            "任务在进程启动前已取消。",
                        )
                    else:
                        logger.info(
                            "Codex job started: label=%s job_id=%s cwd=%s metadata=%s prompt_chars=%d",
                            label,
                            job.job_id,
                            session.cwd,
                            job.metadata,
                            len(job.prompt),
                        )

                        async def process_handoff(
                            process: asyncio.subprocess.Process | None,
                            *,
                            _job: RuntimeJob = job,
                            _label: str = label,
                        ) -> bool:
                            return await self._register_spawned_process(_label, _job, process)

                        async def prompt_handoff(
                            *,
                            _job: RuntimeJob = job,
                            _label: str = label,
                        ) -> bool:
                            return await self._authorize_job_prompt(_label, _job)

                        result = await self.runner.run(
                            cwd=Path(session.cwd),
                            prompt=job.prompt,
                            thread_id=session.thread_id,
                            job=job,
                            artifact_dir=artifact_dir,
                            process_handoff=process_handoff,
                            prompt_handoff=prompt_handoff,
                        )
            except asyncio.CancelledError:
                job.cancel_requested = True
                job.cancel_event.set()
                process = job.process
                if process is not None:
                    try:
                        await asyncio.shield(terminate_process_tree(process))
                    except Exception as exc:  # noqa: BLE001 - shutdown must still finalize.
                        logger.warning("Codex worker cancellation cleanup failed: %s", exc)
                result = self._cancelled_result(session, "Codex worker 已取消任务。")
            except Exception as exc:  # noqa: BLE001 - keep bot task alive and report failure.
                result = CodexRunResult(
                    exit_code=1,
                    thread_id=session.thread_id,
                    final_text=f"Codex 执行异常: {exc}",
                    stdout_tail="",
                    stderr_tail="",
                )
            finally:
                if slot_acquired:
                    self.global_sem.release()
                if result is None:
                    result = (
                        self._cancelled_result(session, "Codex 任务已取消。")
                        if job.cancel_requested
                        else CodexRunResult(
                            exit_code=1,
                            thread_id=session.thread_id,
                            final_text="Codex worker 未返回执行结果。",
                            stdout_tail="",
                            stderr_tail="",
                        )
                    )
                job.finished_at = time.time()
                cancelled = job.cancel_requested or result.cancelled
                if cancelled:
                    job.status = "cancelled"
                elif result.timed_out or result.exit_code != 0:
                    job.status = "failed"
                else:
                    job.status = "done"
                job.result = result
                logger.info(
                    "Codex job finished: label=%s job_id=%s status=%s exit_code=%s timed_out=%s",
                    label,
                    job.job_id,
                    job.status,
                    result.exit_code if result else None,
                    result.timed_out if result else False,
                )
                try:
                    async with self.lock:
                        current = self.running.get(label)
                        if current is job:
                            self.running.pop(label, None)
                        job.process = None
                        job.spawn_handoff.set()
                        if result and label in self.sessions:
                            if result.thread_id:
                                self.sessions[label].thread_id = result.thread_id
                            self.sessions[label].updated_at = time.time()
                            usage = result.usage or {}
                            self.sessions[label].input_tokens += int(
                                usage.get("input_tokens", 0) or 0
                            )
                            self.sessions[label].cached_input_tokens += int(
                                usage.get("cached_input_tokens", 0) or 0
                            )
                            self.sessions[label].output_tokens += int(
                                usage.get("output_tokens", 0) or 0
                            )
                            self._save()
                    try:
                        if not cancelled and label not in self.tombstones:
                            collection = collect_image_artifacts(
                                result.final_text,
                                referenced_paths=result.image_paths,
                                cwd=Path(session.cwd),
                                artifact_dir=artifact_dir,
                                session_dir=self._session_dir(label),
                                images_dir=self._session_images_dir(label),
                                job_id=job.job_id,
                                generated_images_dir=self._generated_images_dir(),
                                started_at=job.started_at,
                                finished_at=job.finished_at,
                                limits=self._artifact_limits(),
                            )
                            job.image_artifacts = collection.artifacts
                            job.artifact_dropped_count = collection.dropped_count
                            job.artifact_drop_reasons = collection.reasons
                            job.artifact_scan_truncated = collection.scan_truncated
                            self._append_artifact_notice(result, job)
                    except Exception as exc:  # noqa: BLE001 - result delivery must survive.
                        logger.exception(
                            "Codex artifact collection failed: label=%s job_id=%s",
                            label,
                            job.job_id,
                        )
                        job.artifact_dropped_count += 1
                        reason = f"collector_error:{type(exc).__name__}"
                        job.artifact_drop_reasons[reason] = (
                            job.artifact_drop_reasons.get(reason, 0) + 1
                        )
                        self._append_artifact_notice(result, job)
                    finally:
                        try:
                            shutil.rmtree(artifact_dir)
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            logger.warning(
                                "Failed to remove raw Codex artifact directory %s: %s",
                                artifact_dir,
                                exc,
                            )
                    if label not in self.tombstones:
                        self._append_job_history(session, job, result)
                finally:
                    job.spawn_handoff.set()
                    job.finished_event.set()

            if label not in self.tombstones and not self.shutting_down:
                await self._send_job_result(session, job, result)

    def _append_artifact_notice(self, result: CodexRunResult, job: RuntimeJob) -> None:
        notices: list[str] = []
        if job.artifact_dropped_count:
            reasons = "; ".join(
                f"{reason}={count}" for reason, count in list(job.artifact_drop_reasons.items())[:3]
            )
            detail = f"（{reasons}）" if reasons else ""
            notices.append(f"拒绝/忽略 {job.artifact_dropped_count} 个图片产物{detail}")
        if job.artifact_scan_truncated:
            notices.append("产物目录扫描达到硬上限，已提前停止")
        unsent = max(0, len(job.image_artifacts) - self.config.max_qq_images)
        if unsent:
            notices.append(f"另有 {unsent} 张合格图片仅归档、未通过 QQ 投递")
        if not notices:
            return
        notice = "\n\n[Codex 产物审计] " + "；".join(notices)
        available = max(0, self.config.max_qq_text_chars - len(notice))
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
    ) -> list[list[dict[str, Any]]]:
        image_segments = [
            image(artifact.absolute_path)
            for artifact in image_artifacts[: self.config.max_qq_images]
        ]
        if not image_segments:
            return [segments(content)]

        text_batches = split_message_segments(segments(content))
        if len(text_batches) == 1:
            return [text_batches[0] + image_segments]
        return text_batches + [image_segments]

    def _metadata_failure_message(self, job: RuntimeJob, *, reason: str, detail: str) -> str | None:
        title = str(job.metadata.get("failure_title") or "").strip()
        if not title:
            return None
        return f"[codex:{job.label} #{job.job_id}] {title}失败{reason}\n{detail}"

    def _delivery_target(
        self,
        session: CodexSession,
        job: RuntimeJob,
    ) -> tuple[int | None, int | None]:
        if job.group_id is not None:
            return job.user_id, job.group_id
        if job.user_id is not None:
            return job.user_id, None
        return session.owner_user_id, session.target_group_id

    async def _send_job_result(
        self,
        session: CodexSession,
        job: RuntimeJob,
        result: CodexRunResult | None,
    ) -> None:
        if job.metadata.get("suppress_delivery"):
            return

        if result is None:
            content = f"[codex:{job.label} #{job.job_id}] 无执行结果。"
        elif result.cancelled:
            content = f"[codex:{job.label} #{job.job_id}] 已取消。"
        elif result.timed_out:
            detail = result.final_text.strip()
            content = (
                self._metadata_failure_message(
                    job,
                    reason="：执行超时。",
                    detail=detail,
                )
                or f"[codex:{job.label} #{job.job_id}] 执行超时。\n{detail}"
            )
        elif result.exit_code not in (0, None):
            detail = result.final_text.strip()
            if result.stderr_tail:
                detail = f"{detail}\n\nstderr:\n{result.stderr_tail.strip()}"
            content = (
                self._metadata_failure_message(
                    job,
                    reason=f"，退出码 {result.exit_code}。",
                    detail=detail,
                )
                or f"[codex:{job.label} #{job.job_id}] 执行失败，退出码 {result.exit_code}。\n{detail}"
            )
        else:
            final_text = result.final_text.strip()
            content = f"[codex:{job.label} #{job.job_id}] 完成:\n{final_text}"

        if result is not None and result.output_path and result.output_path not in content:
            content = f"{content}\n\n受控输出路径: {result.output_path}"

        user_id, group_id = self._delivery_target(session, job)
        for batch in self._result_message_batches(content, job.image_artifacts):
            action = build_action(
                batch,
                user_id,
                group_id,
            )
            if action and hasattr(job.context, "send_action"):
                action["_bypass_sink"] = True
                await job.context.send_action(action)

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
                    f"闲置{age_days}天{ttl_warning}，cwd={session.cwd}"
                )
            return "\n".join(lines)

    async def status(self, label: str | None = None) -> str:
        async with self.lock:
            labels = [label] if label else sorted(self.sessions)
            if not labels:
                return "还没有 Codex 会话。"
            lines: list[str] = []
            for item in labels:
                session = self.sessions.get(item)
                if not session:
                    lines.append(f"Codex 会话不存在: {item}")
                    continue
                running = self.running.get(item)
                queue = self.queues.get(item, deque())
                lines.append(f"`{item}` cwd={session.cwd}")
                lines.append(f"上下文: {'已创建' if session.thread_id else '未开始'}")
                lines.append(f"运行中: {'#' + str(running.job_id) if running else '无'}")
                lines.append(f"排队数: {len(queue)}")
                lines.append(
                    f"Token: input={session.input_tokens} "
                    f"cached={session.cached_input_tokens} output={session.output_tokens}"
                )
                lines.append(f"Codex 数据目录: {self._disk_usage_bytes()} bytes")
            return "\n".join(lines)

    async def _cancel_runtime_job(self, job: RuntimeJob) -> bool:
        """Cancel one selected/running job and wait for lifecycle convergence."""

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
                    timeout=self.config.spawn_timeout_seconds + 1,
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
            except Exception as exc:  # noqa: BLE001 - still wait for worker finalization.
                logger.warning(
                    "Codex process cancellation failed: label=%s job_id=%s error=%s",
                    job.label,
                    job.job_id,
                    exc,
                )

        try:
            await asyncio.wait_for(
                job.finished_event.wait(),
                timeout=max(10, self.config.spawn_timeout_seconds + 5),
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
                        queued.cancel_requested = True
                        queued.cancel_event.set()
                        queued.status = "cancelled"
                        queued.finished_at = time.time()
                        queued.spawn_handoff.set()
                        queued.finished_event.set()
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
                queued.cancel_requested = True
                queued.cancel_event.set()
                queued.status = "cancelled"
                queued.finished_at = time.time()
                queued.spawn_handoff.set()
                queued.finished_event.set()
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
                queued_job.cancel_requested = True
                queued_job.cancel_event.set()
                queued_job.status = "cancelled"
                queued_job.finished_at = time.time()
                queued_job.spawn_handoff.set()
                queued_job.finished_event.set()
            queue.clear()
            worker = self.workers.get(label)

        if running and force:
            await self.cancel(label)

        if worker is not None and worker is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
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
        suffix = f"\n历史已归档: {archive_path}" if archive_path else ""
        return f"已删除 Codex 会话 `{label}`，同时丢弃排队任务 {queued} 个。{suffix}"

    async def shutdown(self) -> None:
        async with self.lock:
            self.shutting_down = True
            jobs = list(self.running.values())
            tasks = list(self.workers.values())
            for queue in self.queues.values():
                for job in queue:
                    job.cancel_requested = True
                    job.cancel_event.set()
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    job.spawn_handoff.set()
                    job.finished_event.set()
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

    async def wait_idle(self) -> None:
        while True:
            async with self.lock:
                tasks = [task for task in self.workers.values() if not task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)


_MANAGER: CodexQueueManager | None = None


async def get_manager(context: Any) -> CodexQueueManager:
    global _MANAGER
    data_dir = Path(getattr(context, "data_dir", Path("data") / "codex"))
    if _MANAGER is None or _MANAGER.data_dir != data_dir:
        _MANAGER = CodexQueueManager(context)
        await _MANAGER.ensure_default_cwd()
        await _MANAGER.maintenance()
    return _MANAGER


def reset_manager_for_tests() -> None:
    global _MANAGER
    _MANAGER = None
