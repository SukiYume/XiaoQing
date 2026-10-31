"""群友提过的、有时间点的事（考试、面试、出行），时间过了以后合适时顺口关心一句。

记录由投递后的抽取任务写入，按会话保存。一件事只问一次：注入并发出回复后标为“已问”，
对方接下来一轮的回复交给模型理解后结束；对方已经说了结果的，由抽取任务直接结束。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from ..store_base import (
    StoreBase,
    coerce_finite_float,
    coerce_int,
    coerce_optional_int,
    delete_json_artifacts,
)
from ..utils.wall_clock import format_local

LoopStatus             = Literal["open", "asked", "closed"]
_REVIEW_WINDOW_SECONDS = 48 * 3600.0
_KEEP_CLOSED_SECONDS   = 7 * 86400.0
_MAX_TITLE_CHARS       = 24
# 提示过这么多轮仍没问出口，说明一直没有合适的时机，不再提示。
MAX_SURFACES     = 3
_SAFE_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class OpenLoop:
    loop_id   : str
    user_id   : int
    user_name : str
    title     : str
    due_at    : float
    created_at: float
    status    : str   = "open"
    asked_at  : float = 0.0
    surfaced  : int   = 0


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", str(text or ""))
    return {compact[i : i + 2] for i in range(len(compact) - 1)} or {compact}


def same_topic(left: str, right: str) -> bool:
    a, b = _bigrams(left), _bigrams(right)
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= 0.5


def mentions(reply: str, loop: OpenLoop) -> bool:
    """回复里是否真的提到了这件事（标题里至少两组相邻字出现在回复中）。"""

    compact = re.sub(r"\s+", "", str(reply or ""))
    hits    = sum(1 for pair in _bigrams(loop.title) if len(pair) == 2 and pair in compact)
    return hits >= min(2, len(_bigrams(loop.title)))


def _loop_from_json(raw: Any) -> OpenLoop | None:
    if not isinstance(raw, dict):
        return None
    user_id = coerce_optional_int(raw.get("user_id"))
    title   = str(raw.get("title") or "").strip()
    due_at = coerce_finite_float(raw.get("due_at"), default=0.0, minimum=0.0)
    if not user_id or user_id <= 0 or not title or not due_at:
        return None
    status = str(raw.get("status") or "open")
    return OpenLoop(
        loop_id   = str(raw.get("loop_id") or uuid.uuid4().hex[:8])[:16],
        user_id   = user_id,
        user_name = str(raw.get("user_name") or "")[:32],
        title     = title[:_MAX_TITLE_CHARS],
        due_at    = due_at,
        created_at=coerce_finite_float(raw.get("created_at"), default=0.0, minimum=0.0),
        status=status if status in {"open", "asked", "closed"} else "open",
        asked_at=coerce_finite_float(raw.get("asked_at"), default=0.0, minimum=0.0),
        surfaced=coerce_int(raw.get("surfaced"), default=0, minimum=0),
    )


class OpenLoopStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._lock                             = threading.Lock()
        self._cache: dict[str, list[OpenLoop]] = {}

    def bind(self, data_dir) -> None:
        if data_dir != self._data_dir:
            with self._lock:
                self._cache.clear()
        super().bind(data_dir)

    def _path(self, chat_id: str) -> Path | None:
        if not _SAFE_CHAT_ID_RE.match(str(chat_id or "")):
            return None
        return self._resolve_path("life", "open_loops", f"{chat_id}.json")

    def _load_locked(self, chat_id: str) -> list[OpenLoop]:
        if chat_id not in self._cache:
            path = self._path(chat_id)
            raw = self._load_json(path, default=[]) if path else []
            loops = (
                [loop for item in raw if (loop := _loop_from_json(item))]
                if isinstance(raw, list)
                else []
            )
            self._cache[chat_id] = loops
        return self._cache[chat_id]

    def _save_locked(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if path:
            self._save_json(path, [asdict(loop) for loop in self._cache.get(chat_id, ())])

    def get(self, chat_id: str) -> list[OpenLoop]:
        with self._lock:
            return list(self._load_locked(chat_id))

    def pending(self, chat_id: str, *, now: float | None = None) -> list[OpenLoop]:
        """还没结束的记录，供抽取任务判断是否已经有了结果。"""

        moment = time.time() if now is None else now
        return [
            loop
            for loop in self.get(chat_id)
            if loop.status != "closed" and loop.due_at > moment - 7 * 86400
        ]

    def add(
        self,
        chat_id: str,
        *,
        user_id: int,
        user_name: str,
        title: str,
        due_at: float,
        max_loops: int,
        now: float | None = None,
    ) -> None:
        moment = time.time() if now is None else now
        clean  = " ".join(str(title or "").split())[:_MAX_TITLE_CHARS]
        if not clean or user_id <= 0:
            return
        with self._lock:
            loops = self._load_locked(chat_id)
            for index, loop in enumerate(loops):
                if (
                    loop.user_id == user_id
                    and loop.status != "closed"
                    and same_topic(loop.title, clean)
                ):
                    # 同一件事再次提到时更新时间点（比如改期）。
                    loops[index] = replace(loop, due_at=due_at, title=clean)
                    break
            else:
                loops.append(
                    OpenLoop(
                        loop_id    = uuid.uuid4().hex[:8],
                        user_id    = user_id,
                        user_name  = str(user_name or "")[:32],
                        title      = clean,
                        due_at     = due_at,
                        created_at = moment,
                    )
                )
            self._cache[chat_id] = self._pruned(loops, moment, max_loops)
            self._save_locked(chat_id)

    @staticmethod
    def _pruned(loops: list[OpenLoop], now: float, max_loops: int) -> list[OpenLoop]:
        kept = [
            loop
            for loop in loops
            if not (loop.status == "closed" or loop.due_at < now - 7 * 86400)
            or max(loop.due_at, loop.asked_at, loop.created_at) > now - _KEEP_CLOSED_SECONDS
        ]
        return kept[-max_loops:] if max_loops > 0 else kept

    def _set_status(self, chat_id: str, loop_ids: set[str], status: str, now: float) -> None:
        with self._lock:
            loops   = self._load_locked(chat_id)
            changed = False
            for index, loop in enumerate(loops):
                if loop.loop_id in loop_ids and loop.status != status:
                    loops[index] = replace(
                        loop, status=status, asked_at=now if status == "asked" else loop.asked_at
                    )
                    changed = True
            if changed:
                self._save_locked(chat_id)

    def mark_surfaced(self, chat_id: str, loop_id: str) -> None:
        """提示过但这轮回复没问出口：记一次，提示够次数后不再提。"""

        with self._lock:
            loops = self._load_locked(chat_id)
            for index, loop in enumerate(loops):
                if loop.loop_id == loop_id and loop.status == "open":
                    loops[index] = replace(loop, surfaced=loop.surfaced + 1)
                    self._save_locked(chat_id)
                    return

    def mark_asked(self, chat_id: str, loop_id: str, *, now: float | None = None) -> None:
        self._set_status(chat_id, {loop_id}, "asked", time.time() if now is None else now)

    def close(self, chat_id: str, loop_ids: set[str], *, now: float | None = None) -> None:
        if loop_ids:
            self._set_status(chat_id, loop_ids, "closed", time.time() if now is None else now)

    def for_speaker(
        self,
        chat_id: str,
        user_id: int | None,
        *,
        followup_seconds: float,
        now: float | None = None,
    ) -> tuple[OpenLoop, str] | None:
        """当前说话人此刻可以接的一件事，以及接法：``ask`` 顺口问、``review`` 对方在答。"""

        if not user_id:
            return None
        moment = time.time() if now is None else now
        mine   = [loop for loop in self.get(chat_id) if loop.user_id == int(user_id)]
        for loop in sorted(mine, key=lambda item: item.asked_at, reverse=True):
            if loop.status == "asked" and 0 <= moment - loop.asked_at <= _REVIEW_WINDOW_SECONDS:
                return loop, "review"
        due = [
            loop
            for loop in mine
            if loop.status == "open"
            and loop.surfaced < MAX_SURFACES
            and 0 <= moment - loop.due_at <= followup_seconds
        ]
        if not due:
            return None
        return max(due, key=lambda item: item.due_at), "ask"

    def clear(self, chat_id: str) -> None:
        with self._lock:
            self._cache.pop(chat_id, None)
            path = self._path(chat_id)
        if path:
            delete_json_artifacts(path)


def open_loop_block(loop: OpenLoop, mode: str) -> str:
    """给回复模型的一句提示；放在用户消息里，属于低信任参考。"""

    when = format_local("%m月%d日%H:%M", loop.due_at)
    if mode == "review":
        return (
            f"你前面顺口问过对方“{loop.title}”的事，这一轮对方可能是在回答。"
            "接住对方的回答就行，不要再追问同一件事。"
        )
    return (
        f"对方之前提过“{loop.title}”（大约{when}），现在时间已经过了。"
        "只在对方没在说别的急事、话题合适时，像忽然想起来那样顺口问一句结果；"
        "不要说你一直记着，这轮不合适就别提。"
    )
