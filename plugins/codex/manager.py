from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.plugin_base import build_action, image, load_json, segments, split_message_segments, write_json

from .artifacts import CodexImageArtifact, collect_image_artifacts, default_generated_images_dir
from .config import LABEL_PATTERN, CodexPluginConfig, load_plugin_config
from .paths import CwdError, normalize_cwd
from .runner import CodexRunResult, CodexRunner, terminate_process_tree


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
    process: asyncio.subprocess.Process | None = None
    cancel_requested: bool = False


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
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.runner = runner or CodexRunner(self.config, self.output_dir)
        self.sessions: dict[str, CodexSession] = {}
        self.queues: dict[str, deque[RuntimeJob]] = {}
        self.running: dict[str, RuntimeJob] = {}
        self.workers: dict[str, asyncio.Task] = {}
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

    def _append_history(self, label: str, payload: dict[str, Any]) -> None:
        event = {"ts": time.time(), **payload}
        line = json.dumps(event, ensure_ascii=False)
        with self._conversation_path(label).open("a", encoding="utf-8") as file:
            file.write(line + "\n")

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
            self.sessions[label] = CodexSession(
                label=label,
                cwd=str(cwd),
                owner_user_id=user_id,
                target_group_id=group_id,
            )
            self._save()
            self._append_history(
                label,
                {
                    "type": "session.created",
                    "label": label,
                    "cwd": str(cwd),
                    "owner_user_id": user_id,
                    "target_group_id": group_id,
                },
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
    ) -> str:
        prompt = prompt.strip()
        if not prompt:
            return "任务内容不能为空。"

        async with self.lock:
            session = self.sessions.get(label)
            if not session:
                return f"Codex 会话不存在: {label}\n先用 /codex create {label} 创建。"
            queue = self.queues.setdefault(label, deque())
            queued_count = len(queue)
            if queued_count >= self.config.per_session_queue_limit:
                return f"会话 `{label}` 的队列已满，当前限制为 {self.config.per_session_queue_limit}。"
            session.total_jobs += 1
            session.updated_at = time.time()
            tasks_ahead = queued_count + (1 if label in self.running else 0)
            job = RuntimeJob(
                job_id=session.total_jobs,
                label=label,
                prompt=prompt,
                user_id=user_id,
                group_id=group_id,
                context=context,
            )
            queue.append(job)
            self._save()
            self._append_history(
                label,
                {
                    "type": "message",
                    "role": "user",
                    "job_id": job.job_id,
                    "content": prompt,
                    "user_id": user_id,
                    "group_id": group_id,
                    "status": "queued",
                },
            )
            self._ensure_worker_locked(label)
            if tasks_ahead:
                return f"已加入 Codex 队列: `{label}` #{job.job_id}\n前面还有 {tasks_ahead} 个任务。"
            return f"已收到 Codex 任务: `{label}` #{job.job_id}\n开始后台执行。"

    def _ensure_worker_locked(self, label: str) -> None:
        worker = self.workers.get(label)
        if worker is None or worker.done():
            self.workers[label] = asyncio.create_task(self._worker(label))

    async def _worker(self, label: str) -> None:
        while True:
            async with self.lock:
                queue = self.queues.setdefault(label, deque())
                if not queue:
                    self.workers.pop(label, None)
                    return
                job = queue.popleft()
                session = self.sessions.get(label)
                if session is None:
                    continue
                job.status = "running"
                job.started_at = time.time()
                self.running[label] = job

            result: CodexRunResult | None = None
            artifact_dir = self._job_artifact_dir(label, job.job_id)
            try:
                async with self.global_sem:
                    result = await self.runner.run(
                        cwd=Path(session.cwd),
                        prompt=job.prompt,
                        thread_id=session.thread_id,
                        job=job,
                        artifact_dir=artifact_dir,
                    )
            except Exception as exc:  # noqa: BLE001 - keep bot task alive and report failure.
                result = CodexRunResult(
                    exit_code=None,
                    thread_id=session.thread_id,
                    final_text=f"Codex 执行异常: {exc}",
                    stdout_tail="",
                    stderr_tail="",
                )
            finally:
                job.finished_at = time.time()
                job.status = "cancelled" if job.cancel_requested else "done"
                job.result = result
                async with self.lock:
                    current = self.running.get(label)
                    if current is job:
                        self.running.pop(label, None)
                    if result and result.thread_id and label in self.sessions:
                        self.sessions[label].thread_id = result.thread_id
                        self.sessions[label].updated_at = time.time()
                        self._save()
                if result is not None:
                    job.image_artifacts = collect_image_artifacts(
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
                    )
                self._append_job_history(session, job, result)

            await self._send_job_result(session, job, result)

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
                "images": [artifact.as_record() for artifact in job.image_artifacts],
            },
        )

    def _result_message_batches(
        self,
        content: str,
        image_artifacts: list[CodexImageArtifact],
    ) -> list[list[dict[str, Any]]]:
        image_segments = [image(artifact.absolute_path) for artifact in image_artifacts]
        if not image_segments:
            return [segments(content)]

        text_batches = split_message_segments(segments(content))
        if len(text_batches) == 1:
            return [text_batches[0] + image_segments]
        return text_batches + [image_segments]

    async def _send_job_result(
        self,
        session: CodexSession,
        job: RuntimeJob,
        result: CodexRunResult | None,
    ) -> None:
        if result is None:
            content = f"[codex:{job.label} #{job.job_id}] 无执行结果。"
        elif result.cancelled:
            content = f"[codex:{job.label} #{job.job_id}] 已取消。"
        elif result.timed_out:
            content = f"[codex:{job.label} #{job.job_id}] 执行超时。\n{result.final_text.strip()}"
        elif result.exit_code not in (0, None):
            detail = result.final_text.strip()
            if result.stderr_tail:
                detail = f"{detail}\n\nstderr:\n{result.stderr_tail.strip()}"
            content = f"[codex:{job.label} #{job.job_id}] 执行失败，退出码 {result.exit_code}。\n{detail}"
        else:
            content = f"[codex:{job.label} #{job.job_id}] 完成:\n{result.final_text.strip()}"

        for batch in self._result_message_batches(content, job.image_artifacts):
            action = build_action(
                batch,
                job.user_id or session.owner_user_id,
                job.group_id or session.target_group_id,
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
                lines.append(f"- {label}: {state}，{thread}，cwd={session.cwd}")
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
            return "\n".join(lines)

    async def cancel(self, label: str, job_id: int | None = None) -> str:
        async with self.lock:
            if label not in self.sessions:
                return f"Codex 会话不存在: {label}"
            queue = self.queues.get(label, deque())
            if job_id is not None:
                for queued in list(queue):
                    if queued.job_id == job_id:
                        queue.remove(queued)
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
            job.cancel_requested = True
            process = job.process

        if process is not None:
            await terminate_process_tree(process)
            return f"已请求取消 Codex 任务: `{label}` #{job.job_id}"
        return f"任务尚未启动进程，已标记取消: `{label}` #{job.job_id}"

    async def clear_queue(self, label: str) -> str:
        async with self.lock:
            if label not in self.sessions:
                return f"Codex 会话不存在: {label}"
            queue = self.queues.get(label, deque())
            count = len(queue)
            for queued in list(queue):
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

    async def delete_session(self, label: str, *, force: bool = False) -> str:
        async with self.lock:
            if label not in self.sessions:
                return f"Codex 会话不存在: {label}"
            running = self.running.get(label)
            if running and not force:
                return f"`{label}` 有任务运行中，先 /codex cancel {label}，或使用 /codex delete {label} --force。"
            queued = len(self.queues.get(label, ()))

        if running and force:
            await self.cancel(label)

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
        return f"已删除 Codex 会话 `{label}`，同时丢弃排队任务 {queued} 个。"

    async def shutdown(self) -> None:
        async with self.lock:
            jobs = list(self.running.values())
            tasks = list(self.workers.values())
        for job in jobs:
            job.cancel_requested = True
            if job.process is not None:
                await terminate_process_tree(job.process)
        for task in tasks:
            task.cancel()

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
    return _MANAGER


def reset_manager_for_tests() -> None:
    global _MANAGER
    _MANAGER = None
