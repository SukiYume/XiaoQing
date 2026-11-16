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
    # 开会话时从统一记忆取的、关于发起人的记忆；会话内不变，系统提示保持相同。
    memory: str = ""
    # 读到（或新开）这份会话时文件的修订号。文件每写一次（包括 /agent new 清空）加一，
    # 写回时据此判断这期间会话有没有被换掉或清空。
    revision: int = 0

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
    """每个（会话, 发起人）一份文件；读改写持有路径锁，并发结束的任务各自追加一轮。

    文件带修订号，每次写入加一。清空不是删文件，而是写一个修订号更大的“已清空”标记：
    清空前就在跑的任务结束时，看得出会话已经从头开始，不会把旧会话写回来。
    """

    def __init__(self, data_dir: Path) -> None:
        self.root  = data_dir / "sessions"
        self._lock = asyncio.Lock()

    def _path(self, chat: ChatRef) -> Path:
        if not CHAT_KEY_RE.fullmatch(chat.key):
            raise ValueError("invalid chat key")
        return self.root / f"{chat.key}-{chat.user_id}.json"

    def _read(self, path: Path) -> tuple[Session | None, int]:
        """返回文件里的会话（没有或已清空为 ``None``）和文件的修订号。

        读不出来（缺失、损坏）就当没有这份会话：它只是对话上下文，丢了从头开始，不像任务记录要隔离保留。
        """

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, 0
        except ValueError:
            logger.warning("xiaoqing_agent session unreadable, starting fresh path=%s", path.name)
            return None, 0
        revision = data.get("revision") if isinstance(data, dict) else None
        revision = revision if type(revision) is int and revision >= 0 else 0
        if isinstance(data, dict) and data.get("cleared") is True:
            return None, revision
        try:
            session = Session(
                system     = dict(data["system"]),
                session_id = str(data["session_id"]),
                turns      = [dict(turn) for turn in data["turns"]],
                plugins    = [str(item) for item in data["plugins"]],
                updated_at = float(data["updated_at"]),
                memory     = str(data.get("memory") or ""),
                revision   = revision,
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("xiaoqing_agent session malformed, starting fresh path=%s", path.name)
            return None, revision
        return session, revision

    async def _run_locked(self, path: Path, work: Callable[[], _T]) -> _T:
        return await run_locked(self._lock, path, work)

    async def load(self, chat: ChatRef, *, idle_seconds: float) -> Session | None:
        """发起人在这个会话里还在说的那份记录；从没说过、隔太久或会话关闭时为 ``None``。"""

        if idle_seconds <= 0:
            return None
        path = self._path(chat)
        session, _ = await self._run_locked(path, lambda: self._read(path))
        if session is None or not session.turns:
            return None
        if time.time() - session.updated_at > idle_seconds:
            return None
        return session

    async def new_session(
        self, chat: ChatRef, system: dict[str, Any], *, memory: str = ""
    ) -> Session:
        """这件事开一份新会话，记下此刻文件的修订号。"""

        path = self._path(chat)
        _, revision = await self._run_locked(path, lambda: self._read(path))
        return Session(system, memory=memory, revision=revision)

    async def clear(self, chat: ChatRef) -> None:
        """发起人要求从头开始：会话换成“已清空”标记，下一件事开新会话。"""

        path = self._path(chat)

        def wipe() -> None:
            _, revision = self._read(path)
            payload = {"version": 1, "revision": revision + 1, "cleared": True}
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False) + "\n")

        await self._run_locked(path, wipe)

    async def append(
        self,
        chat: ChatRef,
        base: Session,
        turn: dict[str, Any],
        *,
        max_turns: int,
    ) -> None:
        """把这次任务的一轮接到会话后面。

        同一会话里另一个任务先结束、已经追加了一轮时，接在它后面。这次任务开始以后会话被清空过，
        或者被别的会话换掉了（比如过期后另一个任务开了新会话并已保存），这一轮不写回：
        新的会话以后来者为准，这件事的结果仍在任务记录里。
        """

        path = self._path(chat)

        def update() -> None:
            current, revision = self._read(path)
            if current is not None and current.session_id == base.session_id:
                previous = current.turns
            elif revision == base.revision:
                previous = base.turns
            else:
                logger.info(
                    "xiaoqing_agent session changed since the task started; turn not saved path=%s",
                    path.name,
                )
                return
            turns = [*previous, turn][-max_turns:]
            while len(turns) > 1 and sum(_size(item) for item in turns) > _MAX_SESSION_CHARS:
                turns.pop(0)
            # 工具说明随所在轮次一起保留；裁掉该轮后，下一次使用时重新加载说明。
            plugins = dict.fromkeys(plugin for item in turns for plugin in item.get("plugins", ()))
            payload = {
                "version"   : 1,
                "revision"  : revision + 1,
                "session_id": base.session_id,
                "updated_at": time.time(),
                "plugins"   : list(plugins),
                "system"    : base.system,
                "memory"    : base.memory,
                "turns"     : turns,
            }
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")

        await self._run_locked(path, update)
