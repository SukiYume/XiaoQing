"""``memory.record``：追加事件、提交笔记操作（新增、导入、更新、结束），逐条核对后写入。

核对都在本地完成：事件和笔记的类型必须是调用方能写的；引用的事件必须在这个范围里；她自己的
近况和承诺必须出自她已经发出的话；未完话头的当事人必须在这段对话里说过话。不合格的条目进
``rejected``，其余照常写入。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .events import clean_event
from .model import DAY, GLOBAL, SELF_KINDS, USAGES, Event, Note, new_note_id, text_limit
from .scopes import (
    AGENT,
    CHAT,
    SELF_SUBJECT,
    Caller,
    may_append,
    may_change_note,
    may_write_note,
    note_scope_for,
    person_subject,
)
from .store import MemoryStore
from .text import clamped, clip, listing, mapping, plain_fields, similarity, squash

# 她自己的记录允许的存活小时数（调用方给的值会被夹在这个范围里）。
SELF_HOURS: dict[str, tuple[float, float]] = {
    "self.state"  : (0.5, 12.0),
    "self.plan"   : (1.0, 48.0),
    "self.promise": (1.0, 168.0),
    "self.story"  : (24.0 * 90, 24.0 * 365),
}
LOOP_KEEP_SECONDS = 7 * DAY
_EVENT_ORIGINS    = {CHAT: frozenset({"user", "self", "tool"}), AGENT: frozenset({"tool"})}
_LIFE_KEEP_DAYS   = (1, 365)
_MAX_EVENTS       = 400
_MAX_OPS          = 40
_RECENT_SPEAKERS  = 80
_SAME_SELF        = 0.5
_SAME_LOOP        = 0.5


def _reject(rejected: list[dict[str, Any]], index: int, reason: str, part: str) -> None:
    rejected.append({"part": part, "index": index, "reason": reason})


def _source_event(store: MemoryStore, scope: str, source: Any) -> Event | None:
    if not isinstance(source, Mapping):
        return None
    if source.get("key"):
        return store.events.by_key(scope, str(source.get("key")))
    try:
        seq = int(source.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    return store.events.get(scope, seq) if seq > 0 else None


def _hours(kind: str, value: Any) -> float:
    low, high = SELF_HOURS[kind]
    return clamped(value, low, low, high)


def _work_task_note(event: Event, now: float, keep_days: int) -> Note | None:
    """一次办事结束时自动留一条办事记录，关于发起人；agent 自己不写笔记。"""

    user = event.user_id
    if event.kind != "task" or user <= 0:
        return None
    summary = clip(event.refs.get("summary") or event.text.splitlines()[0], text_limit("work.task"))
    if not summary:
        return None
    return Note(
        id      = new_note_id(f"task:{event.scope}:{event.refs.get('task_id') or event.seq}"),
        kind    = "work.task",
        scope   = event.scope,
        subject = person_subject(user),
        text    = summary,
        data    = {
            "task_id": str(event.refs.get("task_id") or ""),
            "status" : str(event.refs.get("status") or ""),
            "name"   : event.name,
        },
        origin="tool",
        confidence="verified",
        sources=({"type": "event", "scope": event.scope, "seq": event.seq, "origin": "tool"},),
        created_at=now,
        observed_at=event.ts or now,
        expires_at=now + keep_days * DAY,
    )


def _append_events(
    store: MemoryStore,
    caller: Caller,
    scope: str,
    raw_events: list[Any],
    now: float,
    rejected: list[dict[str, Any]],
    task_keep_days: int,
) -> list[int | None]:
    cleaned: list[dict[str, Any]] = []
    slots  : list[int]            = []
    seqs   : list[int | None]     = [None] * len(raw_events)
    for index, raw in enumerate(raw_events[:_MAX_EVENTS]):
        if not isinstance(raw, Mapping) or not may_append(caller, str(raw.get("kind") or "")):
            _reject(rejected, index, "event_kind", "events")
            continue
        if str(raw.get("origin") or "") not in _EVENT_ORIGINS.get(caller.name, frozenset()):
            _reject(rejected, index, "event_origin", "events")
            continue
        event = clean_event(raw, scope, now)
        if event is None:
            _reject(rejected, index, "event_invalid", "events")
            continue
        cleaned.append(event)
        slots.append(index)
    if not cleaned:
        return seqs
    state = store.scope_state(scope)
    results, after = store.events.append(scope, cleaned, start_seq=int(state["next_seq"]))
    added = [event for event in results if event is not None]
    for slot, appended in zip(slots, results, strict=True):
        seqs[slot] = appended.seq if appended is not None else None
    changes: dict[str, Any] = {"next_seq": after}
    if added:
        changes["last_event_at"] = max(event.ts for event in added)
    store.update_scope(scope, **changes)
    tasks = [note for event in added if (note := _work_task_note(event, now, task_keep_days))]
    if tasks:
        store.put_notes(scope, tasks)
    return seqs


def _self_note(
    store: MemoryStore,
    caller: Caller,
    scope: str,
    op: Mapping[str, Any],
    kind: str,
    target: str,
    now: float,
) -> tuple[Note | None, str]:
    sources = [
        event for item in op.get("sources") or () if (event := _source_event(store, scope, item))
    ]
    if not any(event.role == "assistant" and event.origin == "self" for event in sources):
        return None, "source_not_self"
    text = squash(op.get("text"))[: text_limit(kind)]
    if not text:
        return None, "empty"
    data_raw = mapping(op.get("data"))
    data     = {
        "counterpart" : clip(data_raw.get("counterpart"), 20),
        "origin_scope": scope,
        "source"      : clip(data_raw.get("source") or "chat", 16),
    }
    known = [
        note
        for note in store.notes_for([scope, GLOBAL]).values()
        if note.kind == kind and note.is_active(now)
    ]
    for note in known:
        if similarity(note.text, text) >= _SAME_SELF:
            return None, f"duplicate:{note.id}"
    return (
        Note(
            id         = new_note_id(),
            kind       = kind,
            scope      = target,
            subject    = SELF_SUBJECT,
            text       = text,
            data       = data,
            origin     = "self",
            confidence = "stated",
            sources    = tuple(
                {"type": "event", "scope": scope, "seq": event.seq, "origin": "self"}
                for event in sources
            ),
            created_at  = now,
            observed_at = now,
            expires_at  = now + _hours(kind, op.get("hours")) * 3600.0,
        ),
        "",
    )


def _loop_note(
    store: MemoryStore, scope: str, op: Mapping[str, Any], now: float
) -> tuple[Note | None, str]:
    data_raw = mapping(op.get("data"))
    try:
        user_id = int(data_raw.get("user_id") or 0)
        due_at  = float(data_raw.get("due_at") or 0)
    except (TypeError, ValueError):
        return None, "loop_fields"
    title = squash(op.get("text"))[:24]
    if user_id <= 0 or not title or not (now - 2 * DAY <= due_at <= now + 60 * DAY):
        return None, "loop_fields"
    recent = store.events.events(scope)[-_RECENT_SPEAKERS:]
    spoken = [event for event in recent if event.role == "user" and event.user_id == user_id]
    if not spoken:
        return None, "speaker_not_in_dialogue"
    name = clip(data_raw.get("user_name") or spoken[-1].name, 32)
    for note in store.notes(scope).values():
        if (
            note.kind == "person.loop"
            and note.status == "active"
            and note.subject == person_subject(user_id)
            and similarity(note.text, title) >= _SAME_LOOP
        ):
            # 同一件事再次提到：改时间点（比如改期）。
            return (
                note.with_changes(
                    text        = title,
                    data        = {**note.data, "due_at": due_at},
                    observed_at = now,
                    expires_at  = due_at + LOOP_KEEP_SECONDS,
                ),
                "",
            )
    return (
        Note(
            id      = new_note_id(),
            kind    = "person.loop",
            scope   = scope,
            subject = person_subject(user_id),
            text    = title,
            data    = {
                "user_id"  : user_id,
                "user_name": name,
                "due_at"   : due_at,
                "loop"     : "open",
                "asked_at" : 0.0,
                "surfaced" : 0,
            },
            origin="user",
            confidence="stated",
            sources=({"type": "event", "scope": scope, "seq": spoken[-1].seq, "origin": "user"},),
            created_at=now,
            observed_at=now,
            expires_at=due_at + LOOP_KEEP_SECONDS,
        ),
        "",
    )


def _life_note(op: Mapping[str, Any], kind: str, now: float) -> tuple[Note | None, str]:
    key  = str(op.get("key") or "")
    text = squash(op.get("text"))[: text_limit(kind)]
    if not key or not text:
        return None, "life_fields"
    data = plain_fields(op.get("data"))
    try:
        keep = float(op.get("keep_days") or 30)
    except (TypeError, ValueError):
        keep = 30.0
    keep   = min(_LIFE_KEEP_DAYS[1], max(_LIFE_KEEP_DAYS[0], keep))
    origin = "derived" if kind == "self.diary" else "web"
    # 她看过什么、写过什么是确定的；拿不准的只是查到的知识本身（单一来源时）。
    if kind == "world.learning":
        confidence = "verified" if data.get("verified") is True else "hypothesis"
    else:
        confidence = "stated"
    try:
        observed = float(op.get("observed_at") or now)
    except (TypeError, ValueError):
        observed = now
    return (
        Note(
            id          = new_note_id(f"life:{kind}:{key}"),
            kind        = kind,
            scope       = GLOBAL,
            subject     = SELF_SUBJECT,
            text        = text,
            data        = data,
            origin      = origin,
            confidence  = confidence,
            sources     = ({"type": "external", "key": key[:80], "origin": origin},),
            created_at  = now,
            observed_at = observed,
            expires_at  = observed + keep * DAY,
        ),
        "",
    )


def _import_note(
    op: Mapping[str, Any], kind: str, scope: str, now: float
) -> tuple[Note | None, str]:
    """迁移旧数据：ID 由来源键生成，重复导入得到同一条。没有可核对的原文，可信度记 ``legacy``。"""

    key  = str(op.get("key") or "")
    text = squash(op.get("text"))[: text_limit(kind)]
    if not key or not text:
        return None, "import_fields"
    data   = plain_fields(op.get("data"))
    target = str(op.get("scope") or scope)
    if target not in (scope, GLOBAL) or (target == GLOBAL and kind not in SELF_KINDS):
        return None, "import_scope"
    subject = str(op.get("subject") or "")
    if kind in SELF_KINDS:
        subject = SELF_SUBJECT
        data.setdefault("origin_scope", scope)
    elif not subject:
        return None, "import_subject"
    try:
        observed = float(op.get("observed_at") or now)
        expires  = float(op.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None, "import_fields"
    status = str(op.get("status") or "active")
    usage  = str(op.get("usage") or "normal")
    return (
        Note(
            id          = new_note_id(f"import:{scope}:{key}"),
            kind        = kind,
            scope       = target,
            subject     = subject,
            text        = text,
            data        = data,
            origin      = "self" if kind in SELF_KINDS else "user",
            confidence  = "stated" if kind in SELF_KINDS else "legacy",
            usage       = usage if usage in USAGES else "normal",
            sources     = ({"type": "legacy", "key": key[:120]},),
            created_at  = now,
            observed_at = observed,
            expires_at  = expires,
            status      = status if status in {"active", "done", "dropped"} else "active",
        ),
        "",
    )


def _change(
    store: MemoryStore,
    caller: Caller,
    scope: str,
    op: Mapping[str, Any],
    now: float,
) -> str:
    """``update`` / ``close``：只改调用方能写、在这个范围里的笔记。返回空串表示成功。"""

    note_id = str(op.get("id") or "")
    note    = store.note(scope, note_id) or store.note(GLOBAL, note_id)
    if note is None:
        return "not_found"
    if not may_change_note(caller, note, scope):
        return "not_allowed"
    if (
        note.scope == GLOBAL
        and note.data.get("origin_scope") not in (None, scope)
        and caller.name == CHAT
    ):
        # 别的会话记下的、各处共用的近况：这里可以结束，但不能改写。
        if op.get("op") != "close":
            return "not_allowed"
    if op.get("op") == "close":
        status = str(op.get("status") or "done")
        if status not in {"done", "dropped"}:
            return "bad_status"
        changed = note.with_changes(status=status, observed_at=now)
        if note.kind == "person.loop":
            changed = changed.with_changes(expires_at=now + LOOP_KEEP_SECONDS)
    else:
        data_raw = mapping(op.get("data"))
        data     = dict(note.data)
        for name, value in data_raw.items():
            if isinstance(value, (str, int, float, bool)):
                data[str(name)[:32]] = value
        text = squash(op.get("text"))[: text_limit(note.kind)] or note.text
        changed = note.with_changes(text=text, data=data, observed_at=now)
    store.put_notes(note.scope, [changed])
    return ""


def apply_record(
    store: MemoryStore,
    payload: Mapping[str, Any],
    caller: Caller,
    scope: str,
    *,
    now: float | None   = None,
    task_keep_days: int = 90,
) -> dict[str, Any]:
    moment = time.time() if now is None else now
    rejected: list[dict[str, Any]] = []
    raw_events = listing(payload.get("events"))
    seqs = _append_events(store, caller, scope, raw_events, moment, rejected, task_keep_days)
    ids: list[str | None] = []
    raw_ops = listing(payload.get("ops"))
    for index, op in enumerate(raw_ops[:_MAX_OPS]):
        if not isinstance(op, Mapping):
            _reject(rejected, index, "op_invalid", "ops")
            ids.append(None)
            continue
        name = str(op.get("op") or "")
        if name in ("update", "close"):
            failure = _change(store, caller, scope, op, moment)
            if failure:
                _reject(rejected, index, failure, "ops")
            ids.append(None if failure else str(op.get("id")))
            continue
        kind      = str(op.get("kind") or "")
        importing = name == "import"
        if name not in ("add", "import") or not may_write_note(caller, kind, importing=importing):
            _reject(rejected, index, "op_kind", "ops")
            ids.append(None)
            continue
        note  : Note | None
        reason: str
        if importing:
            note, reason = _import_note(op, kind, scope, moment)
            if note is not None and store.note(note.scope, note.id) is not None:
                ids.append(note.id)
                continue
        elif kind in SELF_KINDS:
            target = note_scope_for(caller, kind, scope, op.get("scope"))
            if target is None:
                note, reason = None, "scope_not_allowed"
            else:
                note, reason = _self_note(store, caller, scope, op, kind, target, moment)
        elif kind == "person.loop":
            note, reason = _loop_note(store, scope, op, moment)
        else:
            note, reason = _life_note(op, kind, moment)
            if note is not None:
                old = store.note(GLOBAL, note.id)
                if old is not None:
                    note = old.with_changes(
                        text        = note.text,
                        data        = note.data,
                        observed_at = note.observed_at,
                        expires_at  = note.expires_at,
                        confidence  = note.confidence,
                    )
        if note is None:
            if reason.startswith("duplicate:"):
                ids.append(reason.split(":", 1)[1])
                continue
            _reject(rejected, index, reason, "ops")
            ids.append(None)
            continue
        store.put_notes(note.scope, [note])
        ids.append(note.id)
    return {"version": 1, "seqs": seqs, "ids": ids, "rejected": rejected}
