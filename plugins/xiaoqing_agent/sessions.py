"""发起人和小青的办事会话：同一个人在同一个会话里接着说，就接在同一份消息记录后面。

一个会话是一条系统提示加若干轮：每轮从发起人的一句话开始，到这次任务结束为止，包括模型的调用和
工具的回报。下一句话接在最后一轮后面继续跑，前面的结果、ID 和服务说明都还在，不用从任务记录里重新拼。
隔了 ``idle_seconds`` 没说话就开新会话；轮数和总字数超过上限时从最早的一轮开始丢。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from core.plugin_base import atomic_write_text

from .tasks import CHAT_KEY_RE, ChatRef, run_locked

logger = logging.getLogger(__name__)
_T     = TypeVar("_T")

# 整个会话（全部轮次的消息）保存的字符上限；执行时更早的工具输出另由上下文压缩处理。
_MAX_SESSION_CHARS = 200_000
_STOPPED_RESULT    = "（任务在这一步停下了，没有结果。）"


@dataclass(slots=True)
class Session:
    system    : dict[str, Any]
    session_id: str = field(default_factory=lambda: secrets.token_hex(4))
    turns     : list[dict[str, Any]] = field(default_factory=list)
    plugins   : list[str] = field(default_factory=list)
    updated_at: float = 0.0

    def messages(self) -> list[dict[str, Any]]:
        return [self.system, *(message for turn in self.turns for message in turn["messages"])]

    @property
    def last_task_id(self) -> str:
        return str(self.turns[-1].get("task_id") or "") if self.turns else ""


def settle_turn(messages: list[dict[str, Any]], reply: str) -> list[dict[str, Any]]:
    """一轮结束时补齐消息结构，下次接着跑时接口能接受。

    中途停下的调用补一条“没有结果”的工具回报；最后不是模型的文字回复时（超时、出错、被停止），
    把实际发给发起人的话补成一条模型回复，下一轮知道上次说到了哪里。
    """

    settled: list[dict[str, Any]] = []
    pending: list[str]            = []
    for message in messages:
        if message.get("role") != "tool" and pending:
            settled.extend(_stopped_results(pending))
            pending = []
        if message.get("role") == "assistant":
            pending = [
                str(call.get("id"))
                for call in message.get("tool_calls") or ()
                if isinstance(call, dict)
            ]
        elif message.get("role") == "tool":
            pending = [call_id for call_id in pending if call_id != message.get("tool_call_id")]
        settled.append(message)
    settled.extend(_stopped_results(pending))
    last           = settled[-1] if settled else {}
    ended_by_model = last.get("role") == "assistant" and not last.get("tool_calls")
    if reply and not ended_by_model and not _ended_with_final_answer(settled):
        settled.append({"role": "assistant", "content": reply})
    return settled


def _stopped_results(call_ids: list[str]) -> list[dict[str, Any]]:
    return [
        {"role": "tool", "tool_call_id": call_id, "content": _STOPPED_RESULT}
        for call_id in call_ids
    ]


def _ended_with_final_answer(messages: list[dict[str, Any]]) -> bool:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return any(
                isinstance(call, dict)
                and isinstance(call.get("function"), dict)
                and call["function"].get("name") == "final_answer"
                for call in message.get("tool_calls") or ()
            )
    return False


def _size(turn: dict[str, Any]) -> int:
    return len(json.dumps(turn["messages"], ensure_ascii=False))


class SessionStore:
    """每个（会话, 发起人）一份文件；读改写持有路径锁，并发结束的任务各自追加一轮。"""

    def __init__(self, data_dir: Path) -> None:
        self.root  = data_dir / "sessions"
        self._lock = asyncio.Lock()

    def _path(self, chat: ChatRef) -> Path:
        if not CHAT_KEY_RE.fullmatch(chat.key):
            raise ValueError("invalid chat key")
        return self.root / f"{chat.key}-{chat.user_id}.json"

    def _read(self, path: Path) -> Session | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            logger.warning("xiaoqing_agent session unreadable, starting fresh path=%s", path.name)
            return None
        try:
            return Session(
                system     = dict(data["system"]),
                session_id = str(data["session_id"]),
                turns      = [dict(turn) for turn in data["turns"]],
                plugins    = [str(item) for item in data["plugins"]],
                updated_at = float(data["updated_at"]),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("xiaoqing_agent session malformed, starting fresh path=%s", path.name)
            return None

    async def _run_locked(self, path: Path, work: Callable[[], _T]) -> _T:
        return await run_locked(self._lock, path, work)

    async def load(self, chat: ChatRef, *, idle_seconds: float) -> Session | None:
        """发起人在这个会话里还在说的那份记录；从没说过、隔太久或会话关闭时为 ``None``。"""

        if idle_seconds <= 0:
            return None
        path    = self._path(chat)
        session = await self._run_locked(path, lambda: self._read(path))
        if session is None or not session.turns:
            return None
        if time.time() - session.updated_at > idle_seconds:
            return None
        return session

    async def clear(self, chat: ChatRef) -> None:
        """发起人要求从头开始：删掉这份会话，下一件事开新会话。"""

        path = self._path(chat)
        await self._run_locked(path, lambda: path.unlink(missing_ok=True))

    async def append(
        self,
        chat: ChatRef,
        base: Session,
        turn: dict[str, Any],
        *,
        max_turns: int,
    ) -> None:
        """把这次任务的一轮接到会话后面。

        同一会话里另一个任务先结束、已经追加了一轮时，接在它后面；会话已经换了（比如过期后另一个任务
        开了新会话）时，以这次任务开始时看到的记录为准。
        """

        path = self._path(chat)

        def update() -> None:
            current = self._read(path)
            same    = current is not None and current.session_id == base.session_id
            turns   = [*(current.turns if same and current else base.turns), turn][-max_turns:]
            while len(turns) > 1 and sum(_size(item) for item in turns) > _MAX_SESSION_CHARS:
                turns.pop(0)
            # 工具说明随所在轮次一起保留；裁掉该轮后，下一次使用时重新加载说明。
            plugins = dict.fromkeys(plugin for item in turns for plugin in item.get("plugins", ()))
            payload = {
                "version"   : 1,
                "session_id": base.session_id,
                "updated_at": time.time(),
                "plugins"   : list(plugins),
                "system"    : base.system,
                "turns"     : turns,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")

        await self._run_locked(path, update)
