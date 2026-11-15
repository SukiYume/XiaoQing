"""三层记录的数据结构：事件、笔记、页面，以及它们和 JSON 之间的转换。

转换时只收合法字段：类型不对的值换成默认值，缺了关键字段的记录整条丢掉。
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .text import listing, mapping, squash

GLOBAL = "global"

ORIGINS     = frozenset({"user", "self", "web", "tool", "admin", "derived"})
CONFIDENCES = frozenset({"stated", "reported", "verified", "hypothesis", "legacy"})
# 按从松到紧排：话题词合并时取更紧的那个。
USAGES      = ("normal", "tone", "avoid")
STATUSES    = frozenset({"active", "superseded", "done", "dropped", "retracted"})
EVENT_KINDS = frozenset({"message", "task"})
DAY         = 86400.0

SELF_KINDS = ("self.state", "self.plan", "self.promise", "self.story")
NOTE_KINDS = frozenset(
    {
        "person.fact",
        "person.pref",
        "person.loop",
        *SELF_KINDS,
        "group.fact",
        "work.task",
        "episode",
        "self.diary",
        "world.learning",
        "legacy.summary",
    }
)
# 每条笔记文字的上限；往事和网上经历可以长一点。
TEXT_LIMITS        = {"self.story": 60, "episode": 120, "self.diary": 300, "world.learning": 160}
DEFAULT_TEXT_LIMIT = 80
LEGACY_TEXT_LIMIT  = 400
QUOTE_LIMIT        = 120

# 页面的小节和字数上限。
PERSON_SECTIONS = ("stable", "recent", "between_us", "errands", "avoid")
GROUP_SECTIONS  = ("about", "topics", "jokes", "recent")
SELF_SECTIONS   = ("doing", "interests", "stories")
PAGE_LIMITS     = {"person": 600, "group": 800, "self": 800}
SECTION_TITLES  = {
    "stable"    : "稳定的事",
    "recent"    : "最近在忙",
    "between_us": "和你之间",
    "errands"   : "办事",
    "avoid"     : "别主动提",
    "about"     : "这个群",
    "topics"    : "常聊的",
    "jokes"     : "群里的梗",
    "doing"     : "最近在做的",
    "interests" : "这阵子的兴趣",
    "stories"   : "讲过的往事",
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _choice(value: Any, allowed: Collection[str], default: str) -> str:
    text = str(value or "")
    return text if text in allowed else default


def supported(note_ids: Sequence[str], notes: Mapping[str, Note]) -> bool:
    """页面的一行（或简介）还有依据：引用的笔记里有有效的，并且没有被撤回的。"""

    cited = [notes.get(note_id) for note_id in note_ids]
    return any(note is not None and note.status == "active" for note in cited) and not any(
        note is not None and note.status == "retracted" for note in cited
    )


def text_limit(kind: str) -> int:
    if kind == "legacy.summary":
        return LEGACY_TEXT_LIMIT
    return TEXT_LIMITS.get(kind, DEFAULT_TEXT_LIMIT)


def new_note_id(seed: str = "") -> str:
    """稳定 ID（导入和外部条目按来源键生成，重复导入得到同一个 ID）或随机 ID。"""

    if seed:
        return "n" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:9]
    return "n" + uuid.uuid4().hex[:9]


def page_kind(subject: str) -> str:
    return subject.split(":", 1)[0] if ":" in subject else subject


# ───────────────────────────── 事件 ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Event:
    seq   : int
    ts    : float
    scope : str
    kind  : str
    actor : dict[str, Any]
    origin: str
    text  : str
    refs  : dict[str, Any] = field(default_factory=dict)
    key   : str = ""

    @property
    def user_id(self) -> int:
        return _int(self.actor.get("user_id"))

    @property
    def role(self) -> str:
        return str(self.actor.get("role") or "")

    @property
    def name(self) -> str:
        return str(self.actor.get("name") or "")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Any) -> Event | None:
        if not isinstance(raw, dict):
            return None
        seq   = _int(raw.get("seq"))
        text  = str(raw.get("text") or "")
        scope = str(raw.get("scope") or "")
        if seq <= 0 or not scope or not text:
            return None
        actor = mapping(raw.get("actor"))
        refs  = mapping(raw.get("refs"))
        return cls(
            seq    = seq,
            ts     = _float(raw.get("ts")),
            scope  = scope,
            kind   = _choice(raw.get("kind"), EVENT_KINDS, "message"),
            actor  = dict(actor),
            origin = _choice(raw.get("origin"), ORIGINS, "user"),
            text   = text,
            refs   = dict(refs),
            key    = str(raw.get("key") or ""),
        )


# ───────────────────────────── 笔记 ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Note:
    id            : str
    kind          : str
    scope         : str
    subject       : str
    text          : str
    data          : dict[str, Any] = field(default_factory=dict)
    origin        : str                        = "user"
    confidence    : str                        = "stated"
    usage         : str                        = "normal"
    sources       : tuple[dict[str, Any], ...] = ()
    quote         : str                        = ""
    created_at    : float                      = 0.0
    observed_at   : float                      = 0.0
    expires_at    : float                      = 0.0
    status        : str                        = "active"
    supersedes    : str                        = ""
    last_used_at  : float                      = 0.0
    use_count     : int                        = 0
    prompt_version: int                        = 0
    verified_at   : float                      = 0.0

    def is_active(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return self.status == "active" and (not self.expires_at or self.expires_at > moment)

    def ends_at(self) -> float:
        """这件事写着的日子结束的时刻（行程、活动、截止日期、约好的时间）；没有时为 0。"""

        try:
            return max(0.0, float(self.data.get("ends_at") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def source_origins(self) -> set[str]:
        """这条笔记的来源链上出现过的来源种类（含它自己）。"""

        found = {self.origin}
        for source in self.sources:
            origin = str(source.get("origin") or "")
            if origin:
                found.add(origin)
        return found

    def with_changes(self, **changes: Any) -> Note:
        return replace(self, **changes)

    def to_json(self) -> dict[str, Any]:
        payload            = asdict(self)
        payload["sources"] = [dict(source) for source in self.sources]
        return payload

    @classmethod
    def from_json(cls, raw: Any) -> Note | None:
        if not isinstance(raw, dict):
            return None
        note_id = str(raw.get("id") or "")
        kind    = str(raw.get("kind") or "")
        scope   = str(raw.get("scope") or "")
        text    = squash(raw.get("text"))[: text_limit(kind)]
        if not note_id or kind not in NOTE_KINDS or not scope or not text:
            return None
        data    = mapping(raw.get("data"))
        sources = listing(raw.get("sources"))
        return cls(
            id             = note_id[:24],
            kind           = kind,
            scope          = scope,
            subject        = str(raw.get("subject") or ""),
            text           = text,
            data           = dict(data),
            origin         = _choice(raw.get("origin"), ORIGINS, "user"),
            confidence     = _choice(raw.get("confidence"), CONFIDENCES, "stated"),
            usage          = _choice(raw.get("usage"), USAGES, "normal"),
            sources        = tuple(dict(item) for item in sources if isinstance(item, dict)),
            quote          = squash(raw.get("quote"))[:QUOTE_LIMIT],
            created_at     = _float(raw.get("created_at")),
            observed_at    = _float(raw.get("observed_at")),
            expires_at     = _float(raw.get("expires_at")),
            status         = _choice(raw.get("status"), STATUSES, "active"),
            supersedes     = str(raw.get("supersedes") or ""),
            last_used_at   = _float(raw.get("last_used_at")),
            use_count      = max(0, _int(raw.get("use_count"))),
            prompt_version = max(0, _int(raw.get("prompt_version"))),
            verified_at    = _float(raw.get("verified_at")),
        )


# ───────────────────────────── 页面 ─────────────────────────────


@dataclass(frozen=True, slots=True)
class PageLine:
    text    : str
    note_ids: tuple[str, ...]
    since   : float = 0.0
    last    : float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "text"    : self.text,
            "note_ids": list(self.note_ids),
            "since"   : self.since,
            "last"    : self.last,
        }

    @classmethod
    def from_json(cls, raw: Any) -> PageLine | None:
        if not isinstance(raw, dict):
            return None
        text = squash(raw.get("text"))
        ids  = listing(raw.get("note_ids"))
        ids  = [str(item) for item in ids if str(item)]
        if not text or not ids:
            return None
        return cls(
            text     = text[:160],
            note_ids = tuple(dict.fromkeys(ids)),
            since    = _float(raw.get("since")),
            last     = _float(raw.get("last")),
        )


@dataclass(frozen=True, slots=True)
class Page:
    subject       : str
    scope         : str
    names         : tuple[str, ...] = ()
    brief         : str             = ""
    brief_note_ids: tuple[str, ...] = ()
    sections      : dict[str, tuple[PageLine, ...]] = field(default_factory=dict)
    version       : int   = 0
    updated_at    : float = 0.0
    prompt_version: int   = 0

    def lines(self) -> list[tuple[str, int, PageLine]]:
        return [
            (section, index, line)
            for section, items in self.sections.items()
            for index, line in enumerate(items)
        ]

    def char_count(self) -> int:
        return len(self.brief) + sum(len(line.text) for _, _, line in self.lines())

    def note_ids(self) -> set[str]:
        found = set(self.brief_note_ids)
        for _, _, line in self.lines():
            found.update(line.note_ids)
        return found

    def without_notes(self, note_ids: Collection[str]) -> Page | None:
        """去掉引用了这些笔记的行和简介（它们是照这些笔记写的）；一点不剩时返回 ``None``。"""

        gone     = set(note_ids)
        sections = {
            name: tuple(line for line in items if gone.isdisjoint(line.note_ids))
            for name, items in self.sections.items()
        }
        keep_brief = bool(self.brief) and gone.isdisjoint(self.brief_note_ids)
        if not any(sections.values()) and not keep_brief:
            return None
        return replace(
            self,
            sections       = sections,
            brief          = self.brief if keep_brief else "",
            brief_note_ids = self.brief_note_ids if keep_brief else (),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "subject"       : self.subject,
            "scope"         : self.scope,
            "names"         : list(self.names),
            "brief"         : self.brief,
            "brief_note_ids": list(self.brief_note_ids),
            "sections"      : {
                name: [line.to_json() for line in items] for name, items in self.sections.items()
            },
            "version"       : self.version,
            "updated_at"    : self.updated_at,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_json(cls, raw: Any) -> Page | None:
        if not isinstance(raw, dict):
            return None
        subject = str(raw.get("subject") or "")
        scope   = str(raw.get("scope") or "")
        if not subject or not scope:
            return None
        sections_raw                              = raw.get("sections")
        sections: dict[str, tuple[PageLine, ...]] = {}
        if isinstance(sections_raw, dict):
            for name, items in sections_raw.items():
                if isinstance(items, list):
                    sections[str(name)] = tuple(
                        line for item in items if (line := PageLine.from_json(item))
                    )
        names     = listing(raw.get("names"))
        brief_ids = listing(raw.get("brief_note_ids"))
        return cls(
            subject        = subject,
            scope          = scope,
            names          = tuple(str(name)[:32] for name in names if str(name).strip())[:6],
            brief          = squash(raw.get("brief"))[:160],
            brief_note_ids = tuple(str(item) for item in brief_ids if str(item)),
            sections       = sections,
            version        = max(0, _int(raw.get("version"))),
            updated_at     = _float(raw.get("updated_at")),
            prompt_version = max(0, _int(raw.get("prompt_version"))),
        )
