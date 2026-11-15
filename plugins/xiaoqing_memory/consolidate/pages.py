"""整理第二步：把一个对象的有效笔记并成页面。

模型拿到现有页面和这个对象的全部有效笔记，写出完整的新页面，每行引用笔记 ID。这里核对：引用的
笔记存在、属于这个对象、在页面所在范围可见；小节合法；总字数不超上限。校验不过时保留旧页面。

没有模型预算或调用失败时，新笔记按确定性规则追加到“最近”小节，等下一次成功重写时再合并，
新内容不依赖整理成功也能被取用。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.ai import first_json_object

from ..model import (
    DAY,
    GLOBAL,
    GROUP_SECTIONS,
    PAGE_LIMITS,
    PERSON_SECTIONS,
    SECTION_TITLES,
    SELF_KINDS,
    SELF_SECTIONS,
    Note,
    Page,
    PageLine,
    page_kind,
    supported,
)
from ..scopes import SELF_SUBJECT, note_visible
from ..text import clip, listing, mapping, squash
from .common import PROMPT_VERSION

PAGE_NOTE_KINDS = {
    "person": frozenset({"person.fact", "person.pref", "work.task"}),
    "group" : frozenset({"group.fact"}),
    "self"  : frozenset({*SELF_KINDS, "episode", "self.diary", "world.learning"}),
}
SECTIONS         = {"person": PERSON_SECTIONS, "group": GROUP_SECTIONS, "self": SELF_SECTIONS}
FALLBACK_SECTION = {"person": "recent", "group": "recent", "self": "doing"}
# 每个小节是做什么用的：写进提示词，让模型知道一行该放哪儿。
SECTION_USES = {
    "stable"    : "这个人比较长久的情况：身份、学校专业或工作、住在哪、家里的猫狗、爱好",
    "recent"    : "他这阵子在忙的、有时间点的事",
    "between_us": "你们之间的约定、梗（叫法照他的原话写在引号里）、你欠着他的事，以及他对你说话方式的要求",
    "errands": (
        "你帮他办事时要记得的：他固定的习惯和默认做法、纠正过你的地方、以前办事踩过的坑；"
        "办事记录是一次次的经过，从里面看出来的习惯才写，某一次办了什么不写；同类的合成一行"
    ),
    "avoid"    : "他不想被提起、或者说出来会让他难受的事",
    "about"    : "这个群是什么样的群、谁在里面",
    "topics"   : "大家常聊的话题",
    "jokes"    : "群里的梗和约定；梗里的叫法、口头禅照群里的原话写在引号里，换个说法就接不上了",
    "recent_g" : "群里最近几天的事",
    "doing"    : "你最近在做的事、近况和打算",
    "interests": "你这阵子的兴趣、在网上常看的东西",
    "stories"  : "你讲过的自己以前的事",
}
_MAX_INPUT_NOTES = 80
_SELF_DAYS       = 21

_PURPOSE = {
    "person": (
        "这一页是你对这个人的了解，每次和他说话前都会看一眼，所以要短。同一件事只留一行；说法变了就用新的，"
        "旧的留一句“以前是……”；拿不准的写明拿不准。"
    ),
    "group": (
        "这一页是你对这个群的了解，每次在群里说话前都会看一眼，所以要短：这个群是什么样的、大家常聊什么、"
        "群里的梗和约定、最近几天的事。同一件事只留一行；过时的事删掉。"
    ),
    "self": (
        "这一页是你对自己这阵子的了解，每次说话前都会看一眼，让你说的话前后一致，所以要短：最近在做什么、"
        "这阵子的兴趣、你讲过的自己以前的事（讲过就是你的经历，再被问到还是这个版本）。同一件事只留一行；"
        "已经过去的近况删掉。"
    ),
}

SYSTEM = """你是{bot}。{purpose}

下面给你现有的页面和全部相关笔记（每条有 id）。写出完整的新页面：每一行用你自己的话写，必须在 notes 里列出它依据的笔记 id；没有笔记依据的内容不要写。整页（简介加所有行）不超过 {limit} 字。
时间写成具体日期，不写“明天”“周五”这类说法（笔记的 date 是说这话的那天）；写着日子的事（带 until 的、行程、活动、截止）过了日子，就改成已经发生的说法或者删掉。
笔记里的条件和范围（在群里、某段时间、对某件事）照写，别写成笼统的说法。

小节：
{sections}

只输出一个 JSON 对象：
{{"names":["这个人常用的称呼"],"brief":"一句话简介","brief_notes":["n..."],"sections":{{{example}}}}}"""


def subject_notes(
    notes: Mapping[str, Note], page_scope: str, subject: str, now: float
) -> list[Note]:
    kind    = page_kind(subject)
    allowed = PAGE_NOTE_KINDS.get(kind, frozenset())
    found   = [
        note
        for note in notes.values()
        if note.subject == subject
        and note.kind in allowed
        and note.status in ("active", "superseded")
        and note_visible(note, page_scope)
        and (note.scope == page_scope or kind == "self")
    ]
    if kind == "self":
        # 自我页只看近三周的经历和近况；讲过的往事一直算。
        found = [
            note
            for note in found
            if note.kind == "self.story"
            or now - max(note.observed_at, note.created_at) < _SELF_DAYS * DAY
        ]
    return sorted(found, key=lambda note: note.observed_at)[-_MAX_INPUT_NOTES:]


def _note_row(note: Note, zone: ZoneInfo) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id"  : note.id,
        "kind": note.kind,
        "text": note.text,
        "date": datetime.fromtimestamp(note.observed_at or note.created_at, zone).strftime(
            "%Y-%m-%d"
        ),
    }
    if note.status == "superseded":
        row["status"] = "以前的说法，已被新的替换"
    if note.usage != "normal":
        row["usage"] = note.usage
    if note.confidence in ("hypothesis", "reported", "legacy"):
        row["confidence"] = note.confidence
    if note.kind in ("episode", "world.learning", "self.diary"):
        row["from"] = "你上网的经历" if note.kind != "self.diary" else "你的日记"
    if note.kind == "work.task":
        row["from"] = "一次办事的记录"
    if note.ends_at():
        row["until"] = datetime.fromtimestamp(note.ends_at() - 1, zone).strftime("%Y-%m-%d")
    return row


def _page_rows(page: Page | None) -> dict[str, Any]:
    if page is None:
        return {}
    return {
        "names"   : list(page.names),
        "brief"   : page.brief,
        "sections": {
            name: [{"text": line.text, "notes": list(line.note_ids)} for line in items]
            for name, items in page.sections.items()
        },
    }


def build_messages(
    *,
    bot_name: str,
    subject: str,
    page: Page | None,
    notes: Sequence[Note],
    zone: ZoneInfo,
    names: Sequence[str] = (),
    today: str           = "",
) -> list[dict[str, str]]:
    kind     = page_kind(subject)
    sections = SECTIONS[kind]
    system   = SYSTEM.format(
        bot      = bot_name,
        purpose  = _PURPOSE[kind],
        limit    = PAGE_LIMITS[kind],
        sections = "\n".join(
            f"- {name}（{SECTION_TITLES[name]}）："
            + SECTION_USES.get(
                f"{name}_g" if kind == "group" else name,
                SECTION_USES.get(name, SECTION_TITLES[name]),
            )
            for name in sections
        ),
        example=",".join(f'"{name}":[{{"text":"...","notes":["n..."]}}]' for name in sections[:2]),
    )
    who   = {"person": "这个人", "group": "这个群", "self": "你自己"}[kind]
    parts = [
        *([f"今天是 {today}"] if today else []),
        f"对象：{who}" + (f"（别人这样叫：{'、'.join(names)}）" if names else ""),
        "现有页面：" + json.dumps(_page_rows(page), ensure_ascii=False),
        "笔记：" + json.dumps([_note_row(note, zone) for note in notes], ensure_ascii=False),
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def parse_page(
    raw: str,
    *,
    subject: str,
    scope: str,
    notes: Sequence[Note],
    old: Page | None,
    now: float,
) -> tuple[Page | None, str]:
    """校验模型写的页面；不合格时返回（None, 原因），调用方保留旧页面。"""

    data = first_json_object(raw) or {}
    if not data:
        return None, "not_json"
    kind    = page_kind(subject)
    allowed = {note.id: note for note in notes}
    limit   = PAGE_LIMITS[kind]

    def cited(value: Any) -> tuple[str, ...] | None:
        ids = [str(item) for item in value] if isinstance(value, list) else []
        if not ids or any(note_id not in allowed for note_id in ids):
            return None
        return tuple(dict.fromkeys(ids))

    sections_raw                              = mapping(data.get("sections"))
    sections: dict[str, tuple[PageLine, ...]] = {}
    for name in SECTIONS[kind]:
        items = listing(sections_raw.get(name))
        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = squash(item.get("text"))
            ids  = cited(item.get("notes"))
            if not text or ids is None:
                # 没有出处的行整页作废，不部分写入。
                return None, "uncited_line"
            dates = [allowed[note_id].observed_at for note_id in ids]
            lines.append(
                PageLine(text=clip(text, 120), note_ids=ids, since=min(dates), last=max(dates))
            )
        sections[name] = tuple(lines)
    unknown = set(sections_raw) - set(SECTIONS[kind])
    if unknown:
        return None, "unknown_section"
    brief     = squash(data.get("brief"))[:120]
    brief_ids = cited(data.get("brief_notes")) if brief else ()
    if brief and brief_ids is None:
        return None, "uncited_brief"
    names = listing(data.get("names"))
    page  = Page(
        subject        = subject,
        scope          = scope,
        names          = tuple(clip(name, 24) for name in names if str(name).strip())[:6],
        brief          = brief,
        brief_note_ids = brief_ids or (),
        sections       = sections,
        version        = (old.version if old else 0) + 1,
        updated_at     = now,
        prompt_version = PROMPT_VERSION,
    )
    if page.char_count() > limit:
        return None, "over_limit"
    if not page.lines() and not page.brief:
        return None, "empty"
    return page, ""


def append_fallback(
    page: Page | None, subject: str, scope: str, fresh: Sequence[Note]
) -> Page | None:
    """确定性追加：没被页面引用的新笔记各占一行放进“最近”，超出上限时先丢最旧的追加行。"""

    if not fresh:
        return page
    kind    = page_kind(subject)
    section = FALLBACK_SECTION[kind]
    base = page or Page(subject=subject, scope=scope, sections={}, updated_at=0.0)
    cited = base.note_ids()
    lines = list(base.sections.get(section, ()))
    for note in fresh:
        if note.id in cited:
            continue
        lines.append(
            PageLine(
                text=note.text, note_ids=(note.id,), since=note.observed_at, last=note.observed_at
            )
        )
    candidate = replace(
        base, subject=subject, scope=scope, sections={**base.sections, section: tuple(lines)}
    )
    while candidate.char_count() > PAGE_LIMITS[kind] and candidate.sections[section]:
        trimmed = {**candidate.sections, section: candidate.sections[section][1:]}
        candidate = replace(candidate, sections=trimmed)
    return candidate


def drop_dead_lines(page: Page, notes: Mapping[str, Note]) -> Page | None:
    """去掉引用的笔记都已不在的行（被清理、撤回或替换掉）；整页空了返回 ``None``。"""

    sections = {
        name: tuple(line for line in items if supported(line.note_ids, notes))
        for name, items in page.sections.items()
    }
    brief_ok = bool(page.brief) and supported(page.brief_note_ids, notes)
    if not any(sections.values()) and not brief_ok:
        return None
    return replace(
        page,
        sections       = sections,
        brief          = page.brief if brief_ok else "",
        brief_note_ids = page.brief_note_ids if brief_ok else (),
    )


def pending_subjects(
    notes: Mapping[str, Note], pages: Mapping[str, Page], scope: str, now: float = 0.0
) -> list[tuple[str, int]]:
    """要重写页面的对象，按待写笔记数从多到少：有新笔记还没写进页面，或者页面上的事写着的日子
    在上次重写之后过去了（要改成过去的事或删掉）。"""

    counts: dict[str, int] = {}
    for note in notes.values():
        if note.status != "active" or note.scope != scope:
            continue
        kind = page_kind(note.subject)
        if kind not in ("person", "group") or note.kind not in PAGE_NOTE_KINDS[kind]:
            continue
        page = pages.get(note.subject)
        if page is not None and (note.id in page.note_ids() or note.created_at <= page.updated_at):
            # 已经写进页面，或者写页面时就有了（模型判断不值得写）。
            passed = note.ends_at() and page.updated_at < note.ends_at() <= now
            if page.prompt_version >= PROMPT_VERSION and not (
                passed and note.id in page.note_ids()
            ):
                continue
        counts[note.subject] = counts.get(note.subject, 0) + 1
    return sorted(counts.items(), key=lambda item: -item[1])


def self_pending(notes: Mapping[str, Note], page: Page | None) -> int:
    updated = page.updated_at if page is not None else 0.0
    return sum(
        1
        for note in notes.values()
        if note.scope == GLOBAL
        and note.subject == SELF_SUBJECT
        and note.status == "active"
        and note.created_at > updated
    )
