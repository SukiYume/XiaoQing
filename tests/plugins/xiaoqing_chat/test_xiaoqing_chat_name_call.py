"""只喊一声名字：立刻回一句预设短应答，发出后记进聊天记录，接下来那句算在跟她说。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.delivery import DeliverySegments
from core.interfaces import PluginCapabilities
from plugins.xiaoqing_chat import follow_ups
from plugins.xiaoqing_chat.constants import DEFAULT_BOT_NAME_ONLY_REPLIES
from plugins.xiaoqing_chat.main import call_bot_name_only
from plugins.xiaoqing_chat.message_parts import render_stored_message
from plugins.xiaoqing_chat.runtime_state import get_state, reset_global_state

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "xiaoqing_chat"
CALLER     = {"message_type": "group", "group_id": 300, "user_id": 7}


@pytest.fixture
def name_call_context(mock_context_factory, tmp_path):
    reset_global_state()
    follow_ups._carried.clear()
    context = mock_context_factory(
        "xiaoqing_chat", PLUGIN_DIR, tmp_path, user_id=7, group_id=300, request_id="r1"
    )
    yield context
    follow_ups._carried.clear()
    reset_global_state()


@pytest.mark.asyncio
async def test_bare_name_gets_a_preset_reply_that_is_recorded_once_sent(name_call_context):
    result = await call_bot_name_only(name_call_context)

    assert isinstance(result, DeliverySegments)
    text = result[0]["data"]["text"]
    assert text in DEFAULT_BOT_NAME_ONLY_REPLIES
    receipt = result.delivery_receipt
    assert receipt is not None
    receipt.add_message_id(991)
    await receipt.record(True)
    last = get_state().memory_store.get("g300")[-1]
    assert last.role == "assistant" and render_stored_message(last) == text
    assert last.message_id == 991
    # 他接着说的下一句算在跟她说（比如“帮我记一笔”照样交给 agent）。
    assert follow_ups.take_carried_attention(CALLER) is True


@pytest.mark.asyncio
async def test_back_to_back_calls_do_not_repeat_the_same_reply(name_call_context):
    first  = (await call_bot_name_only(name_call_context))[0]["data"]["text"]
    second = (await call_bot_name_only(name_call_context))[0]["data"]["text"]
    assert first != second


@pytest.mark.asyncio
async def test_no_reply_when_the_sender_already_sent_the_next_message(name_call_context):
    waiting = ({"message_id": 5, "message": [{"type": "text", "data": {"text": "帮我记一笔"}}]},)
    name_call_context.capabilities = PluginCapabilities(
        inbound_backlog=SimpleNamespace(waiting=lambda: waiting)
    )
    assert await call_bot_name_only(name_call_context) == []
    assert follow_ups.take_carried_attention(CALLER) is True
