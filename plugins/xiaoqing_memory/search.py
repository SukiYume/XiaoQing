"""``memory.search``：按关键词、对象、类型查笔记、页面和（可选）事件。打分是字面重合加新近度。

不给 ``query`` 时按时间列出符合条件的笔记（聊天侧取她自己的记录和未完话头用这种方式）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .model import DAY as _DAY
from .model import GLOBAL, Note
from .recall import live_page, loop_entry, self_entry, visible_notes
from .render import ago, speaker
from .scopes import event_visible
from .store import MemoryStore
from .text import clip, listing, overlap, squash

_MAX_LIMIT = 50


def _note_item(note: Note, score: float) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type"       : "note",
        "id"         : note.id,
        "kind"       : note.kind,
        "scope"      : note.scope,
        "subject"    : note.subject,
        "text"       : note.text,
        "status"     : note.status,
        "confidence" : note.confidence,
        "usage"      : note.usage,
        "observed_at": note.observed_at,
        "created_at" : note.created_at,
        "score"      : round(score, 2),
    }
    if note.kind.startswith("self."):
        item["entry"] = self_entry(note)
    elif note.kind == "person.loop":
        item["loop"] = loop_entry(note)
    elif note.data.get("name"):
        item["name"] = str(note.data.get("name"))
    return item


def _recency(ts: float, now: float) -> float:
    age = max(0.0, now - ts)
    if age < 3 * _DAY:
        return 1.0
    if age < 30 * _DAY:
        return 0.5
    return 0.0


def search(
    store: MemoryStore,
    payload: Mapping[str, Any],
    *,
    scope: str,
    now: float | None = None,
) -> dict[str, Any]:
    moment           = time.time() if now is None else now
    query            = squash(payload.get("query"))[:300]
    kinds            = {str(kind) for kind in listing(payload.get("kinds"))}
    subject          = str(payload.get("subject") or "")
    include_inactive = payload.get("include_inactive") is True
    include_events   = payload.get("include_events") is True
    include_pages    = payload.get("include_pages", bool(query)) is True
    try:
        since = float(payload.get("since") or 0)
        limit = int(payload.get("limit") or 10)
    except (TypeError, ValueError):
        since, limit = 0.0, 10
    limit                                     = max(1, min(_MAX_LIMIT, limit))
    notes                                     = visible_notes(store, scope)
    items: list[tuple[float, dict[str, Any]]] = []
    for note in notes.values():
        if kinds and note.kind not in kinds:
            continue
        if subject and note.subject != subject:
            continue
        if note.status == "retracted" or (not include_inactive and not note.is_active(moment)):
            continue
        if since and max(note.created_at, note.observed_at) < since:
            continue
        if query:
            score = float(overlap(query, note.text + " " + str(note.data.get("name") or "")))
            if score < 1:
                continue
            score += _recency(note.observed_at, moment)
        else:
            score = note.created_at
        items.append((score, _note_item(note, score if query else 0.0)))
    if include_pages and query:
        for page_scope in dict.fromkeys([scope, GLOBAL]):
            for page in store.pages(page_scope).values():
                live = live_page(page, notes, scope)
                if live is None:
                    continue
                for section, index, line in live.lines():
                    score = float(overlap(query, line.text))
                    if score < 2:
                        continue
                    items.append(
                        (
                            score + 0.5,
                            {
                                "type"   : "page",
                                "id"     : f"{page.scope}:{page.subject}#{section}.{index}",
                                "subject": page.subject,
                                "names"  : list(page.names),
                                "text"   : line.text,
                                "score"  : round(score + 0.5, 2),
                            },
                        )
                    )
    if include_events and query:
        for event in store.events.events(scope):
            if not event_visible(event, scope):
                continue
            score = float(overlap(query, event.text))
            if score < 2:
                continue
            score += _recency(event.ts, moment)
            who = speaker(event)
            items.append(
                (
                    score,
                    {
                        "type" : "event",
                        "id"   : f"e{event.seq}",
                        "ts"   : event.ts,
                        "text" : f"{ago(max(0.0, moment - event.ts))}{who}：{clip(event.text, 160)}",
                        "score": round(score, 2),
                    },
                )
            )
    items.sort(key=lambda row: -row[0])
    return {"version": 1, "items": [item for _, item in items[:limit]]}
