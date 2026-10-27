"""agent 任务的数据模型、会话身份与按会话持久化的任务记录。"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.plugin_base import atomic_write_text

from .receipts import HeldReceipt

# 每个会话保留的任务记录数，以及全局保留的完整执行记录数。
MAX_RECORDS_PER_CHAT = 30
MAX_TRANSCRIPTS      = 200
_CHAT_KEY_RE         = re.compile(r"^[gu][0-9]{1,20}$")

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})


@dataclass(frozen=True, slots=True)
class ChatRef:
    """任务所属的 QQ 会话与发起人。

    ``key`` 是会话：群聊按群，私聊按用户。同一个群共享工作目录、任务记录和 Codex 会话，
    运行中的任务群里都能查看；``user_id`` 是发起人，只有发起人和 Bot 管理员能停止或补充。
    """

    user_id: int
    group_id: int | None

    @property
    def key(self) -> str:
        return f"g{self.group_id}" if self.group_id is not None else f"u{self.user_id}"

    @property
    def is_private(self) -> bool:
        return self.group_id is None


@dataclass(frozen=True, slots=True)
class Actor:
    """发出这条请求的人。运行中的任务只有发起人和 Bot 管理员能停止或补充。"""

    user_id: int
    admin: bool = False

    def manages(self, task: AgentTask) -> bool:
        return self.admin or task.chat.user_id == self.user_id


@dataclass(slots=True)
class Outcome:
    """一次任务交付给发起人的内容。"""

    text: str
    files: list[str] = field(default_factory=list)
    status: str = "done"


@dataclass(eq=False)
class AgentTask:
    task_id: str
    chat: ChatRef
    goal: str
    request_text: str
    source: str
    created_at: float = field(default_factory=time.time)
    status: str   = "running"
    steps: int    = 0
    activity: str = "准备中"
    plan: list[dict[str, str]] = field(default_factory=list)
    finished_at: float | None = None
    outcome: Outcome | None   = None
    error: str                = ""
    runner: asyncio.Task[None] | None = field(default=None, repr=False)
    inline_waiter: asyncio.Future[Outcome] | None = field(default=None, repr=False)
    progress_sent: int      = 0
    last_progress_at: float = 0.0
    # 发起这条任务的消息附带的引用文字与已下载到本地的图片。
    quoted_text: str = ""
    image_files: list[str] = field(default_factory=list)
    sender: str = ""
    # 聊天插件交来的人设与说话方式，只用于写回复的口吻，不写入任务记录。
    persona: str = field(default="", repr=False)
    # 任务开始时发起人是不是 Bot 管理员：决定占哪份名额、提示词怎么写；本机工具另由 Core 每次复核。
    admin: bool = False
    # 运行中发起人追加的要求，执行循环在下一步前读走。
    notes: list[str] = field(default_factory=list)
    # 结果交付：inline 随当轮回复交给聊天，sent 主动发出，failed 发送失败。
    delivery: str = ""
    # 调用命令带回的发送回执，随附件实际发出时结算（见 receipts.py）。
    held_receipts: list[HeldReceipt] = field(default_factory=list, repr=False)
    # 当轮回复里内嵌的图片，等聊天侧回报发没发出去时一起结算。
    inline_files: list[str] = field(default_factory=list, repr=False)
    # 保留有界命令回报供追问定位记录；用户回复按人设自然表达。
    command_results: list[str] = field(default_factory=list, repr=False)

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.created_at)

    def record(self) -> dict[str, Any]:
        outcome = self.outcome
        return {
            "task_id": self.task_id,
            "chat": self.chat.key,
            "user_id": self.chat.user_id,
            "group_id": self.chat.group_id,
            "sender": self.sender,
            "goal": self.goal,
            "request_text": self.request_text[:500],
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "steps": self.steps,
            # 结果原样保存：/agent result 补发的必须和当时要发的完全一样。
            "result": outcome.text if outcome is not None else "",
            "files": list(outcome.files) if outcome is not None else [],
            "error": self.error[:500],
            "delivery": self.delivery,
            "command_results": list(self.command_results),
        }


def new_task_id() -> str:
    return secrets.token_hex(3)


def record_initiator(record: dict[str, Any]) -> str:
    """群聊任务的发起人；私聊只有一个人，不必标出。"""

    if record.get("group_id") is None:
        return ""
    name = str(record.get("sender") or "").strip()
    user = record.get("user_id")
    if name and user is not None:
        return f"{name}({user})"
    return name or (str(user) if user is not None else "")


def describe_record(
    record: dict[str, Any],
    *,
    result_chars: int = 160,
    full_paths: bool  = True,
) -> str:
    """把一条任务记录压成一行；给模型看完整路径便于续做，给人看只留文件名。"""

    status = str(record.get("status") or "?")
    if record.get("delivery") == "failed":
        status += "（结果没发出去）"
    goal      = str(record.get("goal") or "").strip().replace("\n", " ")
    result    = str(record.get("result") or record.get("error") or "").strip().replace("\n", " ")
    initiator = record_initiator(record)
    line      = f"[{record.get('task_id', '?')}] {status} · "
    if initiator:
        line += f"{initiator} 发起 · "
    line += goal[:120]
    if result:
        line += f" → {result[:result_chars]}"
    files = [str(item) for item in record.get("files") or [] if isinstance(item, str)]
    if files:
        shown = files[:5] if full_paths else [Path(item).name for item in files[:5]]
        line += f"（文件: {', '.join(shown)}）"
    return line


class TaskStore:
    """按会话保存任务记录；执行记录单独成文件，便于排查一次任务的全过程。"""

    def __init__(self, data_dir: Path) -> None:
        self.root            = data_dir / "tasks"
        self.transcript_root = data_dir / "transcripts"
        self._lock           = asyncio.Lock()

    def _path(self, chat_key: str) -> Path:
        if not _CHAT_KEY_RE.fullmatch(chat_key):
            raise ValueError("invalid chat key")
        return self.root / f"{chat_key}.json"

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []
        records = data.get("tasks") if isinstance(data, dict) else None
        return [item for item in records or [] if isinstance(item, dict)]

    @staticmethod
    def _write(path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "tasks": records[-MAX_RECORDS_PER_CHAT:]}
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    async def recent(self, chat_key: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        path = self._path(chat_key)
        async with self._lock:
            records = await asyncio.to_thread(self._read, path)
        return records[-limit:]

    async def save(self, task: AgentTask) -> None:
        path   = self._path(task.chat.key)
        record = task.record()

        def update() -> None:
            records = [item for item in self._read(path) if item.get("task_id") != task.task_id]
            records.append(record)
            self._write(path, records)

        async with self._lock:
            await asyncio.to_thread(update)

    async def update_delivery(self, chat_key: str, task_id: str, delivery: str) -> bool:
        """改写一条已结束任务的交付状态（补发、当轮回复的回执）；找不到记录时返回 False。"""

        path = self._path(chat_key)

        def update() -> bool:
            records = self._read(path)
            for record in records:
                if record.get("task_id") == task_id:
                    record["delivery"] = delivery
                    self._write(path, records)
                    return True
            return False

        async with self._lock:
            return await asyncio.to_thread(update)

    async def mark_interrupted(self) -> int:
        """进程重启后，上一代没有收尾的任务一律记为中断。"""

        def sweep() -> int:
            if not self.root.is_dir():
                return 0
            changed = 0
            for path in sorted(self.root.glob("*.json")):
                records = self._read(path)
                dirty   = False
                for record in records:
                    if record.get("status") not in TERMINAL_STATUSES:
                        record["status"] = "interrupted"
                        record["error"]  = "进程重启时任务尚未完成"
                        dirty            = True
                        changed += 1
                if dirty:
                    self._write(path, records)
            return changed

        async with self._lock:
            return await asyncio.to_thread(sweep)

    async def save_transcript(self, task: AgentTask, messages: list[dict[str, Any]]) -> None:
        payload = {"task": task.record(), "messages": messages}
        path    = self.transcript_root / f"{task.task_id}.json"

        def write() -> None:
            self.transcript_root.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
            existing = sorted(
                self.transcript_root.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
            )
            for stale in existing[:-MAX_TRANSCRIPTS]:
                stale.unlink(missing_ok=True)

        await asyncio.to_thread(write)
