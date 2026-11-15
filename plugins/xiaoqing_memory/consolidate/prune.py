"""整理第四步：清理。纯函数，不调模型。

- 到期的笔记结束；结束、被替换或撤回超过保留期的删除；她的近况、打算、承诺结束后只再留一周
  （被问到前几天时还记得），迁移来的旧话题摘要到期删除。
- 每个对象每一类（人、办事等前缀各算一组）的有效笔记数有上限，超出时先丢最久没印证也没被用到的。
- 网上经历、日记、往事、学到的东西各有条数上限。
- 事件按保留期删整月文件；引用了已删事件的笔记记 ``evidence_expired``。
- 页面里依据都已不在的行删掉。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..config import MemoryConfig
from ..model import DAY as _DAY
from ..model import Note

KIND_CAPS = {"episode": 300, "world.learning": 200, "self.diary": 60, "self.story": 30}
_CLOSED   = frozenset({"superseded", "done", "dropped", "retracted"})
# 她的近况、打算、承诺来得勤，结束后只留一周。
SHORT_LIVED     = frozenset({"self.state", "self.plan", "self.promise"})
SHORT_KEEP_DAYS = 7


@dataclass
class PruneResult:
    changed: dict[str, Note] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)

    def counts(self) -> dict[str, int]:
        return {"prune.changed": len(self.changed), "prune.removed": len(self.removed)}


def prune_notes(
    notes: Mapping[str, Note],
    cfg: MemoryConfig,
    now: float,
    *,
    dead_seqs: frozenset[int] | set[int] = frozenset(),
) -> PruneResult:
    result  = PruneResult()
    current = dict(notes)
    for note_id, note in current.items():
        if note.status == "active" and note.expires_at and note.expires_at <= now:
            current[note_id] = result.changed[note_id] = note.with_changes(status="done")
    for note_id, note in list(current.items()):
        ended = max(note.observed_at, note.expires_at)
        if note.kind == "legacy.summary" and now - note.created_at > cfg.legacy_keep_days * _DAY:
            result.removed.add(note_id)
        elif (
            note.kind in SHORT_LIVED
            and note.status != "active"
            and now - ended > SHORT_KEEP_DAYS * _DAY
        ):
            result.removed.add(note_id)
        elif note.status in _CLOSED and now - note.observed_at > cfg.superseded_keep_days * _DAY:
            result.removed.add(note_id)
    if dead_seqs:
        for note_id, note in current.items():
            if note_id in result.removed or note.data.get("evidence_expired"):
                continue
            seqs = {
                int(source.get("seq") or 0)
                for source in note.sources
                if source.get("type") == "event"
            }
            if seqs and seqs <= dead_seqs:
                current[note_id] = result.changed[note_id] = note.with_changes(
                    data={**note.data, "evidence_expired": True}
                )
    by_subject: dict[tuple[str, str], list[Note]] = {}
    by_kind   : dict[str, list[Note]]             = {}
    for note_id, note in current.items():
        if note_id in result.removed or note.status != "active":
            continue
        if note.kind in KIND_CAPS:
            by_kind.setdefault(note.kind, []).append(note)
        elif note.kind == "legacy.summary":
            continue
        elif note.subject.startswith("person:") or note.subject.startswith("group:"):
            by_subject.setdefault((note.subject, note.kind.split(".")[0]), []).append(note)
    groups = [(items, cfg.max_notes_per_subject) for items in by_subject.values()]
    groups += [(items, KIND_CAPS[kind]) for kind, items in by_kind.items()]
    for items, cap in groups:
        if len(items) <= cap:
            continue
        items.sort(key=lambda note: max(note.observed_at, note.last_used_at))
        for note in items[: len(items) - cap]:
            result.changed[note.id] = note.with_changes(status="dropped", observed_at=now)
    for note_id in result.removed:
        result.changed.pop(note_id, None)
    return result
