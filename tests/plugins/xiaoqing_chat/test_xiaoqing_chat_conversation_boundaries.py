from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.xiaoqing_chat.memory.memory import (
    MemoryStore,
    StoredMessage,
    active_conversation_suffix,
    idle_gap_before_turn,
)


def _message(
    content: str,
    *,
    ts: float,
    role: str     = "user",
    local_id: str = "",
) -> StoredMessage:
    return StoredMessage(
        role     = role,
        name     = "小青" if role == "assistant" else "群友",
        content  = content,
        ts       = ts,
        local_id = local_id,
    )


def test_active_conversation_suffix_drops_topic_before_three_day_gap() -> None:
    old_ts     = 1_700_000_000.0
    current_ts = old_ts + 3 * 86400
    history    = [
        _message("[图片：一支烟] 这个烟太有格调了", ts=old_ts, local_id="old-user"),
        _message("啥烟啊，发出来看看", ts=old_ts + 12, role="assistant"),
        _message(
            "[图片：KIMI截图] 这个KIMI怎么蛤里蛤气的？",
            ts       = current_ts,
            local_id = "current-user",
        ),
    ]

    active = active_conversation_suffix(history, idle_gap_seconds=1800)

    assert [message.local_id for message in active] == ["current-user"]
    assert "烟" not in "\n".join(message.content for message in active)
    assert "KIMI" in active[0].content
    assert idle_gap_before_turn(history, current_local_id="current-user") > 2 * 86400


def test_active_conversation_suffix_keeps_continuous_turns_and_full_store() -> None:
    store = MemoryStore()
    store.append("g1", role="user", name="甲", content="第一句", ts=1000, local_id="m1")
    store.append("g1", role="assistant", name="小青", content="接住", ts=1100, local_id="m2")
    store.append("g1", role="user", name="乙", content="新一句", ts=1200, local_id="m3")

    full_history = store.get("g1")
    active = active_conversation_suffix(full_history, idle_gap_seconds=1800)

    assert [message.local_id for message in active] == ["m1", "m2", "m3"]
    assert [message.local_id for message in store.get("g1")] == ["m1", "m2", "m3"]


def test_onebot_event_timestamp_accepts_old_seconds_but_rejects_future_or_boolean(
    monkeypatch,
) -> None:
    from plugins.xiaoqing_chat.handlers import _event_message_timestamp

    monkeypatch.setattr("plugins.xiaoqing_chat.handlers.time.time", lambda: 2_000_000.0)

    assert _event_message_timestamp({"time": 1_740_800.0}) == 1_740_800.0
    assert _event_message_timestamp({"time": 2_000_301.0}) is None
    assert _event_message_timestamp({"time": True}) is None
    assert _event_message_timestamp({"time": "invalid"}) is None


@pytest.mark.asyncio
async def test_idle_boundary_clears_transient_state_once_without_clearing_memory(
    tmp_path,
) -> None:
    from plugins.xiaoqing_chat.handlers import _maybe_reset_idle_conversation

    old_ts       = 1_700_000_000.0
    current_ts   = old_ts + 3 * 86400
    memory_store = SimpleNamespace(
        get_recent_async=AsyncMock(
            return_value=[
                _message("旧话题", ts=old_ts, local_id="old"),
                _message("新话题", ts=current_ts, local_id="current"),
            ]
        ),
        clear=MagicMock(),
    )
    state = SimpleNamespace(
        memory_store=memory_store,
        goal_store=SimpleNamespace(clear=MagicMock()),
        heartflow=SimpleNamespace(clear=MagicMock()),
        action_history=SimpleNamespace(clear=MagicMock()),
        pfc_state_store=SimpleNamespace(clear=MagicMock()),
        clear_transient_chat_state=MagicMock(),
    )
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            memory=SimpleNamespace(conversation_idle_gap_seconds=1800),
            debug=SimpleNamespace(log_steps=False),
        )
    )
    context = SimpleNamespace(data_dir=tmp_path, logger=MagicMock())
    event = {"_xc_user_recorded_local_id": "current"}

    gap = await _maybe_reset_idle_conversation(
        event,
        context,
        runtime,
        state,
        chat_id="g1",
    )
    repeated_gap = await _maybe_reset_idle_conversation(
        event,
        context,
        runtime,
        state,
        chat_id="g1",
    )

    assert gap == pytest.approx(3 * 86400)
    assert repeated_gap == 0.0
    state.goal_store.clear.assert_called_once_with("g1")
    state.heartflow.clear.assert_called_once_with("g1")
    state.action_history.clear.assert_called_once_with("g1")
    state.pfc_state_store.clear.assert_called_once_with("g1")
    state.clear_transient_chat_state.assert_called_once_with("g1")
    memory_store.clear.assert_not_called()
    memory_store.get_recent_async.assert_awaited_once_with("g1", max_items=2)
