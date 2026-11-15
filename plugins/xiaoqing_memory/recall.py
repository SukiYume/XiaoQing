"""``memory.recall``：取这一轮的常驻块。纯内存，不调模型。

- ``reply``：聊天回复。她自己的页面和近况、说话人的页面、群页、与这句话字面相关的旧事、可以顺口接的
  到期话头；群聊里另给这两三天的话题词（只有词，chat 转交 xiaoqing_life 当上网的方向）。
  自我页另按行给出（``self_lines``），每行带上它引用的网上生活条目的键：xiaoqing_life 这一轮已经
  把其中某一条的细节交给了聊天，聊天这一轮就不放这一行（只引用网上生活的行；一句话简介总是保留）。
- 页面没引用的笔记，只有页面上次重写之后才记下的才列为“刚记下”；重写时就有的是写页面时判断
  重复或不值得写的。时间一律按原话说出的时间（``observed_at``），迁移和夜间整理晚写入的不算新。
- ``task``：agent 开新办事会话。发起人的页面里稳定的事、办事偏好和别主动提的事，以及在这里答应过
  他的事；来源链里有网页文字的内容一律不给。
- ``proactive``：主动开口。她的近况和自我页、群页（群聊时带上这个群常聊的话题词），给了说话人时
  还有这个人到期的话头与页面。
- ``self``：只要她自己的近况，交给 agent 的当轮材料用。

每块有字数预算，整行取舍；排序看与这句话的字面重合、最近印证时间和取用次数。她最近几条回复里
说过的内容不再给。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from . import topics as topics_mod
from .config import MemoryConfig
from .model import DAY, GLOBAL, SELF_KINDS, Event, Note, Page, PageLine, supported
from .render import ago, brief_text, echoed, fit, note_line, page_lines, speaker
from .scopes import (
    AGENT,
    SELF_SUBJECT,
    event_visible,
    group_subject,
    line_visible,
    note_visible,
    person_subject,
    visible_page,
    web_free,
)
from .store import MemoryStore
from .text import clamped, clip, mapping, overlap, squash

REVIEW_WINDOW_SECONDS = 48 * 3600.0
MAX_SURFACES          = 3
_MAX_SELF_ENTRIES     = 12
_MAX_HISTORY          = 20
_MIN_HIT_OVERLAP      = 2
_EVENT_HIT_DAYS       = 30
_RECENT_WINDOW        = 30
# 明确回指以前的事：只在这时翻迁移来的旧话题摘要。
EXPLICIT_MEMORY_RE = re.compile(
    r"记得|记不记得|还记得|忘了|上次|上回|之前|以前|那次|那天|说过|聊过|提过|讲过|问过"
)
_PERSON_ORDER = ("stable", "between_us", "recent", "errands", "avoid")
_TASK_ORDER   = ("errands", "stable", "between_us", "avoid", "recent")
_GROUP_ORDER  = ("about", "jokes", "topics", "recent")
_SELF_ORDER   = ("doing", "interests", "stories")
_KIND_SHORT   = {kind: kind.split(".", 1)[1] for kind in SELF_KINDS}


@dataclass
class _Request:
    purpose     : str
    scope       : str
    speaker_id  : int
    speaker_name: str
    text        : str
    query       : str
    recent      : list[str]
    bot_recent  : list[str]
    past_days   : float
    followup    : float
    self_limit  : int


@dataclass
class _Picked:
    lines: list[str] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    refs : list[str] = field(default_factory=list)
    rows : list[dict[str, Any]] = field(default_factory=list)


_SELF_HEADER = "你这阵子的样子（你自己的事，整理自你之前说过和做过的）："


def life_keys(notes: Iterable[Note]) -> tuple[list[str], bool]:
    """（引用的网上生活条目在 xiaoqing_life 里的键, 是不是只引用了网上生活条目）。"""

    keys: list[str] = []
    only            = True
    for note in notes:
        source = note.sources[0] if note.sources else {}
        if source.get("type") == "external" and source.get("key"):
            keys.append(str(source["key"]))
        else:
            only = False
    return keys, only and bool(keys)


def _strings(value: Any, limit: int, chars: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clip(item, chars) for item in value[-limit:] if isinstance(item, str) and item.strip()]


def parse_request(payload: Mapping[str, Any], scope: str) -> _Request:
    speaker = mapping(payload.get("speaker"))
    try:
        speaker_id = int(speaker.get("user_id") or 0)
    except (TypeError, ValueError):
        speaker_id = 0
    purpose = str(payload.get("purpose") or "reply")
    return _Request(
        purpose      = purpose if purpose in ("reply", "task", "proactive", "self") else "reply",
        scope        = scope,
        speaker_id   = max(0, speaker_id),
        speaker_name = clip(speaker.get("name"), 32),
        text         = clip(payload.get("text"), 600),
        query        = clip(payload.get("query"), 300),
        recent       = _strings(payload.get("recent"), _RECENT_WINDOW),
        bot_recent   = _strings(payload.get("bot_recent"), 8),
        past_days    = clamped(payload.get("past_days"), 0.0, 0.0, 14.0),
        followup     = clamped(payload.get("followup_hours"), 36.0, 0.0, 24.0 * 14) * 3600.0,
        self_limit   = int(clamped(payload.get("self_limit"), 8, 0, _MAX_SELF_ENTRIES)),
    )


def _group_scopes(store: MemoryStore) -> list[str]:
    return [scope for scope in store.known_scopes() if scope.startswith("g")]


def visible_notes(store: MemoryStore, scope: str) -> dict[str, Note]:
    """读取方在 ``scope`` 时能看到的全部笔记（含已结束的，用于判断页面行是否还有依据）。"""

    scopes = [scope, GLOBAL]
    if scope.startswith("u"):
        scopes += _group_scopes(store)
    return {
        note_id: note
        for note_id, note in store.notes_for(scopes).items()
        if note_visible(note, scope)
    }


def live_page(page: Page | None, notes: Mapping[str, Note], reader: str) -> Page | None:
    if page is None:
        return None
    shown = visible_page(page, notes, reader)
    if shown is None:
        return None
    sections = {
        name: tuple(
            line
            for line in items
            if line_visible(line, notes, reader) and supported(line.note_ids, notes)
        )
        for name, items in shown.sections.items()
    }
    brief_ok = bool(shown.brief) and supported(shown.brief_note_ids, notes)
    if not any(sections.values()) and not brief_ok:
        return None
    return replace(
        shown,
        sections       = sections,
        brief          = shown.brief if brief_ok else "",
        brief_note_ids = shown.brief_note_ids if brief_ok else (),
    )


def person_pages(
    store: MemoryStore, scope: str, user_id: int, notes: Mapping[str, Note]
) -> list[Page]:
    subject = person_subject(user_id)
    pages   = [store.page(scope, subject)]
    if scope.startswith("u"):
        pages += [store.page(group, subject) for group in _group_scopes(store)]
    return [page for item in pages if (page := live_page(item, notes, scope)) is not None]


def said_elsewhere(source_scope: str, reader_scope: str) -> str:
    """私聊里读到的群里的事：标出是在群里说的，原话里“在群里”这类范围才不会被当成对私聊也一样。"""

    if source_scope != reader_scope and source_scope.startswith("g"):
        return "在群里说的"
    return ""


def _cited(pages: Iterable[Page]) -> set[str]:
    found: set[str] = set()
    for page in pages:
        found |= page.note_ids()
    return found


def said_at(note: Note) -> float:
    """这件事是什么时候说的（迁移导入和夜间整理写下的笔记，写入时间晚于原话）。"""

    return note.observed_at or note.created_at


def unwritten(store: MemoryStore, note: Note) -> bool:
    """页面上次重写之后才记下的笔记。

    重写时就有、却没写进页面的，是写页面时判断重复或不值得写的，不单独列出。
    """

    page = store.page(note.scope, note.subject)
    return page is None or note.created_at > page.updated_at


def _fresh_person_notes(
    store: MemoryStore,
    notes: Mapping[str, Note],
    subject: str,
    cited: set[str],
    now: float,
    kinds: Iterable[str],
) -> list[Note]:
    allowed = set(kinds)
    return sorted(
        (
            note
            for note in notes.values()
            if note.subject == subject
            and note.kind in allowed
            and note.is_active(now)
            and note.id not in cited
            and unwritten(store, note)
        ),
        key     = said_at,
        reverse = True,
    )


def _person_block(
    store: MemoryStore,
    cfg: MemoryConfig,
    req: _Request,
    notes: Mapping[str, Note],
    now: float,
    *,
    order: Sequence[str],
    kinds: Sequence[str],
    budget: int,
    header: str,
) -> _Picked:
    picked = _Picked()
    if not req.speaker_id or budget <= 0:
        return picked
    pages = person_pages(store, req.scope, req.speaker_id, notes)
    rows   : list[str] = []
    sources: list[tuple[str, list[Note], str]] = []
    for page in pages:
        # 别的范围的页面在这里读时不带简介（见 visible_page），只有行要标出在哪说的。
        where = said_elsewhere(page.scope, req.scope)
        if page.brief:
            brief = brief_text(page, notes, now, cfg.stale_days)
            rows.append(brief)
            sources.append(
                (
                    brief,
                    [notes[i] for i in page.brief_note_ids if i in notes],
                    f"page:{page.scope}:{page.subject}#brief",
                )
            )
        for section, text, line in page_lines(
            page, notes, now=now, stale_days=cfg.stale_days, order=order, where=where
        ):
            if section not in ("stable", "avoid") and echoed(line.text, req.bot_recent):
                continue
            rows.append(text)
            sources.append(
                (
                    text,
                    [notes[i] for i in line.note_ids if i in notes],
                    f"page:{page.scope}:{page.subject}#{section}",
                )
            )
    for note in _fresh_person_notes(
        store, notes, person_subject(req.speaker_id), _cited(pages), now, kinds
    ):
        if echoed(note.text, req.bot_recent):
            continue
        where = said_elsewhere(note.scope, req.scope)
        text  = f"- [刚记下] {note_line(note, now, cfg.stale_days, where)}"
        rows.append(text)
        sources.append((text, [note], note.id))
    other_names = _other_names(pages, notes, person_subject(req.speaker_id), req.speaker_name)
    if other_names:
        header = f"{header.rstrip('：')}（同一个人，也叫{'、'.join(other_names)}）："
    kept = set(fit(rows, max(0, budget - len(header) - 1)))
    for text, cited_notes, ref in sources:
        if text in kept:
            picked.lines.append(text)
            picked.notes.extend(cited_notes)
            picked.refs.append(ref)
    if picked.lines:
        picked.lines.insert(0, header)
    return picked


def _other_names(
    pages: Sequence[Page], notes: Mapping[str, Note], subject: str, current: str
) -> list[str]:
    """记得的这个人的其他称呼：页面上写的，以及笔记记下的当时的群名片。"""

    found: list[str] = []
    for name in [
        *(name for page in pages for name in page.names),
        *(str(note.data.get("name") or "") for note in notes.values() if note.subject == subject),
    ]:
        name = name.strip()
        if name and name != current and name not in found:
            found.append(name)
    return found[:3]


def _speaker_header(req: _Request) -> str:
    name = req.speaker_name or "这个人"
    return f"你记得的关于{name}的事："


def _group_block(
    store: MemoryStore, cfg: MemoryConfig, req: _Request, notes: Mapping[str, Note], now: float
) -> _Picked:
    picked = _Picked()
    if not req.scope.startswith("g") or cfg.group_budget <= 0:
        return picked
    page = live_page(store.page(req.scope, group_subject(req.scope)), notes, req.scope)
    rows: list[tuple[float, str, PageLine | None, str]] = []
    if page is not None:
        if page.brief:
            rows.append((100.0, brief_text(page, notes, now, cfg.stale_days), None, "brief"))
        for section, text, line in page_lines(
            page, notes, now=now, stale_days=cfg.stale_days, order=_GROUP_ORDER
        ):
            if section == "recent" and echoed(line.text, req.bot_recent):
                continue
            score = overlap(req.text + req.query, line.text) + (2.0 if section == "about" else 0.0)
            rows.append((score, text, line, section))
    cited = page.note_ids() if page is not None else set()
    for note in notes.values():
        if (
            note.kind == "group.fact"
            and note.scope == req.scope
            and note.is_active(now)
            and note.id not in cited
            and unwritten(store, note)
        ):
            rows.append(
                (
                    overlap(req.text + req.query, note.text) + 0.5,
                    f"- [刚记下] {note_line(note, now, cfg.stale_days)}",
                    None,
                    note.id,
                )
            )
    rows.sort(key=lambda row: -row[0])
    header = "这个群（你记得的）："
    kept   = set(fit([row[1] for row in rows], cfg.group_budget - len(header) - 1))
    for _, text, cited_line, ref in rows:
        if text not in kept:
            continue
        picked.lines.append(text)
        if cited_line is not None:
            picked.notes.extend(notes[i] for i in cited_line.note_ids if i in notes)
        elif ref in notes:
            picked.notes.append(notes[ref])
        picked.refs.append(f"page:{req.scope}:group#{ref}" if ref not in notes else ref)
    if picked.lines:
        picked.lines.insert(0, header)
    return picked


def _names_of(page: Page, notes: Mapping[str, Note]) -> list[str]:
    names = [name for name in page.names if len(name) >= 2]
    for note in notes.values():
        if note.subject == page.subject:
            name = str(note.data.get("name") or "")
            if len(name) >= 2:
                names.append(name)
    return list(dict.fromkeys(names))


def _event_line(event: Event, now: float) -> str:
    return f"- {ago(max(0.0, now - event.ts))}{speaker(event)}说过：{clip(event.text, 80)}"


def _hits_block(
    store: MemoryStore,
    cfg: MemoryConfig,
    req: _Request,
    notes: Mapping[str, Note],
    now: float,
    shown: set[str],
) -> _Picked:
    picked = _Picked()
    query  = squash(f"{req.text} {req.query}")
    if not query or cfg.hits_budget <= 0:
        return picked
    explicit                                       = EXPLICIT_MEMORY_RE.search(query) is not None
    rows: list[tuple[float, str, list[Note], str]] = []
    # 话里提到了别人：带上这个人在这里的页面的头几行。
    for page in store.pages(req.scope).values():
        if not page.subject.startswith("person:") or page.subject == person_subject(req.speaker_id):
            continue
        live = live_page(page, notes, req.scope)
        if live is None:
            continue
        names = _names_of(live, notes)
        if not any(name in query for name in names):
            continue
        name              = names[0] if names else "这个人"
        brief             = brief_text(live, notes, now, cfg.stale_days) if live.brief else ""
        lines             = [f"关于{name}：{brief}"]
        cited: list[Note] = []
        for _, text, line in page_lines(
            live, notes, now=now, stale_days=cfg.stale_days, order=_PERSON_ORDER
        )[:3]:
            lines.append("  " + text)
            cited.extend(notes[i] for i in line.note_ids if i in notes)
        rows.append((10.0, "\n".join(lines), cited, f"page:{req.scope}:{page.subject}"))
    for note in notes.values():
        if note.id in shown or not note.is_active(now):
            continue
        if note.kind in SELF_KINDS or note.kind in (
            "person.loop",
            "episode",
            "self.diary",
            "world.learning",
        ):
            continue
        if note.kind == "legacy.summary" and not explicit:
            continue
        if note.subject == person_subject(req.speaker_id) and note.kind != "legacy.summary":
            continue
        keywords = " ".join(str(word) for word in note.data.get("keywords") or ())
        score    = float(overlap(query, f"{note.text} {note.data.get('name') or ''} {keywords}"))
        # 明确回指以前的事时，旧话题摘要对上一个词就给。
        needed = 1 if explicit and note.kind == "legacy.summary" else _MIN_HIT_OVERLAP
        if score < needed:
            continue
        if now - said_at(note) < 3 * DAY:
            score += 1.0
        who = str(note.data.get("name") or "")
        text = f"- {who + '：' if who and note.kind.startswith('person') else ''}{note_line(note, now, cfg.stale_days)}"
        rows.append((score, text, [note], note.id))
    # 还没整理成笔记的事：扫整理水位之后的事件，跳过已经在对话窗口里的。
    watermark = int(store.scope_state(req.scope).get("watermark") or 0)
    recent    = set(req.recent)
    for event in store.events.after(req.scope, watermark):
        if not event_visible(event, req.scope) or now - event.ts > _EVENT_HIT_DAYS * DAY:
            continue
        if any(event.text[:40] in line or line[:40] in event.text for line in recent if line):
            continue
        score = float(overlap(query, event.text))
        if score < _MIN_HIT_OVERLAP + 1:
            continue
        rows.append((score, _event_line(event, now), [], f"e{event.seq}"))
    rows = [row for row in rows if not echoed(row[1], req.bot_recent)]
    rows.sort(key=lambda row: -row[0])
    header = "可能相关的旧事（只在接得上时用，对不上就当没看到）："
    kept   = set(fit([row[1] for row in rows], cfg.hits_budget - len(header) - 1))
    for _, text, cited, ref in rows:
        if text in kept:
            picked.lines.append(text)
            picked.notes.extend(cited)
            picked.refs.append(ref)
    if picked.lines:
        picked.lines.insert(0, header)
    return picked


def _self_page_block(
    store: MemoryStore, cfg: MemoryConfig, req: _Request, notes: Mapping[str, Note], now: float
) -> _Picked:
    picked = _Picked()
    page   = live_page(store.page(GLOBAL, SELF_SUBJECT), notes, req.scope)
    if page is None or cfg.self_budget <= 0:
        return picked
    rows: list[tuple[str, Sequence[str], bool]] = []
    if page.brief:
        rows.append((brief_text(page, notes, now, cfg.stale_days), page.brief_note_ids, False))
    for section, text, line in page_lines(
        page, notes, now=now, stale_days=cfg.stale_days, order=_SELF_ORDER
    ):
        if section != "stories" and echoed(line.text, req.bot_recent):
            continue
        rows.append((text, line.note_ids, True))
    header = _SELF_HEADER
    kept   = set(fit([row[0] for row in rows], cfg.self_budget // 2 - len(header) - 1))
    for text, note_ids, is_line in rows:
        if text not in kept:
            continue
        cited = [notes[i] for i in note_ids if i in notes]
        keys, only_life = life_keys(cited)
        picked.lines.append(text)
        # 一句话简介概括的是整个人，不随某一条经历去掉。
        picked.rows.append({"text": text, "life_keys": keys, "life_only": only_life and is_line})
        if is_line:
            picked.notes.extend(cited)
            picked.refs.append(f"page:global:self#{text[:12]}")
    if picked.lines:
        picked.lines.insert(0, header)
    return picked


def self_entry(note: Note) -> dict[str, Any]:
    return {
        "id"         : note.id,
        "kind"       : _KIND_SHORT.get(note.kind, note.kind),
        "text"       : note.text,
        "ts"         : said_at(note),
        "expires_at" : note.expires_at,
        "status"     : note.status,
        "counterpart": str(note.data.get("counterpart") or ""),
        "chat_id"    : str(note.data.get("origin_scope") or note.scope),
        "shared"     : note.scope == GLOBAL,
        "source"     : str(note.data.get("source") or "chat"),
    }


def self_entries(
    notes: Mapping[str, Note], now: float, limit: int
) -> tuple[list[Note], list[Note]]:
    """（仍然有效的近况、打算、承诺，讲过的往事），旧→新。"""

    active = sorted(
        (note for note in notes.values() if note.kind in SELF_KINDS and note.is_active(now)),
        key=said_at,
    )
    current = [note for note in active if note.kind != "self.story"][-limit:] if limit else []
    stories = [note for note in active if note.kind == "self.story"][-limit:] if limit else []
    return current, stories


def self_history(notes: Mapping[str, Note], since: float) -> list[Note]:
    return sorted(
        (
            note
            for note in notes.values()
            if note.kind in SELF_KINDS and said_at(note) >= since and note.status != "retracted"
        ),
        key=said_at,
    )[-_MAX_HISTORY:]


def loop_entry(note: Note) -> dict[str, Any]:
    return {
        "id"        : note.id,
        "title"     : note.text,
        "user_id"   : int(note.data.get("user_id") or 0),
        "user_name" : str(note.data.get("user_name") or ""),
        "due_at"    : float(note.data.get("due_at") or 0.0),
        "created_at": said_at(note),
        "status"    : "closed" if note.status != "active" else str(note.data.get("loop") or "open"),
        "asked_at"  : float(note.data.get("asked_at") or 0.0),
        "surfaced"  : int(note.data.get("surfaced") or 0),
    }


def due_loop(
    notes: Mapping[str, Note], scope: str, user_id: int, followup: float, now: float
) -> dict[str, Any] | None:
    """说话人此刻可以接的一件事，以及接法：``ask`` 顺口问、``review`` 对方在答。"""

    if not user_id:
        return None
    mine = [
        note
        for note in notes.values()
        if note.kind == "person.loop"
        and note.scope == scope
        and note.status == "active"
        and note.subject == person_subject(user_id)
    ]
    for note in sorted(mine, key=lambda item: float(item.data.get("asked_at") or 0), reverse=True):
        asked = float(note.data.get("asked_at") or 0)
        if note.data.get("loop") == "asked" and 0 <= now - asked <= REVIEW_WINDOW_SECONDS:
            return {**loop_entry(note), "mode": "review"}
    due = [
        note
        for note in mine
        if note.data.get("loop", "open") == "open"
        and int(note.data.get("surfaced") or 0) < MAX_SURFACES
        and 0 <= now - float(note.data.get("due_at") or 0) <= followup
    ]
    if not due:
        return None
    best = max(due, key=lambda item: float(item.data.get("due_at") or 0))
    return {**loop_entry(best), "mode": "ask"}


def _task_promises(notes: Mapping[str, Note], req: _Request, now: float) -> list[str]:
    lines = []
    for note in sorted(notes.values(), key=said_at):
        if note.kind != "self.promise" or note.scope != req.scope or not note.is_active(now):
            continue
        who = str(note.data.get("counterpart") or "")
        lines.append(
            f"- {ago(max(0.0, now - said_at(note)))}答应{who or '对方'}：{note.text}（还没办完）"
        )
    return lines[-4:]


def recall(
    store: MemoryStore,
    cfg: MemoryConfig,
    payload: Mapping[str, Any],
    *,
    caller: str,
    scope: str,
    now: float | None = None,
) -> dict[str, Any]:
    moment                 = time.time() if now is None else now
    req                    = parse_request(payload, scope)
    notes                  = visible_notes(store, scope)
    used  : list[Note]     = []
    refs  : list[str]      = []
    blocks: dict[str, Any] = {}
    result: dict[str, Any] = {"version": 1, "purpose": req.purpose}

    if caller == AGENT or req.purpose == "task":
        # 交给有命令执行能力的 agent：来源链里有网页文字的一律不给。
        safe    = {note_id: note for note_id, note in notes.items() if web_free([note])}
        speaker = _person_block(
            store,
            cfg,
            req,
            safe,
            moment,
            order  = _TASK_ORDER,
            kinds  = ("person.pref",),
            budget = cfg.speaker_budget,
            header = _speaker_header(req),
        )
        promises           = _task_promises(safe, req, moment)
        blocks["speaker"]  = "\n".join(speaker.lines)
        blocks["promises"] = "\n".join(promises)
        used.extend(speaker.notes)
        refs.extend(speaker.refs)
        result["blocks"] = blocks
        result["used"]   = refs
        store.mark_used(used, moment)
        return result

    current, stories = self_entries(notes, moment, req.self_limit)
    result["self_entries"] = [self_entry(note) for note in current]
    result["stories"]      = [self_entry(note) for note in stories]
    if req.purpose == "self":
        result["blocks"] = {}
        return result
    if req.past_days:
        result["self_history"] = [
            self_entry(note) for note in self_history(notes, moment - req.past_days * DAY)
        ]
    due           = due_loop(notes, scope, req.speaker_id, req.followup, moment)
    result["due"] = due

    self_page = _self_page_block(store, cfg, req, notes, moment)
    speaker   = _person_block(
        store,
        cfg,
        req,
        notes,
        moment,
        order  = _PERSON_ORDER,
        kinds  = ("person.fact", "person.pref"),
        budget = cfg.speaker_budget,
        header = _speaker_header(req),
    )
    blocks["self"]    = "\n".join(self_page.lines)
    blocks["speaker"] = "\n".join(speaker.lines)
    if self_page.rows:
        result["self_header"] = _SELF_HEADER
        result["self_lines"]  = self_page.rows
    used.extend(self_page.notes + speaker.notes)
    refs.extend(self_page.refs + speaker.refs)
    group           = _group_block(store, cfg, req, notes, moment)
    blocks["group"] = "\n".join(group.lines)
    used.extend(group.notes)
    refs.extend(group.refs)
    if req.purpose == "proactive" and scope.startswith("g"):
        result["topics"] = topics_mod.heard(store.scope_state(scope).get("topics"), moment)
    if req.purpose == "reply":
        if scope.startswith("g"):
            result["topics"] = topics_mod.fresh(store.scope_state(scope).get("topics"), moment)
        shown          = {note.id for note in used}
        hits           = _hits_block(store, cfg, req, notes, moment, shown)
        blocks["hits"] = "\n".join(hits.lines)
        used.extend(hits.notes)
        refs.extend(hits.refs)
    result["blocks"] = blocks
    result["used"]   = list(dict.fromkeys(refs))
    store.mark_used(used, moment)
    return result
