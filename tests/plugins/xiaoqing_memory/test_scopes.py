"""范围矩阵和调用方矩阵：逐格断言可见与否、能写能读与否。"""

from __future__ import annotations

import pytest

from plugins.xiaoqing_memory.model import Event, Note, Page, PageLine
from plugins.xiaoqing_memory.scopes import (
    AGENT,
    CHAT,
    LIFE,
    Caller,
    ScopeError,
    event_visible,
    may_append,
    may_change_note,
    may_write_note,
    note_scope_for,
    note_visible,
    request_scope,
    visible_page,
    web_free,
)


def _note(
    scope: str,
    *,
    kind: str       = "person.fact",
    subject: str    = "person:7",
    origin: str     = "user",
    confidence: str = "stated",
    note_id: str    = "n1",
) -> Note:
    return Note(
        id         = note_id,
        kind       = kind,
        scope      = scope,
        subject    = subject,
        text       = "t",
        origin     = origin,
        confidence = confidence,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("record_scope", "reader", "subject", "kind", "origin", "confidence", "visible"),
    [
        ("global", "g1", "self", "self.state", "self", "stated", True),
        ("global", "u7", "self", "self.story", "self", "stated", True),
        ("g1", "g1", "person:7", "person.fact", "user", "stated", True),
        ("g1", "g2", "person:7", "person.fact", "user", "stated", False),
        ("u7", "u7", "person:7", "person.fact", "user", "stated", True),
        ("u7", "g1", "person:7", "person.fact", "user", "stated", False),
        ("u7", "u8", "person:7", "person.fact", "user", "stated", False),
        # 他在群里公开说过的事，可以带进和他本人的私聊。
        ("g1", "u7", "person:7", "person.fact", "user", "stated", True),
        ("g1", "u7", "person:7", "person.pref", "user", "stated", True),
        ("g1", "u7", "person:7", "person.fact", "user", "legacy", True),
        # 别人转述的、关于别人的、群的事、话头，都不带。
        ("g1", "u7", "person:7", "person.fact", "user", "reported", False),
        ("g1", "u7", "person:8", "person.fact", "user", "stated", False),
        ("g1", "u7", "group:1", "group.fact", "user", "reported", False),
        ("g1", "u7", "person:7", "person.loop", "user", "stated", False),
        ("g1", "u7", "person:7", "person.fact", "self", "stated", False),
        ("g1", "u8", "person:7", "person.fact", "user", "stated", False),
    ],
)
def test_note_visibility_matrix(
    record_scope, reader, subject, kind, origin, confidence, visible
) -> None:
    note = _note(record_scope, kind=kind, subject=subject, origin=origin, confidence=confidence)
    assert note_visible(note, reader) is visible


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event_scope", "reader", "role", "user_id", "visible"),
    [
        ("g1", "g1", "user", 7, True),
        ("g1", "g2", "user", 7, False),
        ("g1", "u7", "user", 7, True),
        ("g1", "u7", "user", 8, False),
        ("g1", "u7", "assistant", 0, False),
        ("u7", "g1", "user", 7, False),
        ("u7", "u7", "assistant", 0, True),
    ],
)
def test_event_visibility_matrix(event_scope, reader, role, user_id, visible) -> None:
    event = Event(
        seq    = 1,
        ts     = 1.0,
        scope  = event_scope,
        kind   = "message",
        actor  = {"role": role, "user_id": user_id},
        origin = "user",
        text   = "x",
    )
    assert event_visible(event, reader) is visible


@pytest.mark.unit
def test_page_lines_filtered_per_line_for_private_reader() -> None:
    own = _note("g1", note_id="own")
    other = _note("g1", note_id="told", confidence="reported")
    page = Page(
        subject        = "person:7",
        scope          = "g1",
        brief          = "群里的简介",
        brief_note_ids = ("own",),
        sections       = {
            "stable": (
                PageLine("他自己说的", ("own",)),
                PageLine("别人说的", ("told",)),
                PageLine("混合", ("own", "told")),
            )
        },
    )
    notes = {"own": own, "told": other}
    shown = visible_page(page, notes, "u7")
    assert shown is not None
    assert [line.text for line in shown.sections["stable"]] == ["他自己说的"]
    assert shown.brief == ""
    assert visible_page(page, notes, "u8") is None
    assert visible_page(page, notes, "g1") is page


@pytest.mark.unit
@pytest.mark.parametrize(
    ("caller", "kind", "principal", "requested", "expected"),
    [
        (CHAT, "user", "g1", "", "g1"),
        (CHAT, "user", "g1", "g1", "g1"),
        (CHAT, "user", "g1", "g2", ScopeError),
        (CHAT, "user", "u7", "global", ScopeError),
        (CHAT, "system", "", "g5", "g5"),
        (CHAT, "system", "", "global", ScopeError),
        (CHAT, "system", "", "bad", ScopeError),
        (AGENT, "user", "u7", "", "u7"),
        (AGENT, "user", "u7", "u8", ScopeError),
        (LIFE, "system", "", "", "global"),
        (LIFE, "user", "g1", "global", "global"),
        (LIFE, "system", "", "g1", ScopeError),
        ("shell", "user", "u7", "u7", ScopeError),
    ],
)
def test_request_scope_matrix(caller, kind, principal, requested, expected) -> None:
    who     = Caller(caller, kind, principal)
    payload = {"scope": requested}
    if expected is ScopeError:
        with pytest.raises(ScopeError):
            request_scope(payload, who)
    else:
        assert request_scope(payload, who) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("caller", "event_kind", "allowed"),
    [
        (CHAT, "message", True),
        (CHAT, "task", False),
        (AGENT, "task", True),
        (AGENT, "message", False),
        (LIFE, "message", False),
        (LIFE, "task", False),
    ],
)
def test_event_writers(caller, event_kind, allowed) -> None:
    assert may_append(Caller(caller, "user", "u1"), event_kind) is allowed


@pytest.mark.unit
@pytest.mark.parametrize(
    ("caller", "kind", "importing", "allowed"),
    [
        (CHAT, "self.state", False, True),
        (CHAT, "self.promise", False, True),
        (CHAT, "person.loop", False, True),
        (CHAT, "person.fact", False, False),
        (CHAT, "person.fact", True, True),
        (CHAT, "legacy.summary", True, True),
        (CHAT, "episode", False, False),
        (AGENT, "person.pref", False, False),
        (AGENT, "self.state", False, False),
        (AGENT, "person.fact", True, False),
        (LIFE, "episode", False, True),
        (LIFE, "world.learning", False, True),
        (LIFE, "self.state", False, False),
        (LIFE, "person.fact", True, False),
    ],
)
def test_note_writers(caller, kind, importing, allowed) -> None:
    assert may_write_note(Caller(caller, "system", ""), kind, importing=importing) is allowed


@pytest.mark.unit
def test_only_chat_self_state_goes_global_and_life_always_global() -> None:
    chat = Caller(CHAT, "user", "u7")
    assert note_scope_for(chat, "self.state", "u7", "global") == "global"
    assert note_scope_for(chat, "self.promise", "u7", "global") is None
    assert note_scope_for(chat, "person.loop", "u7", "g1") is None
    assert note_scope_for(chat, "self.plan", "u7", None) == "u7"
    assert note_scope_for(Caller(LIFE, "system", ""), "episode", "global", "g1") == "global"


@pytest.mark.unit
def test_changes_limited_to_writable_kinds_in_scope() -> None:
    chat = Caller(CHAT, "user", "g1")
    assert may_change_note(chat, _note("g1", kind="person.loop"), "g1")
    assert not may_change_note(chat, _note("g2", kind="person.loop"), "g1")
    assert not may_change_note(chat, _note("g1", kind="person.fact"), "g1")
    assert may_change_note(chat, _note("global", kind="self.state", subject="self"), "g1")
    assert not may_change_note(Caller(AGENT, "user", "g1"), _note("g1", kind="person.loop"), "g1")


@pytest.mark.unit
def test_web_free_checks_the_whole_source_chain() -> None:
    clean   = _note("g1")
    derived = Note(
        id      = "d",
        kind    = "self.diary",
        scope   = "global",
        subject = "self",
        text    = "x",
        origin  = "derived",
        sources = ({"type": "external", "origin": "web"},),
    )
    assert web_free([clean])
    assert not web_free([clean, derived])
    assert not web_free([_note("global", origin="web")])
