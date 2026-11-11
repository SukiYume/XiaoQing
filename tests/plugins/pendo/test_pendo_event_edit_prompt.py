"""编辑日程时交给解析器的提示：原时间按日程时区换算，相对说法才有正确的基准。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.pendo.handlers.event_editing import EventEditingMixin
from plugins.pendo.models.item import EventItem
from plugins.pendo.services.ai_parser import AIParser


async def _prompt_for(event: EventItem) -> str:
    seen: list[str] = []

    async def parse_event_with_ai(text, user_id, *, partial=False, fallback_text=None):
        seen.append(text)
        return {}

    editor = SimpleNamespace(ai_parser=SimpleNamespace(parse_event_with_ai=parse_event_with_ai))
    await EventEditingMixin._parse_ai_edit_updates(editor, "改到两周后的同一时间", event)
    return seen[0]


@pytest.mark.asyncio
async def test_utc_storage_is_shown_to_the_parser_as_local_wall_time_with_weekday():
    event = EventItem(
        owner_id   = "u1",
        title      = "近邻星系研讨会",
        start_time = "2026-11-08T10:00:00+00:00",
        end_time   = "2026-11-08T12:00:00+00:00",
        timezone   = "Asia/Shanghai",
    )
    prompt = await _prompt_for(event)
    assert "原时间：2026年11月08日 周日 18:00" in prompt
    assert "原结束时间：2026年11月08日 周日 20:00" in prompt
    assert "10:00" not in prompt
    assert "以原时间为基准换算" in prompt


@pytest.mark.asyncio
async def test_event_without_end_time_omits_the_end_clause():
    event = EventItem(
        owner_id   = "u1",
        title      = "会",
        start_time = "2026-11-08T10:00:00+00:00",
        timezone   = "America/Los_Angeles",
    )
    prompt = await _prompt_for(event)
    assert "原时间：2026年11月08日 周日 02:00" in prompt
    assert "原结束时间" not in prompt


def test_parse_prompt_lets_an_explicit_multi_node_request_override_the_meeting_rule():
    rules = AIParser.PARSE_PROMPT_TEMPLATE
    assert "用户明确要求多节点" in rules and "优先于上一条的普通会议写法" in rules
    assert rules.index("普通会议的开始与结束") < rules.index("用户明确要求多节点")


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", ["改成会议结束", "改为会议结束", "改成“会议结束”"])
async def test_natural_rename_is_applied_to_the_existing_event(changes):
    event = EventItem(
        owner_id   = "u1",
        title      = "旧节点",
        start_time = "2030-11-08T10:00:00+00:00",
        end_time   = "2030-11-08T12:00:00+00:00",
        timezone   = "Asia/Shanghai",
    )

    async def parse_event_with_ai(text, user_id, **kwargs):
        assert "仅改名时保留原有起止时间" in text
        return {"title": "会议结束"}

    editor = EventEditingMixin()
    editor.ai_parser = SimpleNamespace(parse_event_with_ai=parse_event_with_ai)
    assert await editor._parse_updates(changes, event) == {"title": "会议结束"}
