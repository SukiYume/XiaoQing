"""``/pendo event add --force``：已经知道时间冲突时直接创建，不再进入确认会话。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugins.pendo.handlers.event import EventHandler, _split_force_flag


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("明天九点组会", ("明天九点组会", False)),
        ("明天九点组会 --force", ("明天九点组会", True)),
        ("--force 明天九点组会", ("明天九点组会", True)),
        ("明天 --force 九点组会", ("明天 九点组会", True)),
        ("讨论 --forcex 参数", ("讨论 --forcex 参数", False)),
        ("--force", ("", True)),
    ],
)
def test_split_force_flag(raw: str, expected: tuple[str, bool]) -> None:
    assert _split_force_flag(raw) == expected


def _handler(results: list[dict]) -> tuple[SimpleNamespace, list]:
    calls: list = []

    async def parse_event_with_ai(text, user_id):
        calls.append(("parse", text))
        return {"title": text}

    async def create_event(user_id, parsed, context, allow_conflict=False):
        calls.append(("create", parsed["title"], allow_conflict))
        return results.pop(0)

    handler = SimpleNamespace(
        ai_parser=SimpleNamespace(parse_event_with_ai=parse_event_with_ai),
        create_event=create_event,
    )
    return handler, calls


def test_force_flag_creates_despite_conflict_without_a_session() -> None:
    sessions: list = []

    class Context:
        async def create_session(self, initial_data=None, timeout=300.0):
            sessions.append(initial_data)

    handler, calls = _handler([{"status": "success", "message": "✅ 已创建日程"}])
    result = asyncio.run(EventHandler.add_event(handler, "u1", "明天九点组会 --force", Context()))

    assert result["status"] == "success"
    assert calls == [("parse", "明天九点组会"), ("create", "明天九点组会", True)]
    assert sessions == []


def test_conflict_without_force_still_asks_for_confirmation() -> None:
    sessions: list = []

    class Context:
        async def create_session(self, initial_data=None, timeout=300.0):
            sessions.append(initial_data)

    handler, calls = _handler(
        [{"status": "need_confirm", "message": "冲突", "data": {"title": "组会"}}]
    )
    result = asyncio.run(EventHandler.add_event(handler, "u1", "明天九点组会", Context()))

    assert result["status"] == "need_confirm"
    assert calls[-1] == ("create", "明天九点组会", False)
    assert sessions and sessions[0]["data"] == {"title": "组会"}


def test_diary_entry_time_is_shown_in_the_user_timezone() -> None:
    from zoneinfo import ZoneInfo

    from plugins.pendo.handlers.diary import DiaryHandler

    shanghai = ZoneInfo("Asia/Shanghai")
    stored = SimpleNamespace(entry_time="2026-10-21T13:32:59+00:00", created_at="")
    legacy = SimpleNamespace(entry_time="2026-10-21T21:32:00", created_at="")
    assert DiaryHandler._format_entry_time(stored, shanghai) == "21:32"
    assert DiaryHandler._format_entry_time(legacy, shanghai) == "21:32"
    assert DiaryHandler._format_entry_time(SimpleNamespace(entry_time="", created_at="x")) == ""
