"""角色自己说出口的近况、打算和承诺，让她之后说的话前后一致。

记录只来自已经确认发出的角色发言（聊天回复、agent 回复与后台结果、主动开口），由投递后的
抽取任务写入。近况和打算属于角色自己，各会话共用；答应某人的事只在原会话可见，避免把
私聊里的约定带进群里。每条都有过期时间，过期或被标记完成后不再注入。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from ..store_base import StoreBase, coerce_finite_float

SelfKind                         = Literal["state", "plan", "promise"]
SELF_KINDS: tuple[SelfKind, ...] = ("state", "plan", "promise")
# 每类记录允许的存活小时数（模型给的值会被夹在这个范围内）。
KIND_HOURS: dict[str, tuple[float, float]] = {
    "state"  : (0.5, 12.0),
    "plan"   : (1.0, 48.0),
    "promise": (1.0, 168.0),
}
_MAX_TEXT_CHARS  = 40
_MAX_NAME_CHARS  = 20
_FILE            = ("life", "self_timeline.json")
_HISTORY_SECONDS = 7 * 86400.0


@dataclass(frozen=True)
class SelfEntry:
    entry_id   : str
    ts         : float
    chat_id    : str
    kind       : str
    text       : str
    expires_at : float
    status     : str = "active"
    source     : str = "chat"
    counterpart: str = ""
    # 能否在别的会话里被记得：答应某人的事和私聊里涉及对方的近况只留在原会话。
    shared: bool = True

    def visible_in(self, chat_id: str) -> bool:
        if self.kind == "promise" or not self.shared:
            return self.chat_id == chat_id
        return True

    def is_active(self, now: float) -> bool:
        return self.status == "active" and self.expires_at > now


def new_entry(
    *,
    chat_id: str,
    kind: str,
    text: str,
    hours: Any,
    source: str,
    counterpart: str  = "",
    now: float | None = None,
    shared: bool      = True,
) -> SelfEntry | None:
    """校验一条候选记录；类别不对或内容为空时返回 None。"""

    clean = " ".join(str(text or "").split())[:_MAX_TEXT_CHARS]
    if kind not in KIND_HOURS or not clean or not chat_id:
        return None
    low, high = KIND_HOURS[kind]
    try:
        span = float(hours)
    except (TypeError, ValueError):
        span = low
    span    = min(high, max(low, span if span == span else low))
    created = time.time() if now is None else now
    return SelfEntry(
        entry_id    = uuid.uuid4().hex[:8],
        ts          = created,
        chat_id     = chat_id,
        kind        = kind,
        text        = clean,
        expires_at  = created + span * 3600.0,
        source      = str(source or "chat")[:16],
        counterpart = " ".join(str(counterpart or "").split())[:_MAX_NAME_CHARS],
        shared      = bool(shared) and kind != "promise",
    )


def _entry_from_json(raw: Any) -> SelfEntry | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "")
    text = str(raw.get("text") or "").strip()
    if kind not in KIND_HOURS or not text:
        return None
    status = str(raw.get("status") or "active")
    return SelfEntry(
        entry_id=str(raw.get("entry_id") or uuid.uuid4().hex[:8])[:16],
        ts=coerce_finite_float(raw.get("ts"), default=0.0, minimum=0.0),
        chat_id = str(raw.get("chat_id") or ""),
        kind    = kind,
        text    = text[:_MAX_TEXT_CHARS],
        expires_at=coerce_finite_float(raw.get("expires_at"), default=0.0, minimum=0.0),
        status      = status if status in {"active", "done", "dropped"} else "active",
        source      = str(raw.get("source") or "chat")[:16],
        counterpart = str(raw.get("counterpart") or "")[:_MAX_NAME_CHARS],
        shared      = raw.get("shared") is not False,
    )


class SelfTimelineStore(StoreBase):
    """全部记录放在一个文件里；读写都在实例锁内完成。"""

    def __init__(self) -> None:
        super().__init__()
        self._lock                            = threading.Lock()
        self._entries: list[SelfEntry] | None = None

    def bind(self, data_dir) -> None:
        if data_dir != self._data_dir:
            with self._lock:
                self._entries = None
        super().bind(data_dir)

    def _load_locked(self) -> list[SelfEntry]:
        if self._entries is None:
            raw = self._load_json_from_path_parts(*_FILE, default=[])
            entries       = [entry for item in raw or () if (entry := _entry_from_json(item))]
            self._entries = entries if isinstance(raw, list) else []
        return self._entries

    def _save_locked(self) -> None:
        self._save_json_to_path_parts(*_FILE, data=[asdict(entry) for entry in self._entries or ()])

    def entries(self) -> list[SelfEntry]:
        with self._lock:
            return list(self._load_locked())

    def visible(self, chat_id: str, *, now: float | None = None, limit: int = 8) -> list[SelfEntry]:
        """此会话可见、仍然有效的记录，最新的在后。"""

        moment = time.time() if now is None else now
        with self._lock:
            active = [
                entry
                for entry in self._load_locked()
                if entry.is_active(moment) and entry.visible_in(chat_id)
            ]
        return active[-max(0, limit) :] if limit else []

    def history(self, chat_id: str, *, since: float) -> list[SelfEntry]:
        """``since`` 之后记下的、此会话可见的全部记录（含已过期、已结束的）。"""

        with self._lock:
            return [
                entry
                for entry in self._load_locked()
                if entry.ts >= since and entry.visible_in(chat_id)
            ]

    def apply(
        self,
        *,
        added: Iterable[SelfEntry]          = (),
        resolved: Iterable[tuple[str, str]] = (),
        chat_id: str,
        max_entries: int,
        now: float | None = None,
    ) -> None:
        """写入新记录并结束旧记录；只能结束本会话可见的条目。"""

        moment  = time.time() if now is None else now
        updates = {
            entry_id: status for entry_id, status in resolved if status in {"done", "dropped"}
        }
        with self._lock:
            kept: list[SelfEntry] = []
            for entry in self._load_locked():
                if entry.entry_id in updates and entry.visible_in(chat_id):
                    entry = replace(entry, status=updates[entry.entry_id])
                # 过期或结束的记录再留一周，被问到前几天时还记得自己说过什么。
                if entry.ts > moment - _HISTORY_SECONDS or entry.is_active(moment):
                    kept.append(entry)
            kept.extend(added)
            if max_entries > 0:
                kept = kept[-max_entries:]
            self._entries = kept
            self._save_locked()

    def clear(self, chat_id: str) -> None:
        """会话重置时删掉这个会话里产生的记录。"""

        with self._lock:
            entries   = self._load_locked()
            remaining = [entry for entry in entries if entry.chat_id != chat_id]
            if len(remaining) != len(entries):
                self._entries = remaining
                self._save_locked()


def _ago(seconds: float) -> str:
    if seconds < 120:
        return "刚才"
    if seconds < 3600:
        return f"约{int(seconds // 60)}分钟前"
    if seconds < 86400:
        return f"约{int(seconds // 3600)}小时前"
    return f"约{int(seconds // 86400)}天前"


def describe_entry(entry: SelfEntry, now: float) -> str:
    when = _ago(max(0.0, now - entry.ts))
    if entry.kind == "promise":
        who = f"答应{entry.counterpart}" if entry.counterpart else "答应过"
        return f"{when}{who}：{entry.text}（还没办完）"
    if entry.kind == "plan":
        return f"{when}说打算：{entry.text}"
    return f"{when}说：{entry.text}"


def self_timeline_block(entries: Sequence[SelfEntry], *, now: float | None = None) -> str:
    """给回复模型的“你最近说过的关于自己的事”。"""

    entries = [entry for entry in entries if isinstance(entry, SelfEntry)]
    if not entries:
        return ""
    moment = time.time() if now is None else now
    lines  = "\n".join(f"- {describe_entry(entry, moment)}" for entry in entries)
    return (
        "你最近说过的关于自己的事（都是你自己说出口的。后面保持一致，不要说出相矛盾的经历；"
        "和作息底色冲突时以这里为准；被问到或正好相关时顺着说，没必要时不复述；"
        "答应的事办完了再说办完）：\n" + lines
    )
