"""第 1 层：只追加的原始记录。每个范围按月一个 JSONL 文件，读取时容忍损坏的行。

读取走内存缓存（按范围懒加载）；检索直接顺序扫描，不建索引。一个范围一把线程锁，追加在锁内
完成文件写入和缓存更新。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model import DAY, EVENT_KINDS, ORIGINS, Event
from .text import clip, mapping, plain_fields

MAX_EVENT_TEXT = 2000
MAX_KEY_CHARS  = 160


def month_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def _month_end(month: str) -> float:
    year, number = (int(part) for part in month.split("-"))
    year, number = (year + 1, 1) if number == 12 else (year, number + 1)
    return datetime(year, number, 1, tzinfo=UTC).timestamp()


def clean_event(raw: Mapping[str, Any], scope: str, now: float) -> dict[str, Any] | None:
    """调用方给的一条事件：类型、来源、文字、时间各自校验；不合格时返回 ``None``。"""

    kind = str(raw.get("kind") or "")
    text = clip(raw.get("text"), MAX_EVENT_TEXT)
    if kind not in EVENT_KINDS or not text:
        return None
    origin = str(raw.get("origin") or "")
    if origin not in ORIGINS:
        return None
    try:
        ts = float(raw.get("ts") or now)
    except (TypeError, ValueError):
        ts = now
    if not (0 < ts <= now + 300):
        ts = now
    actor_raw             = mapping(raw.get("actor"))
    actor: dict[str, Any] = {"role": str(actor_raw.get("role") or "")[:16]}
    try:
        user_id = int(actor_raw.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if user_id > 0:
        actor["user_id"] = user_id
    name = clip(actor_raw.get("name"), 32)
    if name:
        actor["name"] = name
    refs = plain_fields(raw.get("refs"))
    return {
        "ts"    : ts,
        "scope" : scope,
        "kind"  : kind,
        "actor" : actor,
        "origin": origin,
        "text"  : text,
        "refs"  : refs,
        "key"   : str(raw.get("key") or "")[:MAX_KEY_CHARS],
    }


class _ScopeLog:
    __slots__ = ("events", "keys", "by_seq", "loaded")

    def __init__(self) -> None:
        self.events: list[Event]      = []
        self.keys: dict[str, int]     = {}
        self.by_seq: dict[int, Event] = {}
        self.loaded                   = False

    def add(self, event: Event) -> None:
        self.events.append(event)
        self.by_seq[event.seq] = event
        if event.key:
            self.keys[event.key] = event.seq


class EventLog:
    def __init__(self, root: Path) -> None:
        self.root                              = root
        self._guard                            = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._logs: dict[str, _ScopeLog]       = {}

    # ── 内部 ──

    def _lock(self, scope: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(scope, threading.Lock())

    def _dir(self, scope: str) -> Path:
        return self.root / scope

    def _files(self, scope: str) -> list[Path]:
        directory = self._dir(scope)
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.glob("*.jsonl") if path.is_file())

    def _load_locked(self, scope: str) -> _ScopeLog:
        log = self._logs.get(scope)
        if log is not None and log.loaded:
            return log
        log = _ScopeLog()
        for path in self._files(scope):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = Event.from_json(json.loads(line))
                except ValueError:
                    continue
                if event is not None and event.scope == scope and event.seq not in log.by_seq:
                    log.add(event)
        log.events.sort(key=lambda event: event.seq)
        log.loaded        = True
        self._logs[scope] = log
        return log

    @staticmethod
    def _append_lines(path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    # 上次写到一半的末行：先补一个换行，坏行读取时会被跳过。
                    prefix = "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + "".join(line + "\n" for line in lines))
            handle.flush()
            os.fsync(handle.fileno())

    # ── 读取 ──

    def events(self, scope: str) -> list[Event]:
        with self._lock(scope):
            return list(self._load_locked(scope).events)

    def after(self, scope: str, seq: int) -> list[Event]:
        with self._lock(scope):
            return [event for event in self._load_locked(scope).events if event.seq > seq]

    def get(self, scope: str, seq: int) -> Event | None:
        with self._lock(scope):
            return self._load_locked(scope).by_seq.get(int(seq))

    def by_key(self, scope: str, key: str) -> Event | None:
        with self._lock(scope):
            log = self._load_locked(scope)
            seq = log.keys.get(key)
            return log.by_seq.get(seq) if seq is not None else None

    def scopes(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(path.name for path in self.root.iterdir() if path.is_dir())

    # ── 写入 ──

    def append(
        self, scope: str, items: Iterable[dict[str, Any]], *, start_seq: int
    ) -> tuple[list[Event | None], int]:
        """按顺序追加；幂等键已存在的条目返回 ``None``。返回（每条的结果，下一个可用编号）。"""

        results: list[Event | None]    = []
        by_file: dict[Path, list[str]] = {}
        with self._lock(scope):
            log      = self._load_locked(scope)
            next_seq = max(start_seq, (log.events[-1].seq + 1) if log.events else 1)
            for item in items:
                key = str(item.get("key") or "")
                if key and key in log.keys:
                    results.append(None)
                    continue
                event = Event(seq=next_seq, **item)
                next_seq += 1
                by_file.setdefault(self._dir(scope) / f"{month_of(event.ts)}.jsonl", []).append(
                    json.dumps(event.to_json(), ensure_ascii=False)
                )
                log.add(event)
                results.append(event)
            for path, lines in by_file.items():
                self._append_lines(path, lines)
        return results, next_seq

    def forget(self, scope: str) -> None:
        with self._lock(scope):
            self._logs.pop(scope, None)
            directory = self._dir(scope)
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
            self._logs[scope] = _ScopeLog()
            self._logs[scope].loaded = True

    def prune(self, scope: str, *, keep_days: int, now: float | None = None) -> set[int]:
        """删掉整月都早于保留期的文件，返回被删事件的编号。"""

        cutoff            = (time.time() if now is None else now) - keep_days * DAY
        removed: set[int] = set()
        with self._lock(scope):
            for path in self._files(scope):
                month = path.stem
                try:
                    end = _month_end(month)
                except ValueError:
                    continue
                if end > cutoff:
                    continue
                log  = self._load_locked(scope)
                gone = {event.seq for event in log.events if month_of(event.ts) == month}
                removed |= gone
                try:
                    path.unlink()
                except OSError:
                    continue
                log.events = [event for event in log.events if event.seq not in gone]
                for seq in gone:
                    event = log.by_seq.pop(seq, None)
                    if event is not None and event.key:
                        log.keys.pop(event.key, None)
        return removed
