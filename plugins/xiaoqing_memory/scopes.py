"""范围与调用方规则：哪条记录在哪里可见，哪个插件能写什么、读什么。全插件只在这里判断。

范围是 ``global``、``g<群号>``、``u<QQ号>``。可见性只有四条：

1. ``global`` 的记录都可见；
2. 记录的范围等于读取方所在范围时可见；
3. 读取方在 ``u<X>``，记录在某个群、是关于 X 本人、出自 X 自己的原话：可见。他在群里公开说过的事，
   可以带进和他本人的私聊；
4. 其余不可见。私聊里的事不进群，一个群的事不进另一个群。

调用方名字和用户主体的范围由 Core 写进载荷（``caller``、``principal_kind``、``principal_scope``），
调用方自己填的同名字段在 Core 那里就被覆盖了。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .model import GLOBAL, SELF_KINDS, Event, Note, Page, PageLine

CHAT  = "xiaoqing_chat"
AGENT = "xiaoqing_agent"
LIFE  = "xiaoqing_life"

_SCOPE_RE = re.compile(r"^(?:global|[gu][1-9][0-9]{0,19})$")

# 各调用方能追加的事件类型、能新增或修改的笔记类型。
EVENT_WRITERS: dict[str, frozenset[str]] = {
    CHAT : frozenset({"message"}),
    AGENT: frozenset({"task"}),
}
NOTE_WRITERS: dict[str, frozenset[str]] = {
    CHAT: frozenset({*SELF_KINDS, "person.loop"}),
    LIFE: frozenset({"episode", "self.diary", "world.learning"}),
}
# 迁移旧数据时 chat 一次性导入的类型。
IMPORT_KINDS = frozenset({"person.fact", "legacy.summary", *SELF_KINDS, "person.loop"})
# 这几类关于她自己的笔记可以记在 ``global``，各处都记得。
SHAREABLE_SELF = frozenset({"self.state", "self.plan", "self.story"})
# 在群里说、可以带进和本人私聊的笔记：本人原话产生的人物笔记。
_PUBLIC_TO_PRIVATE_KINDS = frozenset({"person.fact", "person.pref"})
_OWN_WORDS               = frozenset({"stated", "legacy", "verified"})


def valid_scope(scope: Any) -> bool:
    return isinstance(scope, str) and _SCOPE_RE.fullmatch(scope) is not None


def scope_user(scope: str) -> int:
    """私聊范围 ``u<QQ号>`` 的 QQ 号；其他范围为 0。"""

    return int(scope[1:]) if scope.startswith("u") and scope[1:].isdigit() else 0


def person_subject(user_id: int) -> str:
    return f"person:{int(user_id)}"


def group_subject(scope: str) -> str:
    return f"group:{scope[1:]}"


SELF_SUBJECT = "self"


# ───────────────────────────── 可见性 ─────────────────────────────


def note_visible(note: Note, reader: str) -> bool:
    if note.scope in (GLOBAL, reader):
        return True
    user = scope_user(reader)
    return bool(
        user
        and note.scope.startswith("g")
        and note.kind in _PUBLIC_TO_PRIVATE_KINDS
        and note.subject == person_subject(user)
        and note.origin == "user"
        and note.confidence in _OWN_WORDS
    )


def event_visible(event: Event, reader: str) -> bool:
    if event.scope == reader:
        return True
    user = scope_user(reader)
    return bool(
        user and event.scope.startswith("g") and event.role == "user" and event.user_id == user
    )


def line_visible(line: PageLine, notes: Mapping[str, Note], reader: str) -> bool:
    """页面的一行只在它引用的笔记全都可见时可见。"""

    cited = [notes.get(note_id) for note_id in line.note_ids]
    return bool(cited) and all(note is not None and note_visible(note, reader) for note in cited)


def visible_page(page: Page, notes: Mapping[str, Note], reader: str) -> Page | None:
    """按读取方逐行过滤页面；一行都不剩时返回 ``None``。简介只在整页可见时保留。"""

    if page.scope in (GLOBAL, reader):
        return page
    sections = {
        name: tuple(line for line in items if line_visible(line, notes, reader))
        for name, items in page.sections.items()
    }
    if not any(sections.values()):
        return None
    return replace(page, brief="", brief_note_ids=(), sections=sections)


def web_free(notes: Iterable[Note]) -> bool:
    """来源链里没有网页文字。交给有命令执行能力的 agent 的内容必须满足。"""

    return all("web" not in note.source_origins() for note in notes)


# ───────────────────────────── 调用方 ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Caller:
    name : str
    kind : str
    scope: str

    @property
    def is_user(self) -> bool:
        return self.kind == "user"


class ScopeError(ValueError):
    """请求的范围不合法，或者不是这个调用方能用的范围。"""


def caller_of(payload: Mapping[str, Any]) -> Caller:
    return Caller(
        name  = str(payload.get("caller") or ""),
        kind  = "user" if payload.get("principal_kind") == "user" else "system",
        scope = str(payload.get("principal_scope") or ""),
    )


def request_scope(payload: Mapping[str, Any], caller: Caller) -> str:
    """这次请求作用的范围。

    用户主体发起的调用，范围必须等于 Core 写入的主体范围（不填就用它）；系统主体（定时任务）按
    调用方声明的范围执行。life 只关于她自己，只能用 ``global``。
    """

    requested = str(payload.get("scope") or "")
    if caller.name == LIFE:
        if requested not in ("", GLOBAL):
            raise ScopeError("life_global_only")
        return GLOBAL
    if caller.name not in (CHAT, AGENT):
        raise ScopeError("unknown_caller")
    if caller.is_user:
        if not valid_scope(caller.scope) or caller.scope == GLOBAL:
            raise ScopeError("bad_principal_scope")
        if requested and requested != caller.scope:
            raise ScopeError("scope_mismatch")
        return caller.scope
    if not valid_scope(requested) or requested == GLOBAL:
        raise ScopeError("bad_scope")
    return requested


def may_append(caller: Caller, kind: str) -> bool:
    return kind in EVENT_WRITERS.get(caller.name, frozenset())


def may_write_note(caller: Caller, kind: str, *, importing: bool = False) -> bool:
    if importing:
        return caller.name == CHAT and kind in IMPORT_KINDS
    return kind in NOTE_WRITERS.get(caller.name, frozenset())


def note_scope_for(caller: Caller, kind: str, request: str, wanted: Any) -> str | None:
    """一条新笔记落在哪个范围；不允许时返回 ``None``。

    chat 可以把她自己的近况、打算、往事记在 ``global``（各处都记得），其余都留在本范围；life 的
    一律在 ``global``。
    """

    target = str(wanted or request)
    if caller.name == LIFE:
        return GLOBAL
    if target == request:
        return request
    if target == GLOBAL and caller.name == CHAT and kind in SHAREABLE_SELF:
        return GLOBAL
    return None


def may_change_note(caller: Caller, note: Note, request: str) -> bool:
    """改动或结束一条已有笔记：类型是调用方能写的，且笔记在这次请求的范围里可见。"""

    if note.kind not in NOTE_WRITERS.get(caller.name, frozenset()):
        return False
    return note.scope == request or (note.scope == GLOBAL and caller.name in (CHAT, LIFE))
