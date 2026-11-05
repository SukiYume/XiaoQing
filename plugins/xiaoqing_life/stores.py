"""全部持久化：内存快照 + 原子 JSON 写入（带备份），一把锁串行所有写。

读取走内存快照（``turn_context`` 不碰磁盘）；修改只能经 ``mutate``：同步函数在锁内改内存，
随后把有变化的文件原子写盘。主文件损坏时先读备份，都坏时隔离旧文件从空开始。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from core.atomic_store import AtomicJsonStore

T = TypeVar("T")

# 文件名 -> 工厂（空值）。类型也用来校验读回的内容。
_COLLECTIONS: dict[str, Callable[[], Any]] = {
    "episodes"   : list,
    "terms"      : dict,
    "expressions": list,
    "interests"  : dict,
    "threads"    : list,
    "seen"       : dict,
    "state"      : dict,
}

MAX_TERMS        = 500
MAX_EXPRESSIONS  = 300
MAX_INTERESTS    = 200
MAX_LOOKUP_QUEUE = 20
MAX_RECENT       = 12
SEEN_DAYS        = 7


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def default_state() -> dict[str, Any]:
    return {
        "day"            : "",
        "sessions_today" : 0,
        "llm_calls_today": 0,
        "activity_log"   : [],
        "last_session_at": 0.0,
        "last_failure_at": 0.0,
        "last_decision"  : {},
        "wound_down_day" : "",
        "lookup_queue"   : [],
        "recent"         : [],
        "diary"          : {},
    }


def _quarantine(path: Path) -> None:
    """读不懂的旧文件改名留底，让插件从空开始，而不是反复覆盖或崩溃。"""

    try:
        os.replace(path, path.with_name(path.name + ".corrupt"))
    except OSError:
        return


def _load_collection(path: Path, empty: Callable[[], Any]) -> Any:
    store = AtomicJsonStore(path)
    value = store.read(None)  # 主文件坏了会尝试用备份恢复
    if value is None:
        if path.exists():
            _quarantine(path)
        return empty()
    if not isinstance(value, type(empty())):
        _quarantine(path)
        return empty()
    return value


class LifeStore:
    """内存快照与落盘的唯一入口。字段直接是 JSON 友好的 list/dict。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir                             = Path(data_dir)
        self._lock                                = asyncio.Lock()
        self._written: dict[str, str]             = {}
        self.episodes: list[dict[str, Any]]       = []
        self.terms: dict[str, dict[str, Any]]     = {}
        self.expressions: list[dict[str, Any]]    = []
        self.interests: dict[str, dict[str, Any]] = {}
        self.threads: list[dict[str, Any]]        = []
        self.seen: dict[str, float]               = {}
        self.state: dict[str, Any]                = default_state()

    def load(self) -> None:
        """同步读取全部文件；插件初始化时调用一次。"""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name, empty in _COLLECTIONS.items():
            value = _load_collection(self._path(name), empty)
            if name == "state":
                value = {**default_state(), **value}
            setattr(self, name, value)
            self._written[name] = self._dump(value)

    def _path(self, name: str) -> Path:
        return self.data_dir / f"{name}.json"

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _pending_writes(self) -> list[tuple[str, str]]:
        pending = []
        for name in _COLLECTIONS:
            text = self._dump(getattr(self, name))
            if text != self._written.get(name):
                pending.append((name, text))
        return pending

    async def mutate(self, fn: Callable[[LifeStore], T]) -> T:
        """在锁内同步改内存，再把有变化的文件写盘。``fn`` 内不要 await。"""

        async with self._lock:
            result = fn(self)
            await self._flush()
            return result

    async def flush(self) -> None:
        async with self._lock:
            await self._flush()

    async def _flush(self) -> None:
        for name, text in self._pending_writes():
            store = AtomicJsonStore(self._path(name))
            await asyncio.to_thread(store.write_with_backup, json.loads(text))
            self._written[name] = text


def roll_day(state: dict[str, Any], day: str) -> None:
    """跨日（配置时区的自然日）重置当天计数；活动日志不清，滚动窗口还要用。"""

    if state.get("day") != day:
        state["day"]             = day
        state["sessions_today"]  = 0
        state["llm_calls_today"] = 0


async def reserve_llm_call(store: LifeStore, day: str, limit: int) -> bool:
    """先把计数 +1 并落盘再调用模型；失败不退还，重启不清零。"""

    def reserve(s: LifeStore) -> bool:
        roll_day(s.state, day)
        if int(s.state.get("llm_calls_today", 0)) >= limit:
            return False
        s.state["llm_calls_today"] = int(s.state.get("llm_calls_today", 0)) + 1
        return True

    return await store.mutate(reserve)
