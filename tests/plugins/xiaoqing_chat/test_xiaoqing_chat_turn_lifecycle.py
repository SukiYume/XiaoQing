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
    event                       = dict(sample_group_event)
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
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u1",
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
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u2",
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
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u1",
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
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u1",
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
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u1",
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
    event                       = dict(sample_group_event)
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
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u1",
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
    event                       = dict(sample_group_event)
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
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u1",
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
