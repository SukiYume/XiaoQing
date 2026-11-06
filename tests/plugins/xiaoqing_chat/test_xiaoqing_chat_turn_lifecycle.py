"""单轮生成、提交和并发生命周期。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.planning.pfc_engine import PFCRunResult
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState
from tests.helpers.xiaoqing_chat_test_support import (
    _ack_reply_delivery,
    _make_hctx,
    _reply_draft,
    patch_turn,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_smalltalk_rejection_persists_updated_pfc_state_and_cancels_speculative_memory(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk
    from plugins.xiaoqing_chat.llm.check_result import ReplyRejected

    lock                              = asyncio.Lock()
    cancel_seen                       = asyncio.Event()
    blocker                           = asyncio.Event()
    state                             = MagicMock()
    state.get_lock.return_value       = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=PFCConversationState(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.pfc_state_store.set_state       = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=6,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    async def fake_build_memory_block(**_kwargs):
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            raise
        return ""

    async def fake_run_pfc_once(**kwargs):
        snapshot                    = kwargs["state_override"]
        snapshot.goal_list          = [{"goal": "重规划后的目标"}]
        snapshot.planner_skip_until = 30.0
        await asyncio.sleep(0)
        raise ReplyRejected("需要重新规划", True)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers.chat_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.decide_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.retrieve_memory_context",
            new=AsyncMock(side_effect=fake_build_memory_block),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_generation.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch("plugins.xiaoqing_chat.handlers.schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers.schedule_action_history_flush"),
        patch_turn("schedule_pfc_state_flush") as mock_flush,
        patch("plugins.xiaoqing_chat.handlers.log_step"),
    ):
        with pytest.raises(ReplyRejected, match="需要重新规划"):
            await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    await asyncio.wait_for(cancel_seen.wait(), timeout=1.0)
    state.pfc_state_store.set_state.assert_called_once()
    persisted = state.pfc_state_store.set_state.call_args.args[1]
    assert persisted.goal_list == [{"goal": "重规划后的目标"}]
    assert persisted.planner_skip_until == 30.0
    mock_flush.assert_called_once()


@pytest.mark.asyncio
async def test_smalltalk_forced_reply_generation_runs_outside_chat_lock_but_commits_inside(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                                = asyncio.Lock()
    state                               = MagicMock()
    state.get_lock.return_value         = lock
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.pfc_state_store.get_async       = AsyncMock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event.pop("group_id", None)
    event["_xc_command_forced"] = True

    async def fake_generate_reply_draft(**kwargs):
        assert not lock.locked()
        return _reply_draft("forced-ok")

    async def fake_record_bot_reply(*args, **kwargs):
        assert lock.locked()
        return []

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers.chat_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.generate_reply_draft",
            new=AsyncMock(side_effect=fake_generate_reply_draft),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_delivery._record_bot_reply",
            new=AsyncMock(side_effect=fake_record_bot_reply),
        ) as record_reply,
        patch(
            "plugins.xiaoqing_chat.turn_delivery.split_history_at",
            return_value=([], []),
        ),
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers.schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers.schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch("plugins.xiaoqing_chat.handlers.log_step"),
        patch(
            "plugins.xiaoqing_chat.turn_delivery.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)
        await _ack_reply_delivery(result)
        record_reply.assert_awaited_once()

    assert result == [{"type": "text", "data": {"text": "forced-ok"}}]


@pytest.mark.asyncio
async def test_smalltalk_stale_generation_is_dropped_before_commit(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                                = asyncio.Lock()
    state                               = MagicMock()
    state.get_lock.return_value         = lock
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=PFCConversationState(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.pfc_state_store.set_state       = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=10,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch_turn("chat_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.decide_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.retrieve_memory_context",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_generation.run_pfc_once",
            new=AsyncMock(
                return_value=PFCRunResult(
                    reply="planner-ok", action="reply", reason="ok", ended=False
                )
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_delivery.split_history_at",
            return_value=None,
        ),
        patch(
            "plugins.xiaoqing_chat.turn_delivery._record_bot_reply", new=AsyncMock()
        ) as mock_record_bot_reply,
        patch_turn(
            "schedule_pfc_state_flush",
            create=True,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch_turn("schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.turn_delivery.schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch_turn("log_step") as mock_log_step,
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == []
    mock_record_bot_reply.assert_not_awaited()
    state.heartflow.on_no_reply_async.assert_not_awaited()
    state.action_history.append.assert_not_called()
    state.pfc_state_store.set_state.assert_not_called()
    mock_schedule_pfc_state_flush.assert_not_called()
    assert any(
        call.kwargs.get("step") == "smalltalk.stale.drop" for call in mock_log_step.call_args_list
    )


@pytest.mark.asyncio
async def test_smalltalk_no_reply_current_turn_schedules_pfc_state_flush(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                                = asyncio.Lock()
    state                               = MagicMock()
    state.get_lock.return_value         = lock
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=PFCConversationState(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=10,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    def fake_set_state(chat_id, snapshot):
        assert lock.locked()

    def fake_schedule_pfc_state_flush(context, runtime, *, chat_id):
        assert not lock.locked()

    state.pfc_state_store.set_state = Mock(side_effect=fake_set_state)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch_turn("chat_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.decide_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.retrieve_memory_context",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_generation.run_pfc_once",
            new=AsyncMock(
                return_value=PFCRunResult(reply="", action="wait", reason="later", ended=False)
            ),
        ),
        patch_turn(
            "schedule_pfc_state_flush",
            side_effect = fake_schedule_pfc_state_flush,
            create      = True,
        ) as mock_schedule_pfc_state_flush,
        patch(
            "plugins.xiaoqing_chat.turn_delivery.split_history_at",
            return_value=([], []),
        ),
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch_turn("schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.turn_delivery.schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch_turn("log_step") as mock_log_step,
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == []
    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()
    state.heartflow.on_no_reply_async.assert_awaited_once()
    no_reply_calls = [
        call
        for call in mock_log_step.call_args_list
        if call.kwargs.get("step") == "smalltalk.no_reply"
    ]
    assert no_reply_calls
    assert no_reply_calls[-1].kwargs["fields"]["reason"] == "pfc_no_reply"


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_reply_record_fails(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                                = asyncio.Lock()
    state                               = MagicMock()
    state.get_lock.return_value         = lock
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=PFCConversationState(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=10,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    def fake_set_state(chat_id, snapshot):
        assert lock.locked()

    def fake_schedule_pfc_state_flush(context, runtime, *, chat_id):
        assert not lock.locked()

    state.pfc_state_store.set_state = Mock(side_effect=fake_set_state)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch_turn("chat_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.decide_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.retrieve_memory_context",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_generation.run_pfc_once",
            new=AsyncMock(
                return_value=PFCRunResult(
                    reply="planner-ok", action="reply", reason="ok", ended=False
                )
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_delivery._record_bot_reply",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch_turn(
            "schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
        ) as mock_schedule_pfc_state_flush,
        patch(
            "plugins.xiaoqing_chat.turn_delivery.split_history_at",
            return_value=([], []),
        ),
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch_turn("schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.turn_delivery.schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch_turn("log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

        receipt = await _ack_reply_delivery(result)

        assert isinstance(receipt.callback_error, RuntimeError)
        assert str(receipt.callback_error) == "boom"
        state.pfc_state_store.set_state.assert_called_once()
        mock_schedule_pfc_state_flush.assert_called_once()


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_no_reply_hook_fails(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                                = asyncio.Lock()
    state                               = MagicMock()
    state.get_lock.return_value         = lock
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=PFCConversationState(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock(side_effect=RuntimeError("no-reply boom"))
    state.inc_stats             = Mock()
    state.action_history.append = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=10,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    def fake_set_state(chat_id, snapshot):
        assert lock.locked()

    def fake_schedule_pfc_state_flush(context, runtime, *, chat_id):
        assert not lock.locked()

    state.pfc_state_store.set_state = Mock(side_effect=fake_set_state)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch_turn("chat_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.decide_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.retrieve_memory_context",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_generation.run_pfc_once",
            new=AsyncMock(
                return_value=PFCRunResult(reply="", action="wait", reason="later", ended=False)
            ),
        ),
        patch_turn(
            "schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
        ) as mock_schedule_pfc_state_flush,
        patch(
            "plugins.xiaoqing_chat.turn_delivery.split_history_at",
            return_value=([], []),
        ),
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch_turn("schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.turn_delivery.schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch_turn("log_step"),
    ):
        with pytest.raises(RuntimeError, match="no-reply boom"):
            await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()
    state.action_history.append.assert_not_called()


@pytest.mark.asyncio
async def test_mode_indicator_only_applies_in_brain_chat(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(
                enable_private_brain_chat = False,
                show_mode_indicator       = True,
                brain_mode_indicator      = "[brain]",
            ),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event.pop("group_id", None)
    event["_xc_command_forced"] = True

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers.chat_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.generate_reply_draft",
            new=AsyncMock(return_value=_reply_draft("ok")),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_delivery.split_history_at",
            return_value=([], []),
        ),
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers.schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers.schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch("plugins.xiaoqing_chat.handlers.log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "ok"}}]
    mock_context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_indicator_is_emitted_when_brain_chat_is_active(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(
                enable_private_brain_chat = True,
                show_mode_indicator       = True,
                brain_mode_indicator      = "[brain]",
            ),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event.pop("group_id", None)
    event["_xc_command_forced"] = True

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers.chat_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers.is_ignored_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.turn_generation.ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=True),
        patch(
            "plugins.xiaoqing_chat.turn_generation.generate_reply_draft",
            new=AsyncMock(return_value=_reply_draft("ok")),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_delivery.split_history_at",
            return_value=([], []),
        ),
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers.schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers.schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch("plugins.xiaoqing_chat.handlers.log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "[brain]\nok"}}]
    mock_context.send_action.assert_not_awaited()


# ---------------------------------------------------------------- 晚到的回复：近的直接接，远的引用，太远或换话题才丢


def _record_messages(chat_id: str, *entries: tuple[str, str] | tuple[str, str, str]) -> None:
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts
    from plugins.xiaoqing_chat.runtime_state import get_state

    for local_id, role, *text in entries:
        get_state().memory_store.append(
            chat_id,
            role     = role,
            name     = "群友" if role == "user" else "小青",
            user_id  = 1 if role == "user" else None,
            local_id = local_id,
            parts    = build_text_message_parts(text[0] if text else f"{local_id} 的内容"),
        )


def _later(count: int) -> list[tuple[str, str]]:
    return [(f"m{index + 2}", "user") for index in range(count)]


def _fresh_finalization(tmp_path, *, chat_id: str = "g1", event: dict | None = None):
    from plugins.xiaoqing_chat.generation_limiter import GenerationLimiter

    return SimpleNamespace(
        checked_users = None,
        generated     = SimpleNamespace(
            local_id                  = "m1",
            session_generation        = None,
            cancel_speculative_memory = Mock(),
            reply                     = "对，九十二次改成 901 了",
            reply_parts               = (),
            reply_payload             = None,
            timings                   = {},
        ),
        hctx=SimpleNamespace(
            chat_id  = chat_id,
            bot_name = "小青",
            secrets  = {},
            runtime=SimpleNamespace(cfg=SimpleNamespace(llm_config=lambda _route: Mock())),
            state=SimpleNamespace(generation_limiter=GenerationLimiter()),
        ),
        context=SimpleNamespace(data_dir=tmp_path),
        event=event if event is not None else {"group_id": 1, "message_id": 555},
    )


def test_split_history_counts_every_visible_message_after_the_target():
    from plugins.xiaoqing_chat.memory.local_ids import (
        MAX_REPLY_RECENT_MESSAGES,
        reply_is_stale,
        split_history_at,
    )

    _record_messages("g1", ("m0", "user"), ("m1", "user"), ("m2", "user"), ("m3", "assistant"))
    before, after = split_history_at("g1", "m1")
    assert [message.local_id for message in before] == ["m0", "m1"]
    assert [message.local_id for message in after] == ["m2", "m3"]
    assert split_history_at("g1", "m9") is None
    assert not reply_is_stale("g1", "m1")
    assert reply_is_stale("g1", "m9")

    _record_messages(
        "g1", *[(f"x{index}", "user") for index in range(MAX_REPLY_RECENT_MESSAGES - 2)]
    )
    assert reply_is_stale("g1", "m1")


@pytest.mark.parametrize(
    ("later", "dropped"),
    [
        (1, False),  # 生产日志：别的机器人 4 秒后插一句，审查通过的回复就被整条丢掉。
        (4, False),
        (14, False),  # 要接的那句还在最近 15 条里
        (15, True),
    ],
)
def test_reply_is_kept_until_the_target_leaves_the_last_15_messages(tmp_path, later, dropped):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), *_later(later))
    finalization = _fresh_finalization(tmp_path)
    with patch.object(turn_delivery, "log_step") as log:
        assert (
            turn_delivery._drop_stale_generated_turn(finalization, phase="before_commit") is dropped
        )
    assert log.called is dropped
    if dropped:
        assert log.call_args.kwargs["fields"]["newer_messages"] == later


@pytest.mark.parametrize(
    ("later", "reason"),
    [
        ([("m2", "user", "不用回复了")], ""),
        ([("m2", "user", "哈哈"), ("m3", "user", "@的不是你")], ""),
        ([("m2", "assistant", "不用回复了")], ""),  # 小青自己的话不算叫停
        ([("m2", "user", "不用回复了吗？")], ""),  # 问句不是叫停
        ([("m2", "user", "阿梨，别回复我了")], ""),
        ([("m2", "user", "@小明 别回复我了")], ""),
        ([("m2", "user", "小明，不是叫你")], ""),
        ([("m2", "user", "小青，别回复了")], ""),
        ([("m2", "user", "嘶")], ""),
    ],
)
def test_stale_reason_leaves_message_meaning_to_context_review(later, reason):
    from plugins.xiaoqing_chat.memory.local_ids import split_history_at, stale_reason

    _record_messages("g1", ("m1", "user"), *later)
    split = split_history_at("g1", "m1")
    assert split is not None
    assert stale_reason(split[1]) == reason


@pytest.mark.asyncio
async def test_reply_is_dropped_after_context_review_of_stop_request(tmp_path):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), ("m2", "user", "不用回复了"))
    finalization = _fresh_finalization(tmp_path)
    with (
        patch.object(
            turn_delivery, "reply_should_be_withheld", AsyncMock(return_value=True)
        ) as check,
        patch.object(turn_delivery, "log_step"),
    ):
        assert await turn_delivery._drop_if_context_changed(finalization)
    check.assert_awaited_once()
    finalization.generated.cancel_speculative_memory.assert_called_once()


@pytest.mark.asyncio
async def test_silence_request_reaches_context_review(tmp_path):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), *_later(5), ("m9", "user", "别回了"))
    finalization = _fresh_finalization(tmp_path)
    with (
        patch.object(
            turn_delivery, "reply_should_be_withheld", AsyncMock(return_value=True)
        ) as check,
        patch.object(turn_delivery, "log_step"),
    ):
        assert await turn_delivery._drop_if_context_changed(finalization)
    check.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,stopped",
    [("不用回复了", True), ("阿梨，别回复我了", False), ("@小明 别回复我了", False)],
)
async def test_silence_request_between_bubbles_respects_addressee(tmp_path, text, stopped):
    """气泡之间针对小青的叫停会结束投递，发给其他群友的要求保留当前回复。"""

    from plugins.xiaoqing_chat import turn_delivery
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts

    _record_messages("g1", ("m1", "user"))
    sent: list = []

    async def send_action(action):
        sent.append(action)
        _record_messages("g1", (f"s{len(sent)}", "user", text))
        return True

    from plugins.xiaoqing_chat.chat_runtime import ChatRuntime
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig

    finalization = _fresh_finalization(tmp_path)
    finalization.context = SimpleNamespace(data_dir=tmp_path, send_action=send_action)
    finalization.hctx.runtime            = ChatRuntime(XiaoQingChatConfig(), [])
    finalization.generated.reply_payload = turn_delivery.build_reply_payload_from_parts(
        build_text_message_parts("一\n\n二\n\n三")
    )
    batches = [[{"type": "text", "data": {"text": text}}] for text in ("一", "二", "三")]
    bubbles = turn_delivery._ReplyBubbles(finalization, (), batches)
    with (
        patch.object(turn_delivery, "chat_lock", side_effect=lambda _chat_id: asyncio.Lock()),
        patch.object(turn_delivery.asyncio, "sleep", AsyncMock()),
        patch.object(turn_delivery, "log_step"),
        patch.object(turn_delivery, "reply_should_be_withheld", AsyncMock(return_value=stopped)),
        patch.object(turn_delivery, "_commit_smalltalk_delivery", AsyncMock()) as commit,
    ):
        continued = await turn_delivery._send_smalltalk_intermediate_batches(
            finalization, batches, bubbles, apply_humanize=False
        )
        assert continued is not stopped
        assert turn_delivery._drop_stale_generated_turn(finalization, phase="before_final") is False
    assert len(sent) == (1 if stopped else 2)
    if stopped:
        commit.assert_awaited_once_with(finalization, build_text_message_parts("一"), ())
    else:
        commit.assert_not_awaited()


@pytest.mark.parametrize(
    "text,stopped",
    [
        ("不用回复了", True),
        ("小青，别回复了", True),
        ("阿梨，别回复我了", False),
        ("@小明 别回复我了", False),
    ],
)
def test_reply_generator_defers_stop_wording_to_delivery_review(text, stopped):
    from plugins.xiaoqing_chat.reply_generator import _is_turn_stale

    _record_messages("g1", ("m1", "user"))
    event = {"_xc_user_recorded_local_id": "m1"}
    assert not _is_turn_stale("g1", event)
    _record_messages("g1", ("m2", "user", text))
    assert not _is_turn_stale("g1", event)


@pytest.mark.asyncio
async def test_third_party_silence_still_allows_topic_check(tmp_path):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), *_later(5), ("m9", "user", "阿梨，别回复了"))
    finalization = _fresh_finalization(tmp_path)
    with patch.object(
        turn_delivery, "reply_should_be_withheld", AsyncMock(return_value=False)
    ) as check:
        assert not await turn_delivery._drop_if_context_changed(finalization)
    check.assert_awaited_once()


# ---------------------------------------------------------------- 迟到的回执不能覆盖更新的规划状态


def _pfc_state(goal: str, *, ended: bool):
    return PFCConversationState(
        chat_id                      = "g1",
        ignore_until_ts              = 0.0,
        ended                        = ended,
        last_successful_reply_action = "",
        goal_list                    = [{"goal": goal}],
        knowledge_list               = [],
        planner_fail_ts              = [],
        planner_skip_until           = 0.0,
        updated_at                   = 0.0,
    )


def test_planner_turns_commit_in_order():
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    store = PFCStateStore()
    first, second = store.begin_turn("g1"), store.begin_turn("g1")
    assert (first, second) == (1, 2)
    other = store.begin_turn("g2")
    assert other > second
    assert store.claim_commit("g1", first)  # 新轮次尚未提交时，较早的有效快照仍可写回。
    assert store.claim_commit("g1", second)
    assert not store.claim_commit("g1", first)
    assert store.claim_commit("g1", second)  # 同一轮重复提交（重规划）仍然允许
    assert store.claim_commit("g2", other)


@pytest.mark.asyncio
async def test_late_receipt_does_not_roll_back_a_newer_turns_planner_state(tmp_path):
    """评审复现：等回执超时后新一轮先提交（新目标、ended=True），旧回执晚到又改回旧值。"""

    from plugins.xiaoqing_chat.planning.goal_state import GoalStore
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore
    from plugins.xiaoqing_chat.turn_generation import commit_planner_snapshot

    store = PFCStateStore()
    goals = GoalStore()
    state = SimpleNamespace(pfc_state_store=store, goal_store=goals)
    runtime = SimpleNamespace(cfg=SimpleNamespace(goal=SimpleNamespace(enable_goal=True)))

    old_turn = store.begin_turn("g1")  # 旧回复取快照，交给 Core 后回执迟迟不来
    new_turn = store.begin_turn("g1")  # 等待超时，新一轮开始
    assert await commit_planner_snapshot(
        state, runtime, "g1", _pfc_state("new-topic", ended=True), turn=new_turn
    )
    assert not await commit_planner_snapshot(
        state, runtime, "g1", _pfc_state("old-topic", ended=False), turn=old_turn
    )

    current = store.get("g1")
    assert current.top_goal == "new-topic"
    assert current.ended is True
    assert (await goals.get_async("g1")).goal == "new-topic"


def test_reply_is_dropped_after_session_reset_even_without_new_messages(tmp_path):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"))
    finalization                              = _fresh_finalization(tmp_path)
    finalization.generated.session_generation = "old-generation"
    with patch.object(turn_delivery, "log_step"):
        assert turn_delivery._drop_stale_generated_turn(finalization, phase="before_commit")


@pytest.mark.parametrize(
    ("later", "event", "quoted"),
    [
        (4, {"group_id": 1, "message_id": 555}, False),  # 还在最近 5 条里：直接接
        (5, {"group_id": 1, "message_id": 555}, True),
        (10, {"group_id": 1, "message_id": 555}, True),
        (6, {"group_id": None, "message_id": 555}, False),
        (6, {"group_id": 1}, False),
    ],
)
def test_reply_quotes_the_target_once_it_is_out_of_the_last_five(tmp_path, later, event, quoted):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), *_later(later))
    finalization = _fresh_finalization(tmp_path, event=event)
    first   = [{"type": "text", "data": {"text": "对"}}]
    batches = [first, [{"type": "text", "data": {"text": "就是这样"}}]]
    turn_delivery._quote_far_target(finalization, batches)
    if quoted:
        assert batches[0] == [{"type": "reply", "data": {"id": "555"}}, *first]
    else:
        assert batches[0] == first
    assert batches[1] == [{"type": "text", "data": {"text": "就是这样"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("later", "checked"),
    [
        ([], False),
        ([("m2", "user", "嘶"), ("m3", "user", "喔？")], True),  # 近处的反应：直接发，不多问
        ([("m2", "user", "对了，明天谁去图书馆")], True),  # 近处有人转话题
        ([("m2", "assistant", "上一轮的回复")], False),  # 之后没有群友说话
        (_later(5), True),  # 已经隔了 5 条：确认话题
        (_later(15), False),  # 太远的交给过期检查
    ],
)
async def test_topic_is_only_checked_when_far_or_someone_changes_the_subject(
    tmp_path, later, checked
):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), *later)
    finalization = _fresh_finalization(tmp_path)
    with (
        patch.object(
            turn_delivery, "reply_should_be_withheld", AsyncMock(return_value=False)
        ) as check,
        patch.object(turn_delivery, "log_step"),
    ):
        assert not await turn_delivery._drop_if_context_changed(finalization)
    assert check.await_count == (1 if checked else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [True, False])
async def test_reply_is_dropped_when_the_group_moved_to_another_topic(tmp_path, changed):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m0", "user", "901 换车底了"), ("m1", "user"), *_later(6))
    finalization = _fresh_finalization(tmp_path)
    with (
        patch.object(
            turn_delivery, "reply_should_be_withheld", AsyncMock(return_value=changed)
        ) as check,
        patch.object(turn_delivery, "log_step") as log,
    ):
        assert await turn_delivery._drop_if_context_changed(finalization) is changed

    kwargs = check.await_args.kwargs
    assert [message.local_id for message in kwargs["before"]] == ["m0", "m1"]
    assert len(kwargs["after"]) == 6
    assert kwargs["reply"] == "对，九十二次改成 901 了"
    assert finalization.generated.cancel_speculative_memory.called is changed
    assert (
        any(call.kwargs.get("step") == "smalltalk.stale.drop" for call in log.call_args_list)
        is changed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [True, False])
@pytest.mark.parametrize(
    "text",
    ["@阿梨 对了，明天谁去图书馆", "@阿梨 话说，明天谁去图书馆", "阿梨，对了，明天谁去图书馆"],
)
async def test_single_addressed_topic_opener_reaches_model(tmp_path, text, changed):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), ("m2", "user", text))
    finalization = _fresh_finalization(tmp_path)
    with (
        patch.object(
            turn_delivery, "reply_should_be_withheld", AsyncMock(return_value=changed)
        ) as check,
        patch.object(turn_delivery, "log_step"),
    ):
        assert await turn_delivery._drop_if_context_changed(finalization) is changed
    check.assert_awaited_once()
    assert [message.content for message in check.await_args.kwargs["after"]] == [text]
    assert finalization.generated.cancel_speculative_memory.called is changed


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["during_check", "waiting_for_lock"])
@pytest.mark.parametrize(
    "new_role,new_text,second_verdict,delivered,checks",
    [
        ("user", "对了，明天谁去图书馆", True, False, 2),
        ("user", "没错，901 就是这辆", False, True, 2),
        ("assistant", "对了，明天谁去图书馆", False, True, 1),
        ("user", "不用回复了", True, False, 2),
    ],
)
async def test_finalization_refreshes_topic_snapshot_before_delivery(
    tmp_path,
    arrival,
    new_role,
    new_text,
    second_verdict,
    delivered,
    checks,
):
    from plugins.xiaoqing_chat import turn_delivery
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts

    _record_messages("g1", ("m1", "user"), *_later(5))
    finalization = _fresh_finalization(tmp_path)
    parts = build_text_message_parts(finalization.generated.reply)
    finalization.generated.reply_payload = turn_delivery.build_reply_payload_from_parts(parts)
    batches = [[{"type": "text", "data": {"text": finalization.generated.reply}}]]
    started = asyncio.Event()
    finish_check = asyncio.Event()
    lock = asyncio.Lock()
    snapshots = []

    async def check(**kwargs):
        snapshots.append([message.local_id for message in kwargs["after"]])
        if len(snapshots) == 1:
            started.set()
            await finish_check.wait()
            return False
        return second_verdict

    with (
        patch.object(turn_delivery, "chat_lock", return_value=lock),
        patch.object(turn_delivery, "reply_should_be_withheld", AsyncMock(side_effect=check)),
        patch.object(turn_delivery, "_prepare_smalltalk_reply_output", return_value=parts),
        patch.object(turn_delivery, "_smalltalk_outbound_batches", return_value=batches),
        patch.object(
            turn_delivery, "_apply_smalltalk_presend_delay", AsyncMock(return_value=False)
        ),
        patch.object(turn_delivery, "_rollback_smalltalk_delivery", AsyncMock()),
        patch.object(turn_delivery, "log_step"),
    ):
        task = asyncio.create_task(
            turn_delivery.finalize_smalltalk_turn(
                SimpleNamespace(forced=True),
                finalization.generated,
                finalization.event,
                finalization.context,
                finalization.hctx,
                started_at=0,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            if arrival == "waiting_for_lock":
                await lock.acquire()
                finish_check.set()
                await asyncio.sleep(0)
            _record_messages("g1", ("m99", new_role, new_text))
            finish_check.set()
            if lock.locked():
                lock.release()
            result = await asyncio.wait_for(task, 1)
            if result:
                await result.delivery_receipt.record(False)
        finally:
            finish_check.set()
            if lock.locked():
                lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert bool(result) is delivered
    assert len(snapshots) == checks
    assert "m99" not in snapshots[0]
    if checks == 2:
        assert snapshots[1][-1] == "m99"
    assert finalization.generated.cancel_speculative_memory.called is not delivered


@pytest.mark.asyncio
async def test_finalization_stops_after_two_outdated_topic_snapshots(tmp_path):
    from plugins.xiaoqing_chat import turn_delivery
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts

    _record_messages("g1", ("m1", "user"), *_later(5))
    finalization                         = _fresh_finalization(tmp_path)
    parts                                = build_text_message_parts(finalization.generated.reply)
    finalization.generated.reply_payload = turn_delivery.build_reply_payload_from_parts(parts)
    checks                               = 0

    async def check(**_kwargs):
        nonlocal checks
        checks += 1
        await asyncio.sleep(0)
        _record_messages("g1", (f"new{checks}", "user", "还有新消息"))
        return False

    with (
        patch.object(turn_delivery, "chat_lock", return_value=asyncio.Lock()),
        patch.object(turn_delivery, "reply_should_be_withheld", AsyncMock(side_effect=check)),
        patch.object(turn_delivery, "_prepare_smalltalk_reply_output", return_value=parts),
        patch.object(
            turn_delivery,
            "_smalltalk_outbound_batches",
            return_value=[[{"type": "text", "data": {"text": "回复"}}]],
        ),
        patch.object(
            turn_delivery, "_apply_smalltalk_presend_delay", AsyncMock(return_value=False)
        ),
        patch.object(turn_delivery, "log_step") as log,
    ):
        result = await asyncio.wait_for(
            turn_delivery.finalize_smalltalk_turn(
                SimpleNamespace(forced=True),
                finalization.generated,
                finalization.event,
                finalization.context,
                finalization.hctx,
                started_at=0,
            ),
            1,
        )
    assert result == []
    assert checks == 2
    finalization.generated.cancel_speculative_memory.assert_called_once()
    assert log.call_args.kwargs["fields"]["reason"] == "topic_snapshot_outdated"


@pytest.mark.asyncio
async def test_delivered_reply_commits_planner_state_despite_interleaved_messages(tmp_path):
    """回复已经发出：后面又有人插话，“刚回复过”的规划状态也要记上，下一轮才会按跟进来规划。"""

    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), ("m2", "user"))
    finalization              = _fresh_finalization(tmp_path)
    finalization.started_at   = 0.0
    finalization.hctx.runtime = SimpleNamespace(
        cfg=SimpleNamespace(debug=SimpleNamespace(log_latency=False))
    )

    def drop_task(_context, coro, *, name):
        coro.close()

    with (
        patch.object(turn_delivery, "chat_lock", return_value=asyncio.Lock()),
        patch.object(
            turn_delivery, "_apply_generated_pfc_state", AsyncMock(return_value=True)
        ) as apply_state,
        patch.object(turn_delivery, "_record_delivered_reply", AsyncMock(return_value=[])),
        patch.object(turn_delivery, "schedule_pfc_state_flush") as flush,
        patch.object(turn_delivery, "spawn_bg_task", side_effect=drop_task),
        patch.object(turn_delivery, "display_reply_text", return_value="对"),
        patch.object(turn_delivery, "log_step"),
    ):
        await turn_delivery._commit_smalltalk_delivery(finalization, (), (101,))

    apply_state.assert_awaited_once_with(finalization)
    flush.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [True, False])
async def test_reply_bubbles_release_the_chat_once_settled(tmp_path, outcome):
    from plugins.xiaoqing_chat import turn_delivery

    finalization = _fresh_finalization(tmp_path)
    limiter      = finalization.hctx.state.generation_limiter
    bubbles      = turn_delivery._ReplyBubbles(finalization, (), [[{"type": "text"}]])
    receipt      = bubbles.receipt(0)
    await bubbles.close()
    assert "g1" in limiter._delivery_holds

    with (
        patch.object(turn_delivery, "_commit_smalltalk_delivery", AsyncMock()),
        patch.object(turn_delivery, "_rollback_smalltalk_delivery", AsyncMock()),
    ):
        await receipt.record(outcome)
    assert "g1" not in limiter._delivery_holds


def _stored(local_id: str, text: str, role: str = "user"):
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    return StoredMessage(
        role=role, name="群友", ts=1_000.0, user_id=1, local_id=local_id, content=text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"withhold_reply": true}', True),
        ('```json\n{"withhold_reply": false}\n```', False),
        ('{"withhold_reply": "true"}', True),  # 只认 JSON 布尔
        ("看不懂", True),
    ],
)
async def test_reply_should_be_withheld_reads_the_planner_verdict(monkeypatch, content, expected):
    from plugins.xiaoqing_chat.llm import topic_shift
    from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig

    captured: dict = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": content}}]}, "profile"

    monkeypatch.setattr(topic_shift, "chat_completions_raw_with_fallback_paths", fake_chat)
    changed = await topic_shift.reply_should_be_withheld(
        secrets  = {},
        bot_name = "小青",
        before   = [_stored("m1", "有可能901/902这对车从京局车变成广局车了？")],
        after    = [_stored("m2", "对了，明天谁去图书馆")],
        reply    = "我猜是换车底了",
        llm=LLMCallConfig(timeout_seconds=30.0, total_timeout_seconds=60.0, max_retry=2),
    )

    assert changed is expected
    assert captured["route"] == "reasoning"
    assert captured["timeout_seconds"] <= 6.0
    assert captured["total_timeout_seconds"] <= 6.0
    assert captured["max_retry"] == 0
    prompt = captured["messages"][0]["content"]
    assert "901/902" in prompt and "明天谁去图书馆" in prompt and "我猜是换车底了" in prompt


@pytest.mark.asyncio
async def test_context_review_withholds_reply_when_model_is_unavailable(monkeypatch):
    from plugins.xiaoqing_chat.llm import topic_shift
    from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig

    async def failing_chat(**_kwargs):
        raise TimeoutError

    monkeypatch.setattr(topic_shift, "chat_completions_raw_with_fallback_paths", failing_chat)
    log    = Mock()
    kwargs = {
        "bot_name": "小青",
        "before"  : [_stored("m1", "901 换车底了")],
        "after"   : [_stored("m2", "对了，明天考试")],
        "reply"   : "真的假的",
        "llm"     : LLMCallConfig(),
        "log"     : log,
    }
    assert await topic_shift.reply_should_be_withheld(secrets={}, **kwargs)
    assert log.call_args.args[0] == "smalltalk.context_check.error"
    assert await topic_shift.reply_should_be_withheld(secrets={"_ai": None}, **kwargs)
