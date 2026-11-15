"""记忆插件的存储：事件日志、按范围的笔记与页面文件、状态文件（编号、代际、水位、预算、统计）。

笔记和页面按范围懒加载进内存，写入用 Core 的原子存储并保留备份。取用时记下的使用次数只改内存，
随定时检查和关闭时一起落盘。所有内存结构由一把可重入锁保护；跨步骤的整理另用按范围的异步锁。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import Any

from core.async_keyed_lock import AsyncKeyedLockPool
from core.atomic_store import AtomicJsonStore

from .events import EventLog
from .model import GLOBAL, Note, Page
from .scopes import valid_scope
from .text import mapping

STATS_DAYS = 14


# 忘掉的笔记记这么久：只为挡住删除时正在进行的那一轮整理，模型调用不会拖过这个时间。
_FORGOTTEN_KEEP_SECONDS = 3600.0


def _scope_state() -> dict[str, Any]:
    return {
        "next_seq"            : 1,
        "generation"          : 0,
        "watermark"           : 0,
        "last_event_at"       : 0.0,
        "last_consolidated_at": 0.0,
    }


class MemoryStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir                           = data_dir
        self.events                             = EventLog(data_dir / "events")
        self.locks                              = AsyncKeyedLockPool()
        self._lock                              = threading.RLock()
        self._notes: dict[str, dict[str, Note]] = {}
        self._pages: dict[str, dict[str, Page]] = {}
        self._dirty_notes: set[str]             = set()
        # 被忘掉的笔记（范围, ID）→ 忘掉的时刻。删除时正在进行的整理稍后才写回，靠它认出不该写回的。
        self._forgotten: dict[tuple[str, str], float] = {}
        self._state                                   = self._load_state()

    # ── 文件 ──

    def _state_store(self) -> AtomicJsonStore:
        return AtomicJsonStore(self.data_dir / "state.json")

    def _notes_store(self, scope: str) -> AtomicJsonStore:
        return AtomicJsonStore(self.data_dir / "notes" / f"{scope}.json")

    def _pages_store(self, scope: str) -> AtomicJsonStore:
        return AtomicJsonStore(self.data_dir / "pages" / f"{scope}.json")

    def _load_state(self) -> dict[str, Any]:
        raw: Any = self._state_store().read({}, raise_on_error=False)
        if not isinstance(raw, dict):
            raw = {}
        scopes = mapping(raw.get("scopes"))
        state  = {
            "version": 1,
            "scopes" : {
                str(scope): {**_scope_state(), **value}
                for scope, value in scopes.items()
                if valid_scope(scope) and isinstance(value, dict)
            },
            "budget": mapping(raw.get("budget")),
            "stats" : mapping(raw.get("stats")),
            "verify": mapping(raw.get("verify")),
        }
        return state

    def save_state(self) -> None:
        with self._lock:
            self._state_store().write(self._state)

    # ── 状态 ──

    def scope_state(self, scope: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._state["scopes"].get(scope) or _scope_state())

    def update_scope(self, scope: str, **changes: Any) -> None:
        with self._lock:
            current = self._state["scopes"].setdefault(scope, _scope_state())
            current.update(changes)
            self._state_store().write(self._state)

    def generation(self, scope: str) -> int:
        return int(self.scope_state(scope).get("generation") or 0)

    def known_scopes(self) -> list[str]:
        with self._lock:
            found = set(self._state["scopes"])
        found.update(self.events.scopes())
        for folder in ("notes", "pages"):
            directory = self.data_dir / folder
            if directory.is_dir():
                found.update(path.stem for path in directory.glob("*.json"))
        return sorted(scope for scope in found if valid_scope(scope))

    def reserve_calls(self, day: str, count: int, limit: int, *, force: bool = False) -> bool:
        """先占预算并落盘再调用模型；失败不退还。``force`` 只记账不拦。"""

        with self._lock:
            budget = self._state["budget"]
            if budget.get("day") != day:
                budget.clear()
                budget.update({"day": day, "used": 0})
            used = int(budget.get("used") or 0)
            if not force and used + count > limit:
                return False
            budget["used"] = used + count
            self._state_store().write(self._state)
            return True

    def calls_used(self, day: str) -> int:
        with self._lock:
            budget = self._state["budget"]
            return int(budget.get("used") or 0) if budget.get("day") == day else 0

    def bump(self, day: str, counters: Mapping[str, int | float], *, save: bool = False) -> None:
        with self._lock:
            stats = self._state["stats"]
            today = stats.setdefault(day, {})
            for name, value in counters.items():
                if value:
                    today[name] = today.get(name, 0) + value
            for old in sorted(stats)[:-STATS_DAYS]:
                stats.pop(old, None)
            if save:
                self._state_store().write(self._state)

    def stats(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {day: dict(values) for day, values in self._state["stats"].items()}

    def verify_state(self) -> dict[str, Any]:
        with self._lock:
            marks: dict[str, Any] = self._state["verify"]
            return marks

    # ── 笔记 ──

    def _notes_locked(self, scope: str) -> dict[str, Note]:
        notes = self._notes.get(scope)
        if notes is None:
            raw: Any = self._notes_store(scope).read([], raise_on_error=False)
            notes = {}
            for item in raw if isinstance(raw, list) else []:
                note = Note.from_json(item)
                if note is not None and note.scope == scope:
                    notes[note.id] = note
            self._notes[scope] = notes
        return notes

    def _save_notes_locked(self, scope: str) -> None:
        notes = self._notes.get(scope, {})
        self._notes_store(scope).write([note.to_json() for note in notes.values()])
        self._dirty_notes.discard(scope)

    def notes(self, scope: str) -> dict[str, Note]:
        with self._lock:
            return dict(self._notes_locked(scope))

    def note(self, scope: str, note_id: str) -> Note | None:
        with self._lock:
            return self._notes_locked(scope).get(note_id)

    def find_note(self, note_id: str) -> Note | None:
        for scope in self.known_scopes():
            note = self.note(scope, note_id)
            if note is not None:
                return note
        return None

    def notes_for(self, scopes: Iterable[str]) -> dict[str, Note]:
        merged: dict[str, Note] = {}
        with self._lock:
            for scope in dict.fromkeys(scopes):
                merged.update(self._notes_locked(scope))
        return merged

    def put_notes(self, scope: str, notes: Iterable[Note]) -> None:
        with self._lock:
            current = self._notes_locked(scope)
            changed = False
            for note in notes:
                if note.scope != scope:
                    raise ValueError("note scope mismatch")
                current[note.id] = note
                changed          = True
            if changed:
                self._save_notes_locked(scope)

    def put_consolidated(
        self, scope: str, notes: Iterable[Note], *, generation: int | None = None
    ) -> bool:
        """整理把笔记写回：这期间被忘掉的不写回；范围已被清空（代际变了）时一条都不写，返回 ``False``。"""

        with self._lock:
            if generation is not None and self.generation(scope) != generation:
                return False
            gone = self._forgotten_locked([scope])
            self.put_notes(scope, [note for note in notes if note.id not in gone])
            return True

    def forget_notes(self, scope: str, ids: Iterable[str]) -> int:
        """忘掉这些笔记：删除，页面上照它们写的行和简介一起去掉；正在进行的整理也不会把它们写回。"""

        with self._lock:
            wanted  = list(ids)
            removed = self.remove_notes(scope, wanted)
            self._mark_forgotten_locked(scope, wanted)
            self.scrub_pages(scope, wanted)
            return removed

    def _mark_forgotten_locked(self, scope: str, ids: Iterable[str]) -> None:
        now    = time.time()
        cutoff = now - _FORGOTTEN_KEEP_SECONDS
        for key in [key for key, at in self._forgotten.items() if at < cutoff]:
            del self._forgotten[key]
        for note_id in ids:
            self._forgotten[(scope, note_id)] = now

    def _forgotten_locked(self, scopes: Iterable[str]) -> set[str]:
        wanted = set(scopes)
        return {note_id for (scope, note_id) in self._forgotten if scope in wanted}

    def remove_notes(self, scope: str, ids: Iterable[str]) -> int:
        with self._lock:
            current = self._notes_locked(scope)
            removed = [note_id for note_id in ids if current.pop(note_id, None) is not None]
            if removed:
                self._save_notes_locked(scope)
            return len(removed)

    def mark_used(self, notes: Iterable[Note], now: float) -> None:
        """取用进常驻块的笔记记一次使用；只改内存，随下一次落盘保存。"""

        with self._lock:
            for note in notes:
                current = self._notes_locked(note.scope)
                if note.id in current:
                    old              = current[note.id]
                    current[note.id] = old.with_changes(
                        last_used_at=now, use_count=old.use_count + 1
                    )
                    self._dirty_notes.add(note.scope)

    def flush(self) -> None:
        with self._lock:
            for scope in list(self._dirty_notes):
                self._save_notes_locked(scope)
            self._state_store().write(self._state)

    # ── 页面 ──

    def _pages_locked(self, scope: str) -> dict[str, Page]:
        pages = self._pages.get(scope)
        if pages is None:
            raw: Any = self._pages_store(scope).read({}, raise_on_error=False)
            pages = {}
            for item in raw.values() if isinstance(raw, dict) else []:
                page = Page.from_json(item)
                if page is not None and page.scope == scope:
                    pages[page.subject] = page
            self._pages[scope] = pages
        return pages

    def pages(self, scope: str) -> dict[str, Page]:
        with self._lock:
            return dict(self._pages_locked(scope))

    def page(self, scope: str, subject: str) -> Page | None:
        with self._lock:
            return self._pages_locked(scope).get(subject)

    def put_page(self, page: Page) -> Page | None:
        """写入页面，返回实际写下的那一份。

        引用了已被忘掉的笔记的行和简介先去掉（页面是照删除之前的笔记写的）；一点不剩时不写，
        返回 ``None``。
        """

        with self._lock:
            gone = self._forgotten_locked([page.scope, GLOBAL]) & page.note_ids()
            kept = page.without_notes(gone) if gone else page
            if kept is None:
                return None
            pages               = self._pages_locked(kept.scope)
            pages[kept.subject] = kept
            self._pages_store(kept.scope).write(
                {subject: item.to_json() for subject, item in pages.items()}
            )
            return kept

    def drop_pages(self, scope: str, subjects: Iterable[str]) -> None:
        with self._lock:
            pages   = self._pages_locked(scope)
            removed = [subject for subject in subjects if pages.pop(subject, None) is not None]
            if removed:
                self._pages_store(scope).write(
                    {subject: item.to_json() for subject, item in pages.items()}
                )

    def scrub_pages(self, scope: str, note_ids: Collection[str]) -> None:
        """这些笔记删掉了：这个范围的页面上引用它们的行和简介一起去掉，空了的页面删除。"""

        with self._lock:
            pages   = self._pages_locked(scope)
            changed = False
            for subject, page in list(pages.items()):
                kept = page.without_notes(note_ids)
                if kept is None:
                    del pages[subject]
                elif kept != page:
                    pages[subject] = kept
                else:
                    continue
                changed = True
            if changed:
                self._pages_store(scope).write(
                    {subject: item.to_json() for subject, item in pages.items()}
                )

    # ── 清空 ──

    def forget_scope(self, scope: str) -> int:
        """清空一个范围：代际加一，删掉事件、笔记和页面，以及从这里记进 ``global`` 的自我记录。

        ``global`` 的页面上照这些自我记录写的行和简介一起去掉。
        """

        with self._lock:
            state                  = self._state["scopes"].setdefault(scope, _scope_state())
            state["generation"]    = int(state.get("generation") or 0) + 1
            state["watermark"]     = 0
            state["unparsed"]      = None
            state["last_event_at"] = 0.0
            state["topics"]        = []
            self._mark_forgotten_locked(scope, list(self._notes_locked(scope)))
            self._notes[scope] = {}
            self._pages[scope] = {}
            for folder in ("notes", "pages"):
                for suffix in ("", ".bak"):
                    path = self.data_dir / folder / f"{scope}.json{suffix}"
                    path.unlink(missing_ok=True)
            shared = self._notes_locked(GLOBAL)
            gone   = [
                note_id
                for note_id, note in shared.items()
                if str(note.data.get("origin_scope") or "") == scope
            ]
            for note_id in gone:
                shared.pop(note_id, None)
            if gone:
                self._save_notes_locked(GLOBAL)
                self._mark_forgotten_locked(GLOBAL, gone)
                self.scrub_pages(GLOBAL, gone)
            self._state_store().write(self._state)
            generation = state["generation"]
        self.events.forget(scope)
        return int(generation)
