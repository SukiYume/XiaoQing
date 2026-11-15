"""``memory.forget``：清空一个范围（代际加一）、删一个人在这个范围里的记忆，或删指定的笔记。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .model import GLOBAL
from .scopes import person_subject
from .store import MemoryStore
from .text import listing


def forget(store: MemoryStore, payload: Mapping[str, Any], *, scope: str) -> dict[str, Any]:
    if payload.get("all") is True:
        generation = store.forget_scope(scope)
        return {"version": 1, "scope": scope, "generation": generation}
    removed = 0
    try:
        user_id = int(payload.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if user_id > 0:
        removed += store.forget_subject(scope, person_subject(user_id))
    ids = [str(item) for item in listing(payload.get("ids")) if str(item)]
    if ids:
        removed += store.forget_notes(scope, ids)
        # 从这个会话记进 ``global`` 的自我记录也能删。
        own = [
            note_id
            for note_id in ids
            if (note := store.note(GLOBAL, note_id)) is not None
            and note.data.get("origin_scope") == scope
        ]
        removed += store.forget_notes(GLOBAL, own)
    return {"version": 1, "scope": scope, "removed": removed}
