"""统一记忆测试共用的构造：存储、调用方、事件和固定输出的模型替身。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from plugins.xiaoqing_memory.config import MemoryConfig
from plugins.xiaoqing_memory.consolidate.runner import Deps
from plugins.xiaoqing_memory.ops import apply_record
from plugins.xiaoqing_memory.scopes import AGENT, CHAT, LIFE, Caller
from plugins.xiaoqing_memory.store import MemoryStore

ZONE  = ZoneInfo("Asia/Shanghai")
T0    = 1_790_000_000.0
GROUP = "g300"
ALI   = 456
BO    = 789


def chat_user(scope: str) -> Caller:
    return Caller(CHAT, "user", scope)


def chat_system() -> Caller:
    return Caller(CHAT, "system", "")


def agent_user(scope: str) -> Caller:
    return Caller(AGENT, "user", scope)


def life_system() -> Caller:
    return Caller(LIFE, "system", "")


def user_event(text: str, user_id: int, name: str, key: str, ts: float = T0) -> dict[str, Any]:
    return {
        "kind"  : "message",
        "ts"    : ts,
        "actor" : {"role": "user", "user_id": user_id, "name": name},
        "origin": "user",
        "text"  : text,
        "key"   : key,
    }


def bot_event(text: str, key: str, ts: float = T0) -> dict[str, Any]:
    return {
        "kind"  : "message",
        "ts"    : ts,
        "actor" : {"role": "assistant", "name": "小青"},
        "origin": "self",
        "text"  : text,
        "key"   : key,
    }


def task_event(
    text: str, user_id: int, name: str, key: str, ts: float = T0, **refs: Any
) -> dict[str, Any]:
    return {
        "kind"  : "task",
        "ts"    : ts,
        "actor" : {"role": "user", "user_id": user_id, "name": name},
        "origin": "tool",
        "text"  : text,
        "refs"  : refs,
        "key"   : key,
    }


def record(
    store: MemoryStore,
    caller: Caller,
    scope: str,
    *,
    events: list[dict[str, Any]] | None = None,
    ops: list[dict[str, Any]] | None    = None,
    now: float                          = T0,
) -> dict[str, Any]:
    return apply_record(store, {"events": events or [], "ops": ops or []}, caller, scope, now=now)


def new_store(path: Path) -> MemoryStore:
    return MemoryStore(path / "memory")


class FakeModel:
    """按调用顺序回放预置的输出；输出也可以是根据消息生成的函数。"""

    def __init__(self, *outputs: Any) -> None:
        self.outputs                           = list(outputs)
        self.calls: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if not self.outputs:
            return "{}"
        output = self.outputs.pop(0)
        if callable(output):
            output = output(messages)
        if isinstance(output, Exception):
            raise output
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)


def deps(model: FakeModel, clock: Callable[[], float] = lambda: T0 + 3600) -> Deps:
    return Deps(complete=model, bot_name="小青", zone=ZONE, clock=clock)


CFG = MemoryConfig()
