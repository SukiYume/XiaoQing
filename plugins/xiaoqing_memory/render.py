"""把页面和笔记写成给模型看的几行字，按字数预算整行取舍。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .model import DAY as _DAY
from .model import SECTION_TITLES, Event, Note, Page, PageLine
from .text import similarity

# 写“最近”的小节：这里的事过了两周还没再提起，就标出是多久以前说的。
TIMELY_SECTIONS = frozenset({"recent", "doing"})
_TIMELY_DAYS    = 14


def ago(seconds: float) -> str:
    if seconds < 120:
        return "刚才"
    if seconds < 3600:
        return f"约{int(seconds // 60)}分钟前"
    if seconds < _DAY:
        return f"约{int(seconds // 3600)}小时前"
    if seconds < 60 * _DAY:
        return f"约{int(seconds // _DAY)}天前"
    return f"约{int(seconds // (30 * _DAY))}个月前"


def speaker(event: Event) -> str:
    """给模型看的“谁说的”：她自己的发言写“你”，其余用当时的名字。"""

    return "你" if event.role == "assistant" else (event.name or "有人")


def said_last(notes: Iterable[Note]) -> float:
    """这一行依据的事最近一次被说起（或印证）的时刻。被取用不算：每轮都取用的行也会变旧。"""

    return max((note.observed_at for note in notes), default=0.0)


def stale(notes: Iterable[Note], now: float, stale_days: int) -> bool:
    """这一行依据的事都很久没被说起、也没被印证过。"""

    newest = said_last(notes)
    return bool(newest) and now - newest > stale_days * _DAY


def date_passed(notes: Sequence[Note], now: float) -> bool:
    """依据的事都写着日子，而且日子都过了。"""

    ends = [note.ends_at() for note in notes]
    return bool(ends) and all(ends) and max(ends) <= now


def time_mark(section: str, notes: Sequence[Note], now: float, stale_days: int) -> str:
    if date_passed(notes, now):
        return "写着的日子已经过了"
    if stale(notes, now, stale_days):
        return "很久以前的事，现在未必还是这样"
    newest = said_last(notes)
    if section in TIMELY_SECTIONS and newest and now - newest > _TIMELY_DAYS * _DAY:
        return f"{ago(now - newest)}的事"
    return ""


def line_marks(
    section: str,
    line: PageLine,
    notes: Mapping[str, Note],
    now: float,
    stale_days: int,
    where: str = "",
) -> str:
    cited = [notes[note_id] for note_id in line.note_ids if note_id in notes]
    marks = [where] if where else []
    if section == "avoid" or any(note.usage == "avoid" for note in cited):
        marks.append("对方没提就别主动说")
    elif cited and all(note.usage == "tone" for note in cited):
        marks.append("影响你说话的方式和分寸，不必说出来")
    if cited and all(note.confidence in ("hypothesis", "reported") for note in cited):
        marks.append("拿不准")
    when = time_mark(section, cited, now, stale_days)
    if when:
        marks.append(when)
    return f"（{'；'.join(marks)}）" if marks else ""


def brief_text(page: Page, notes: Mapping[str, Note], now: float, stale_days: int) -> str:
    """页面的一句话简介，依据的事过了日子或很久没被说起时同样标出来。"""

    cited = [notes[note_id] for note_id in page.brief_note_ids if note_id in notes]
    when  = time_mark("", cited, now, stale_days)
    return f"{page.brief}（{when}）" if when else page.brief


def note_line(note: Note, now: float, stale_days: int, where: str = "") -> str:
    marks = [where] if where else []
    if note.usage == "avoid":
        marks.append("对方没提就别主动说")
    elif note.usage == "tone":
        marks.append("影响你说话的方式和分寸，不必说出来")
    if note.confidence in ("hypothesis", "reported", "legacy"):
        marks.append("拿不准" if note.confidence != "legacy" else "旧记录，拿不准")
    when = time_mark("", [note], now, stale_days)
    if when:
        marks.append(when)
    suffix = f"（{'；'.join(marks)}）" if marks else ""
    return f"{note.text}{suffix}"


def fit(lines: Sequence[str], budget: int) -> list[str]:
    """按顺序整行放进预算；放不下的行跳过，后面更短的行还能放。"""

    kept: list[str] = []
    total           = 0
    for line in lines:
        cost = len(line) + 1
        if total + cost > budget:
            continue
        kept.append(line)
        total += cost
    return kept


def echoed(text: str, bot_recent: Sequence[str]) -> bool:
    """她最近几条回复里已经说过的内容。"""

    return any(similarity(text, said) >= 0.6 for said in bot_recent if said)


def page_lines(
    page: Page,
    notes: Mapping[str, Note],
    *,
    now: float,
    stale_days: int,
    order: Sequence[str],
    skip: Iterable[str] = (),
    where: str          = "",
) -> list[tuple[str, str, PageLine]]:
    """页面的行，按小节顺序；返回（小节，渲染后的文字，行）。``where`` 是这页记下的地方和读的地方
    不一样时给每行加的说明（比如私聊里读到的群里的事）。"""

    skipped = set(skip)
    result  = []
    for section in order:
        for line in page.sections.get(section, ()):
            if section in skipped:
                continue
            title = SECTION_TITLES.get(section, section)
            marks = line_marks(section, line, notes, now, stale_days, where)
            text  = f"- [{title}] {line.text}{marks}"
            result.append((section, text, line))
    return result
