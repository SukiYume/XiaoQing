"""整理第三步：对照原文核对页面上的行。

每晚抽几行还有没核对过的依据的行：取用次数多的、可信度低的、最久没核对的优先。把这一行、它引用的
原始记录和原话前面的几条聊天一起交给模型，判断原文是否支持这一行：

- 支持：依据的笔记记为核对过。
- 部分支持：行改成按原文改好的说法；只依据一条笔记时笔记也改成这个说法，下次重写页面不会再写回去。
- 不支持：删掉这一行，把还没核对过的依据撤回；核对过的笔记不因为后来的一次判断被撤回。

依据都核对过的行不再核对，同一行不会夜夜重核、被判定的随机性慢慢删掉。迁移来的旧数据、网上经历
和工具记下的办事记录没有要核对的聊天原文，不参加核对。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from zoneinfo import ZoneInfo

from core.ai import first_json_object

from ..events import EventLog
from ..model import DEFAULT_TEXT_LIMIT, Event, Note, Page, PageLine
from ..text import clip, listing, squash
from .common import local_time

LINES_PER_CALL  = 4
_CONTEXT_BEFORE = 3

SYSTEM = """你是{bot}。下面是你记忆里的几行话，每行附着它依据的原始聊天记录（original），以及原话前面的几条聊天（before，帮你看懂原话在接什么、指的是什么）；每条记录前有说话的日期和星期，原话里的“明天”“两周后”按那条记录的时间推算。这些话每次说话前你都会看到，写错了会一直错下去，所以请对照原文逐行看：

- supported：原文支持这一行。
- partly：原文只支持一部分，或者说法走样了。在 rewrite 里给出按原文改好的一句（不超过 40 字）。
- unsupported：原文不支持这一行（张冠李戴、把玩笑当真、把别人的事安到这个人头上、凭空多出来的细节）。

只输出一个 JSON 对象：{{"results":[{{"line":"L1","verdict":"supported","rewrite":""}}]}}"""


@dataclass(frozen=True)
class Candidate:
    scope  : str
    subject: str
    section: str
    index  : int
    line   : PageLine
    notes  : tuple[Note, ...]
    events : tuple[Event, ...]
    context: tuple[Event, ...] = ()


def _priority(candidate: Candidate) -> tuple[float, ...]:
    uses = sum(note.use_count for note in candidate.notes)
    low  = any(note.confidence in ("hypothesis", "reported") for note in candidate.notes)
    verified = min((note.verified_at for note in candidate.notes), default=0.0)
    return (-uses, -float(low), verified)


def checked(note: Note) -> bool:
    """核对过，或者本来就是工具记下的事实。"""

    return bool(note.verified_at) or note.confidence == "verified"


def _context(events: EventLog, cited: Sequence[Event]) -> tuple[Event, ...]:
    """每条原话前面同一范围里的几条聊天，不含原话本身。"""

    seen                                = {(event.scope, event.seq) for event in cited}
    found: dict[tuple[str, int], Event] = {}
    for event in cited:
        for seq in range(event.seq - _CONTEXT_BEFORE, event.seq):
            key = (event.scope, seq)
            if seq <= 0 or key in seen or key in found:
                continue
            before = events.get(event.scope, seq)
            if before is not None:
                found[key] = before
    return tuple(found[key] for key in sorted(found))[-_CONTEXT_BEFORE * 2 :]


def candidates(
    pages: Mapping[str, Page], notes: Mapping[str, Note], events: EventLog, *, limit: int
) -> list[Candidate]:
    found: list[Candidate] = []
    for page in pages.values():
        for section, index, line in page.lines():
            cited = tuple(notes[note_id] for note_id in line.note_ids if note_id in notes)
            if not cited or any(note.confidence == "legacy" for note in cited):
                continue
            if all(checked(note) for note in cited):
                continue
            sources = [
                source for note in cited for source in note.sources if source.get("type") == "event"
            ]
            if not sources:
                continue
            found_events = []
            for source in sources:
                event = events.get(
                    str(source.get("scope") or page.scope), int(source.get("seq") or 0)
                )
                if event is not None:
                    found_events.append(event)
            if not found_events:
                continue
            unique = tuple({(event.scope, event.seq): event for event in found_events}.values())
            found.append(
                Candidate(
                    scope   = page.scope,
                    subject = page.subject,
                    section = section,
                    index   = index,
                    line    = line,
                    notes   = cited,
                    events  = unique,
                    context = _context(events, unique[:4]),
                )
            )
    found.sort(key=_priority)
    return found[:limit]


def build_messages(
    bot_name: str, batch: Sequence[Candidate], zone: ZoneInfo | None = None
) -> list[dict[str, str]]:
    def said(event: Event, limit: int) -> str:
        who  = event.name or ("你" if event.role == "assistant" else "有人")
        when = f"[{local_time(event.ts, zone)}] " if zone is not None else ""
        return f"{when}{who}：{clip(event.text, limit)}"

    rows = []
    for number, candidate in enumerate(batch, start=1):
        row: dict[str, Any] = {
            "line"    : f"L{number}",
            "text"    : candidate.line.text,
            "original": [said(event, 300) for event in candidate.events[:4]],
        }
        if candidate.context:
            row["before"] = [said(event, 160) for event in candidate.context]
        rows.append(row)
    return [
        {"role": "system", "content": SYSTEM.format(bot=bot_name)},
        {"role": "user", "content": json.dumps(rows, ensure_ascii=False)},
    ]


def parse(raw: str, batch: Sequence[Candidate]) -> list[tuple[Candidate, str, str]]:
    """返回（行，判断，改写）；判断只取 supported / partly / unsupported。"""

    data    = first_json_object(raw) or {}
    results = []
    by_name = {f"L{number}": candidate for number, candidate in enumerate(batch, start=1)}
    for item in listing(data.get("results")):
        if not isinstance(item, Mapping):
            continue
        candidate = by_name.get(str(item.get("line") or ""))
        verdict   = str(item.get("verdict") or "")
        if candidate is None or verdict not in ("supported", "partly", "unsupported"):
            continue
        # 单条笔记的说法也会改成这一句，所以不超过笔记文字的默认上限。
        rewrite = squash(item.get("rewrite"))[:DEFAULT_TEXT_LIMIT]
        if verdict == "partly" and not rewrite:
            continue
        results.append((candidate, verdict, rewrite))
    return results


@dataclass
class Applied:
    page     : Page
    confirmed: set[str] = field(default_factory=set)
    retract  : set[str] = field(default_factory=set)
    corrected: dict[str, str] = field(default_factory=dict)


def apply_verdicts(page: Page, verdicts: Sequence[tuple[Candidate, str, str]]) -> Applied:
    """把判断落到页面上：核对通过的笔记、要撤回的笔记、按原文改了说法的笔记（笔记 ID → 新说法）。"""

    sections                       = {name: list(items) for name, items in page.sections.items()}
    confirmed: set[str]            = set()
    retract  : set[str]            = set()
    corrected: dict[str, str]      = {}
    removals : dict[str, set[int]] = {}
    for candidate, verdict, rewrite in verdicts:
        if candidate.scope != page.scope or candidate.subject != page.subject:
            continue
        items = sections.get(candidate.section)
        if (
            items is None
            or candidate.index >= len(items)
            or items[candidate.index] != candidate.line
        ):
            continue
        if verdict == "supported":
            confirmed.update(candidate.line.note_ids)
        elif verdict == "partly":
            items[candidate.index] = PageLine(
                text     = rewrite,
                note_ids = candidate.line.note_ids,
                since    = candidate.line.since,
                last     = candidate.line.last,
            )
            confirmed.update(candidate.line.note_ids)
            if len(candidate.notes) == 1:
                corrected[candidate.notes[0].id] = rewrite
        else:
            removals.setdefault(candidate.section, set()).add(candidate.index)
            # 已经核对过的笔记不因为这一次判断被撤回，只是这一行不再这样写。
            retract.update(note.id for note in candidate.notes if not checked(note))
    for section, indexes in removals.items():
        sections[section] = [
            line for index, line in enumerate(sections[section]) if index not in indexes
        ]
    updated = replace(page, sections={k: tuple(v) for k, v in sections.items()})
    return Applied(updated, confirmed - retract, retract, corrected)


def verdict_counts(verdicts: Sequence[tuple[Candidate, str, str]]) -> dict[str, Any]:
    counts = {"verify.lines": len(verdicts)}
    for _, verdict, _ in verdicts:
        counts[f"verify.{verdict}"] = counts.get(f"verify.{verdict}", 0) + 1
    return counts
