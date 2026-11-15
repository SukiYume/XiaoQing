"""整理第一步：从水位之后的事件写笔记。

模型读一批带编号的原始记录，给出新笔记、再次印证的旧笔记和说法变了的旧笔记，每条都引用事件编号
并附逐字摘录。这里逐条核对：编号在这一批里；摘录是那条原文里的连续片段；关于某人的笔记，引用的
必须是这个人自己说的话（或者替他办的事）；类型在白名单里。和已有笔记说的是同一件事时只更新印证
时间，不新增。

群聊里顺带给出这段在聊的话题词（``topics``），只核对出处编号在这一批里，见 ``topics.py``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from .. import topics as topics_mod
from ..model import DAY, QUOTE_LIMIT, USAGES, Event, Note, new_note_id, text_limit
from ..scopes import group_subject, person_subject
from ..text import clip, listing, quote_in, similarity, squash
from .common import ENDED_KEEP_DAYS, PROMPT_VERSION, day_end, event_line, seq_of

EXTRACT_KINDS = frozenset({"person.fact", "person.pref", "group.fact"})
_SAME_NOTE    = 0.6
_MAX_NOTES    = 30
_KNOWN_NOTES  = 25
_MAX_TOPICS   = 6

SYSTEM = """你是{bot}。下面是你最近在{place}里的一段聊天记录（还有你帮人办事的经过），每条前面有编号 e<数字>。

这份记忆是给以后的你用的：下次和这个人聊天、帮他办事时，哪些事不记下来就会忘，记得就能接得上。聊天记录本身会留着，可以再查，所以不用复述发生了什么，记的是以后用得上的那部分——这个人是什么样的人、他的情况和近况、他在意什么、你们之间的约定和梗、办事时他习惯怎么做；群里的话，还有这个群是什么样的、大家常聊什么、群里的梗。

已经记过的事在“已有记忆”里。同一件事又被提到，放进 same；说法变了（搬家了、换工作了、改主意了），放进 changed，写新的说法；只有真正新的事才放进 notes。没什么值得记的就都给空数组。

每条都要给出处：source 是那条记录的编号，quote 是从那条原文里原样摘的一小段（不改字、不拼接，10 到 30 字）。关于某个人的事，出处必须是他本人说的话，或者你替他办的那件事。

只输出一个 JSON 对象：
{{"notes":[{{"kind":"person.fact","about":123,"text":"...","attribute":"","usage":"normal","until":"","source":"e12","quote":"..."}}],
 "same":[{{"id":"n...","source":"e15","quote":"..."}}],
 "changed":[{{"id":"n...","text":"新的说法","until":"","source":"e20","quote":"..."}}]{topics_shape}}}

kind：person.fact 是关于这个人自己的事；person.pref 是以后帮他办事还用得上的：固定的习惯和默认做法、他纠正过你的地方、办事时踩过的坑——某一次具体办了什么不用记，办事记录会留着；group.fact 是关于这个群的事（about 不填）。
about 是那个人的 QQ 号（尖括号里的数字）。text 用你自己的话写成一句，不超过 40 字，写清是谁的事；原话里的条件和范围（在群里、某段时间、对某件事）照写，别写成笼统的说法。attribute 在这件事是住址、学校、专业、年级、工作、城市这类同一时间只有一个答案的属性时填属性名，否则留空。
usage：normal 以后可以自然提起；tone 影响你和他说话的方式和分寸，不必说出来；avoid 是他不想被提起、或者说出来会让他难受的事，除非他自己提起，你不主动说。
时间写成具体日期（每条记录前有日期和星期），不写“明天”“下周五”这类说法。until：这件事过了某一天就不再是眼下的事（行程、活动、考试、截止日期、约好的时间）时，填那一天 YYYY-MM-DD，否则留空。{topics_rule}"""

_TOPICS_SHAPE = """,
 "topics":[{"topic":"...","keywords":["..."],"usage":"normal","source":"e12"}]"""
_TOPICS_RULE = """
topics：这段里群友在聊的话题（不超过 12 个字）和 1 到 3 个关键词（每个不超过 8 个字），你上网时会拿来当方向，所以写成能拿去搜的说法（具体的作品、游戏、事件，而不是“闲聊”“吐槽”）；source 是群友聊到它的一条消息的编号（机器人推送的内容不算）；usage 同上，吵架、伤心事、谁的私事标 tone 或 avoid。没聊什么具体话题就给空数组。"""


@dataclass
class ExtractResult:
    added     : list[Note] = field(default_factory=list)
    touched   : list[Note] = field(default_factory=list)
    superseded: list[Note] = field(default_factory=list)
    topics    : list[dict[str, Any]] = field(default_factory=list)
    rejected  : dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def batches(events: Sequence[Event], max_chars: int) -> list[list[Event]]:
    groups : list[list[Event]] = []
    current: list[Event]       = []
    size                       = 0
    for event in events:
        cost = min(len(event.text), 800) + 30
        if current and size + cost > max_chars:
            groups.append(current)
            current, size = [], 0
        current.append(event)
        size += cost
    if current:
        groups.append(current)
    return groups


def _subject_of(event: Event) -> str:
    return person_subject(event.user_id) if event.user_id else ""


def build_messages(
    *,
    bot_name: str,
    scope: str,
    batch: Sequence[Event],
    known: Sequence[Note],
    pages_text: str,
    zone: ZoneInfo,
    today: str = "",
) -> list[dict[str, str]]:
    place      = "这个群" if scope.startswith("g") else "这段私聊"
    lines      = [event_line(event, zone, bot_name) for event in batch]
    known_rows = [
        {"id": note.id, "about": note.subject, "kind": note.kind, "text": note.text}
        for note in known[:_KNOWN_NOTES]
    ]
    user = "\n\n".join(
        part
        for part in (
            f"现在是 {today}" if today else "",
            "你对这些人的了解（整理过的页面）：\n" + pages_text if pages_text else "",
            "已有记忆：" + json.dumps(known_rows, ensure_ascii=False)
            if known_rows
            else "已有记忆：[]",
            "聊天记录：\n" + "\n".join(lines),
        )
        if part
    )
    group  = scope.startswith("g")
    system = SYSTEM.format(
        bot          = bot_name,
        place        = place,
        topics_shape = _TOPICS_SHAPE if group else "",
        topics_rule  = _TOPICS_RULE if group else "",
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _cited(batch_by_seq: Mapping[int, Event], item: Mapping[str, Any]) -> tuple[Event | None, str]:
    event = batch_by_seq.get(seq_of(item.get("source")))
    if event is None:
        return None, "source_not_in_batch"
    quote = squash(item.get("quote"))
    if not quote_in(quote, event.text):
        return None, "quote_not_found"
    return event, ""


def _source(event: Event) -> dict[str, Any]:
    return {"type": "event", "scope": event.scope, "seq": event.seq, "origin": event.origin}


def _resolve_subject(
    item: Mapping[str, Any], kind: str, event: Event, scope: str
) -> tuple[str, str, str, str] | str:
    """（类型，对象，可信度，来源），或者拒绝的原因。"""

    if kind == "group.fact":
        if not scope.startswith("g"):
            return "group_fact_outside_group"
        return kind, group_subject(scope), "reported", event.origin
    try:
        about = int(item.get("about") or 0)
    except (TypeError, ValueError):
        about = 0
    # 关于某人的事，出处必须是他本人说的话，或者替他办的那件事。
    if about <= 0 or about != event.user_id:
        return "not_speaker"
    if event.kind == "message" and event.role != "user":
        return "not_speaker"
    return kind, person_subject(about), "stated", "user" if event.kind == "message" else "tool"


def parse(
    raw: Mapping[str, Any],
    *,
    scope: str,
    batch: Sequence[Event],
    known: Mapping[str, Note],
    now: float,
    zone: ZoneInfo | None = None,
) -> ExtractResult:
    result            = ExtractResult()
    batch_by_seq      = {event.seq: event for event in batch}
    fresh: list[Note] = []

    def similar_active(subject: str, kind: str, text: str) -> Note | None:
        for note in [*known.values(), *fresh]:
            if (
                note.subject == subject
                and note.kind == kind
                and note.status == "active"
                and similarity(note.text, text) >= _SAME_NOTE
            ):
                return note
        return None

    def touch(note: Note, event: Event, quote: str) -> None:
        sources = [*note.sources, _source(event)][-6:]
        changed = note.with_changes(
            observed_at=max(note.observed_at, event.ts), sources=tuple(sources)
        )
        if note.quote == "" and quote:
            changed = changed.with_changes(quote=quote)
        result.touched.append(changed)

    for item in listing(raw.get("notes"))[:_MAX_NOTES]:
        if not isinstance(item, Mapping):
            result.reject("not_object")
            continue
        kind = str(item.get("kind") or "")
        if kind not in EXTRACT_KINDS:
            result.reject("kind")
            continue
        event, reason = _cited(batch_by_seq, item)
        if event is None:
            result.reject(reason)
            continue
        text = squash(item.get("text"))[: text_limit(kind)]
        if not text:
            result.reject("empty")
            continue
        quote    = squash(item.get("quote"))[:QUOTE_LIMIT]
        resolved = _resolve_subject(item, kind, event, scope)
        if isinstance(resolved, str):
            result.reject(resolved)
            continue
        kind, subject, confidence, origin = resolved
        same = similar_active(subject, kind, text)
        if same is not None:
            touch(same, event, quote)
            continue
        usage                = str(item.get("usage") or "normal")
        attribute            = clip(item.get("attribute"), 12)
        data: dict[str, Any] = {"name": event.name} if event.name and kind != "group.fact" else {}
        if kind == "group.fact" and event.name:
            data["speaker"] = event.name
        if attribute:
            data["attribute"] = attribute
        ends = day_end(str(item.get("until") or ""), zone, said_at=event.ts) if zone else 0.0
        if ends:
            data["ends_at"] = ends
        note = Note(
            id             = new_note_id(),
            kind           = kind,
            scope          = scope,
            subject        = subject,
            text           = text,
            data           = data,
            origin         = origin,
            confidence     = confidence,
            usage          = usage if usage in USAGES else "normal",
            sources        = (_source(event),),
            quote          = quote,
            created_at     = now,
            observed_at    = event.ts,
            expires_at     = ends + ENDED_KEEP_DAYS * DAY if ends else 0.0,
            prompt_version = PROMPT_VERSION,
        )
        # 同一时间只有一个答案的属性：旧说法被替换，页面里留一句“以前是……”。
        if attribute:
            for old in [*known.values(), *fresh]:
                if (
                    old.subject == subject
                    and old.status == "active"
                    and old.data.get("attribute") == attribute
                    and old.id != note.id
                ):
                    result.superseded.append(old.with_changes(status="superseded", observed_at=now))
                    note = note.with_changes(supersedes=old.id)
        fresh.append(note)
        result.added.append(note)

    for item in listing(raw.get("same")):
        if not isinstance(item, Mapping):
            continue
        again = known.get(str(item.get("id") or ""))
        cited, failure = _cited(batch_by_seq, item)
        if again is None or cited is None:
            result.reject(failure or "unknown_note")
            continue
        if again.subject.startswith("person:") and again.subject != _subject_of(cited):
            result.reject("not_speaker")
            continue
        touch(again, cited, squash(item.get("quote"))[:QUOTE_LIMIT])

    for item in listing(raw.get("changed")):
        if not isinstance(item, Mapping):
            continue
        previous = known.get(str(item.get("id") or ""))
        cited, failure = _cited(batch_by_seq, item)
        text = squash(item.get("text"))
        if previous is None or cited is None or not text:
            result.reject(failure or "unknown_note")
            continue
        old, event = previous, cited
        text = text[: text_limit(old.kind)]
        if old.subject.startswith("person:") and old.subject != _subject_of(event):
            result.reject("not_speaker")
            continue
        # 改口时日子按新的说法算：没给新日子就不再算有日子的事。
        ends = day_end(str(item.get("until") or ""), zone, said_at=event.ts) if zone else 0.0
        data = {key: value for key, value in old.data.items() if key != "ends_at"}
        if ends:
            data["ends_at"] = ends
        new = old.with_changes(
            id             = new_note_id(),
            text           = text,
            data           = data,
            expires_at     = ends + ENDED_KEEP_DAYS * DAY if ends else 0.0,
            sources        = (_source(event),),
            quote          = squash(item.get("quote"))[:QUOTE_LIMIT],
            created_at     = now,
            observed_at    = event.ts,
            supersedes     = old.id,
            status         = "active",
            use_count      = 0,
            last_used_at   = 0.0,
            prompt_version = PROMPT_VERSION,
        )
        result.superseded.append(old.with_changes(status="superseded", observed_at=now))
        result.added.append(new)

    if scope.startswith("g"):
        _collect_topics(raw, batch_by_seq, result)
    return result


def _collect_topics(
    raw: Mapping[str, Any], batch_by_seq: Mapping[int, Event], result: ExtractResult
) -> None:
    for item in listing(raw.get("topics"))[:_MAX_TOPICS]:
        cited = batch_by_seq.get(seq_of(item.get("source"))) if isinstance(item, Mapping) else None
        if cited is None:
            result.reject("topic_source")
            continue
        # 话题词是“朋友们在聊什么”：出处得是群友自己说的话。
        if cited.kind != "message" or cited.role != "user" or cited.origin != "user":
            result.reject("topic_not_from_friends")
            continue
        topic = topics_mod.clean(item, cited.ts)
        if topic is None:
            result.reject("topic_empty")
            continue
        result.topics.append(topic)


def known_notes(notes: Mapping[str, Note], scope: str, batch: Sequence[Event]) -> dict[str, Note]:
    """这一批里出现的人（和这个群）已有的有效笔记，给模型对照用。"""

    subjects = {_subject_of(event) for event in batch if event.user_id}
    if scope.startswith("g"):
        subjects.add(group_subject(scope))
    return {
        note_id: note
        for note_id, note in notes.items()
        if note.scope == scope
        and note.subject in subjects
        and note.kind in EXTRACT_KINDS
        and note.status == "active"
    }
