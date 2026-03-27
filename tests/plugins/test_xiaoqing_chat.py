"""Tests for xiaoqing_chat plugin"""

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from plugins.xiaoqing_chat.handler_context import HandlerContext, handle_errors


def _make_hctx(
    *,
    runtime,
    state,
    context,
    event=None,
    chat_id="g67890",
    bot_name="小青",
    secrets=None,
    data_dir=None,
) -> HandlerContext:
    """Build a HandlerContext without going through from_event."""
    return HandlerContext(
        chat_id=chat_id,
        runtime=runtime,
        state=state,
        secrets=secrets if secrets is not None else {},
        data_dir=data_dir
        if data_dir is not None
        else (
            context.data_dir if context else Path(tempfile.gettempdir()) / "xiaoqing_chat_test_data"
        ),
        bot_name=bot_name,
        context=context,
    )


ROOT = Path(__file__).resolve().parent.parent.parent

# Import the plugin module using the package structure
from plugins.xiaoqing_chat import main as xiaoqing_chat


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_context(tmp_path: Path):
    """Create a mock plugin context for xiaoqing_chat"""
    context = MagicMock()
    context.config = {"bot_name": "小青"}
    context.secrets = {
        "openai_api_key": "test_key",
        "plugins": {
            "xiaoqing_chat": {"api_key": "test", "api_base": "http://test", "model": "test-model"}
        },
    }
    context.plugin_name = "xiaoqing_chat"
    context.plugin_dir = tmp_path / "plugins" / "xiaoqing_chat"
    context.data_dir = tmp_path / "data" / "xiaoqing_chat"
    context.http_session = AsyncMock()
    context.send_action = AsyncMock()
    context.reload_config = Mock()
    context.reload_plugins = Mock()
    context.list_commands = Mock(return_value=["help: 查看帮助"])
    context.list_plugins = Mock(return_value=["xiaoqing_chat"])
    context.current_user_id = 12345
    context.current_group_id = 67890
    context.request_id = "test-request-123"
    context.state = {}
    context.logger = MagicMock()
    context.session_manager = None
    context.config_manager = MagicMock()
    return context


@pytest.fixture
def sample_group_event():
    """Create a sample group message event"""
    return {
        "post_type": "message",
        "message_type": "group",
        "time": 1234567890,
        "self_id": 11111,
        "user_id": 12345,
        "group_id": 67890,
        "message": [{"type": "text", "data": {"text": "/xc 你好"}}],
        "raw_message": "/xc 你好",
        "font": 0,
        "sender": {
            "user_id": 12345,
            "nickname": "TestUser",
            "card": "",
            "sex": "unknown",
            "age": 0,
            "area": "",
            "level": "",
            "role": "member",
            "title": "",
        },
        "message_id": 1,
        "message_seq": 1,
    }


@pytest.fixture
def sample_private_event():
    """Create a sample private message event"""
    return {
        "post_type": "message",
        "message_type": "private",
        "time": 1234567890,
        "self_id": 11111,
        "user_id": 12345,
        "message": "你好",
        "raw_message": "你好",
        "font": 0,
        "sender": {
            "user_id": 12345,
            "nickname": "TestUser",
            "sex": "unknown",
            "age": 0,
        },
        "message_id": 1,
    }


# ============================================================
# Main Module Tests
# ============================================================


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_init(mock_context):
    """Test plugin initialization"""
    # Should not raise
    xiaoqing_chat.init(mock_context)
    mock_context.logger.info.assert_called()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_chat_command(mock_context, sample_group_event):
    """Test handle with chat command"""
    from plugins.xiaoqing_chat.main import handle
    from core.args import parse

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_smalltalk:
        result = await handle(
            command="xc",
            args="你好",
            event=sample_group_event,
            context=mock_context,
        )

        mock_smalltalk.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_help_command(mock_context, sample_group_event):
    """Test handle with help subcommand"""
    result = await xiaoqing_chat.handle(
        command="xc",
        args="help",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "小青智能对话" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_reset_command(mock_context, sample_group_event):
    """Test handle with reset subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        result = await handle(
            command="xc",
            args="reset",
            event=sample_group_event,
            context=mock_context,
        )

        mock_internal.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_stats_command(mock_context, sample_group_event):
    """Test handle with stats subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        result = await handle(
            command="xc",
            args="stats",
            event=sample_group_event,
            context=mock_context,
        )

        mock_internal.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_brain_command(mock_context, sample_group_event):
    """Test handle with brain subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        result = await handle(
            command="xc",
            args="brain",
            event=sample_group_event,
            context=mock_context,
        )

        mock_internal.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_unknown_command(mock_context, sample_group_event):
    """Test handle with unknown subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk",
        new=AsyncMock(return_value=[{"type": "text", "data": {"text": "mock_response"}}]),
    ) as mock_smalltalk:
        result = await handle(
            command="xc",
            args="unknown_subcommand",
            event=sample_group_event,
            context=mock_context,
        )

        # Should be treated as smalltalk
        mock_smalltalk.assert_called_once()
        assert len(result) > 0


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_empty_args(mock_context, sample_group_event):
    """Test handle with empty args"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    text = result[0]["data"]["text"]
    assert "小青智能对话" in text or "可用命令" in text


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_exception(mock_context, sample_group_event):
    """Test handle handles exceptions gracefully"""
    from plugins.xiaoqing_chat.main import handle

    with patch("plugins.xiaoqing_chat.main.parse", side_effect=Exception("Test error")):
        result = await handle(
            command="xc",
            args="test",
            event=sample_group_event,
            context=mock_context,
        )

        # Should return error message
        assert len(result) > 0


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_config_command(mock_context, sample_group_event):
    """Test handle with /xc 配置 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="配置",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "配置" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_memory_command(mock_context, sample_group_event):
    """Test handle with /xc 记忆 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="记忆",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "记忆" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_expression_command(mock_context, sample_group_event):
    """Test handle with /xc 表达 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="表达",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "表达" in result[0]["data"]["text"]


# ============================================================
# Help Text Tests
# ============================================================


@pytest.mark.plugin
def test_xiaoqing_chat_show_help():
    """Test _show_help returns help text"""
    from plugins.xiaoqing_chat.main import _show_help

    help_text = _show_help()

    assert "小青智能对话" in help_text
    assert "/xc" in help_text
    assert "清空" in help_text
    assert "统计" in help_text
    assert "深度" in help_text


@pytest.mark.plugin
def test_xiaoqing_chat_show_help_contains_all_sections():
    """Test help contains all major sections"""
    from plugins.xiaoqing_chat.main import _show_help

    help_text = _show_help()

    assert "基础对话" in help_text or "基础" in help_text
    assert "会话管理" in help_text or "会话" in help_text
    assert "高级功能" in help_text or "高级" in help_text
    assert "使用提示" in help_text or "提示" in help_text


# ============================================================
# Bot Name Only Tests
# ============================================================


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_call_bot_name_only_internal(mock_context):
    """Test call_bot_name_only_internal returns response"""
    from plugins.xiaoqing_chat.main import call_bot_name_only_internal

    result = await call_bot_name_only_internal(mock_context)

    assert isinstance(result, list)
    assert len(result) > 0
    assert result[0]["type"] == "text"


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_call_bot_name_only_internal_varies_response(mock_context):
    """Test call_bot_name_only_internal can return different responses"""
    from plugins.xiaoqing_chat.main import call_bot_name_only_internal

    responses = set()
    for _ in range(20):
        result = await call_bot_name_only_internal(mock_context)
        if result:
            responses.add(result[0]["data"]["text"])

    # Should have at least some variety
    assert len(responses) > 0


# ============================================================
# Shutdown Tests
# ============================================================


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_shutdown(mock_context):
    """Test plugin shutdown"""
    from plugins.xiaoqing_chat.main import shutdown

    # Should not raise
    await shutdown(mock_context)
    mock_context.logger.info.assert_called()


# ============================================================
# Command Variants Tests
# ============================================================


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_chat_command_variants(mock_context, sample_group_event):
    """Test handle recognizes chat via /xc <content>"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_smalltalk:
        await handle(
            command="xc",
            args="test",
            event=sample_group_event,
            context=mock_context,
        )

        assert mock_smalltalk.call_count == 1


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_reset_command_variants(mock_context, sample_group_event):
    """Test handle recognizes reset subcommand aliases"""
    from plugins.xiaoqing_chat.main import handle

    subcommands = ["reset", "重置", "清空"]

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        for sub in subcommands:
            await handle(
                command="xc",
                args=sub,
                event=sample_group_event,
                context=mock_context,
            )

        assert mock_internal.call_count == len(subcommands)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_stats_command_variants(mock_context, sample_group_event):
    """Test handle recognizes stats subcommand aliases"""
    from plugins.xiaoqing_chat.main import handle

    subcommands = ["stats", "统计", "状态"]

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        for sub in subcommands:
            await handle(
                command="xc",
                args=sub,
                event=sample_group_event,
                context=mock_context,
            )

        assert mock_internal.call_count == len(subcommands)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_brain_command_variants(mock_context, sample_group_event):
    """Test handle recognizes brain subcommand aliases"""
    from plugins.xiaoqing_chat.main import handle

    subcommands = ["brain", "深度"]

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        for sub in subcommands:
            await handle(
                command="xc",
                args=sub,
                event=sample_group_event,
                context=mock_context,
            )

        assert mock_internal.call_count == len(subcommands)


# ============================================================
# Integration Tests
# ============================================================


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_smalltalk_called_with_correct_params(mock_context, sample_group_event):
    """Test handle_smalltalk is called with correct parameters"""
    from plugins.xiaoqing_chat.main import handle

    sample_group_event["raw_message"] = "/xc 测试消息"

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_smalltalk:
        await handle(
            command="xc",
            args="测试消息",
            event=sample_group_event,
            context=mock_context,
        )

        # Verify handle_smalltalk was called with args (not raw_message)
        call_args = mock_smalltalk.call_args
        assert call_args[0][0] == "测试消息"  # clean_text
        assert call_args[0][1] == sample_group_event  # event
        assert call_args[0][2] == mock_context  # context


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_internal_called_with_correct_params(mock_context, sample_group_event):
    """Test handle_internal is called with correct parameters"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        await handle(
            command="xc",
            args="reset",
            event=sample_group_event,
            context=mock_context,
        )

        # Verify handle_internal was called
        call_args = mock_internal.call_args
        assert call_args[0][0] == "重置"  # command


# ============================================================
# Edge Cases Tests
# ============================================================


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_no_raw_message(mock_context):
    """Test handle with event missing raw_message routes to smalltalk"""
    from plugins.xiaoqing_chat.main import handle

    event = {"post_type": "message"}  # No raw_message

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_st:
        result = await handle(
            command="xc",
            args="test",
            event=event,
            context=mock_context,
        )

        # Should route to handle_smalltalk with forced flag
        assert result == []
        assert mock_st.called
        assert event.get("_xc_command_forced") is True


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_empty_raw_message(mock_context, sample_group_event):
    """Test handle with empty raw_message"""
    from plugins.xiaoqing_chat.main import handle

    sample_group_event["raw_message"] = ""

    result = await handle(
        command="xc",
        args="",
        event=sample_group_event,
        context=mock_context,
    )

    # Should return help text when no args provided
    assert len(result) > 0
    assert "小青智能对话" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_whitespace_args(mock_context, sample_group_event):
    """Test handle with whitespace-only args"""
    from plugins.xiaoqing_chat.main import handle

    with patch("core.args.parse") as mock_parse:
        mock_parse.return_value = MagicMock(first=None, rest=Mock(return_value=""))

        result = await handle(
            command="xc",
            args="   ",
            event=sample_group_event,
            context=mock_context,
        )

        # Should handle gracefully and return help
        assert len(result) > 0


@pytest.mark.asyncio
async def test_runtime_cleanup_eviction_also_cleans_locks_and_persist_tasks():
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    old_limit = ChatRuntimeState._MAX_TRACKED_CHATS
    setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", 2)
    state = ChatRuntimeState()
    try:
        loop = asyncio.get_running_loop()
        for i in range(5):
            cid = f"g{i}"
            state.get_lock(cid)
            state.set_last_reply_ts(cid, float(i))
            done_task = loop.create_future()
            done_task.set_result(None)
            state.set_persist_task(cid, cast(Any, done_task))

        state.cleanup_stale_chats()

        assert len(state._per_chat.locks) <= 2
        assert len(state._per_chat.persist_tasks) <= 2
    finally:
        setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", old_limit)


@pytest.mark.asyncio
async def test_runtime_cleanup_stale_prefers_recent_observe_activity():
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    old_limit = ChatRuntimeState._MAX_TRACKED_CHATS
    setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", 1)
    state = ChatRuntimeState()
    try:
        state.get_lock("g_observe")
        state.get_lock("g_reply")
        state.set_last_reply_ts("g_reply", 100.0)
        state.set_last_observe_ts("g_reply", 100.0)
        state.set_last_observe_ts("g_observe", 200.0)

        state.cleanup_stale_chats()

        assert "g_observe" in state._per_chat.locks
        assert "g_reply" not in state._per_chat.locks
    finally:
        setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", old_limit)


@pytest.mark.asyncio
async def test_action_history_flush_task_entry_removed_after_task_done():
    from plugins.xiaoqing_chat.task_scheduler import (
        _action_flush_tasks,
        _schedule_action_history_flush,
    )

    context = MagicMock()
    runtime = cast(Any, SimpleNamespace(cfg=SimpleNamespace(io_persist_debounce_seconds=0.0)))
    fake_state = MagicMock()

    _action_flush_tasks.clear()
    try:
        with (
            patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=fake_state),
            patch(
                "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
                new=AsyncMock(return_value=None),
            ),
        ):
            _schedule_action_history_flush(context, runtime, chat_id="g1")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert "g1" not in _action_flush_tasks
    finally:
        _action_flush_tasks.clear()


# ---- interest scorer tests ----
from plugins.xiaoqing_chat.frequency_control import _score_interest


class TestScoreInterest:
    def test_question_mark_is_high(self):
        assert _score_interest("这是什么？") == "high"

    def test_ascii_question_mark_is_high(self):
        assert _score_interest("what?") == "high"

    def test_exclamation_is_high(self):
        assert _score_interest("竟然还有这么离谱的项目！") == "high"

    def test_laugh_word_is_high(self):
        assert _score_interest("哈哈哈太好笑了") == "high"

    def test_life_keyword_is_high(self):
        assert _score_interest("没有什么是一顿火锅解决不了的") == "high"

    def test_question_ending_is_high(self):
        assert _score_interest("你觉得这样行吗") == "high"

    def test_short_text_is_low(self):
        assert _score_interest("哦") == "low"

    def test_empty_is_low(self):
        assert _score_interest("") == "low"

    def test_pure_url_is_low(self):
        assert _score_interest("https://example.com/page") == "low"

    def test_pure_number_is_low(self):
        assert _score_interest("12345") == "low"

    def test_normal_statement_is_neutral(self):
        assert _score_interest("今天天气不错") == "neutral"

    @pytest.mark.asyncio
    async def test_interest_affects_probability_high(self):
        """High interest score allows reply when neutral would be blocked (random=0.7 > base=0.6 but < scaled=0.95)."""
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.reply_probability_private = 0.95
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False
        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=1.0)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        # random=0.7: neutral blocked (0.7 >= 0.6), high allowed (0.7 < 0.95)
        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.7
            result_high = await _should_reply(
                runtime, state, "g1", "火锅好吃吗？", False, False, False, interest="high"
            )
            mock_rand.random.return_value = 0.7
            result_neutral = await _should_reply(
                runtime, state, "g1", "火锅好吃吗？", False, False, False, interest="neutral"
            )
        assert result_high is True
        assert result_neutral is False

    def test_ascii_exclamation_is_high(self):
        assert _score_interest("wow!") == "high"

    def test_question_ending_variants_are_high(self):
        assert _score_interest("是这样嘛") == "high"
        assert _score_interest("真的啊") == "high"
        assert _score_interest("去哪里呢") == "high"
        assert _score_interest("你也去吧") == "high"
        assert _score_interest("是诶") == "high"

    @pytest.mark.asyncio
    async def test_interest_affects_probability_low(self):
        """低兴趣度消息概率降低，random=0.5 时应该被拦截（base*0.2=0.12 < 0.5）"""
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.reply_probability_private = 0.95
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False
        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=1.0)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        # base=0.6, low → p=0.12, random=0.5 → 0.5 >= 0.12 → False
        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.5
            result = await _should_reply(
                runtime, state, "g1", "哦", False, False, False, interest="low"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_heartflow_low_score_does_not_reduce_group_base_probability(self):
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.reply_probability_private = 0.95
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = True
        runtime.cfg.heartflow.base_score = 0.2
        runtime.cfg.heartflow.threshold = 0.5
        runtime.cfg.heartflow.weight_private = 0.55
        runtime.cfg.heartflow.weight_mentioned = 0.45
        runtime.cfg.heartflow.weight_question = 0.12
        runtime.cfg.heartflow.weight_goal_match = 0.06
        runtime.cfg.heartflow.weight_short_text = -0.08
        runtime.cfg.heartflow.weight_rate_limit = -0.35
        runtime.cfg.heartflow.weight_cooldown = -0.45
        runtime.cfg.heartflow.weight_interval = -0.25
        runtime.cfg.heartflow.weight_no_reply_streak = 0.05
        runtime.cfg.heartflow.weight_long_silence = 0.08
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=0.2)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with (
            patch("plugins.xiaoqing_chat.frequency_control._get_talk_value", return_value=1.0),
            patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand,
        ):
            mock_rand.random.return_value = 0.55
            result = await _should_reply(
                runtime, state, "g1", "今天天气不错", False, False, False, interest="neutral"
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_group_heartflow_bonus_stays_soft(self):
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.reply_probability_private = 0.95
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = True
        runtime.cfg.heartflow.base_score = 0.2
        runtime.cfg.heartflow.threshold = 0.5
        runtime.cfg.heartflow.weight_private = 0.55
        runtime.cfg.heartflow.weight_mentioned = 0.45
        runtime.cfg.heartflow.weight_question = 0.12
        runtime.cfg.heartflow.weight_goal_match = 0.06
        runtime.cfg.heartflow.weight_short_text = -0.08
        runtime.cfg.heartflow.weight_rate_limit = -0.35
        runtime.cfg.heartflow.weight_cooldown = -0.45
        runtime.cfg.heartflow.weight_interval = -0.25
        runtime.cfg.heartflow.weight_no_reply_streak = 0.05
        runtime.cfg.heartflow.weight_long_silence = 0.08
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=0.32)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with (
            patch("plugins.xiaoqing_chat.frequency_control._get_talk_value", return_value=1.0),
            patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand,
        ):
            mock_rand.random.return_value = 0.67
            result = await _should_reply(
                runtime, state, "g1", "今天天气不错？", False, False, False, interest="neutral"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_long_no_reply_streak_can_break_through_low_interest_group_chatter(self):
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.5
        runtime.cfg.reply_probability_private = 0.95
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=1.0)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=12))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with (
            patch("plugins.xiaoqing_chat.frequency_control._get_talk_value", return_value=1.0),
            patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand,
        ):
            mock_rand.random.return_value = 0.3
            result = await _should_reply(
                runtime, state, "g1", "哦", False, False, False, interest="low"
            )

        assert result is True

    def test_emo_substring_in_english_word_is_neutral(self):
        # "demo" contains "emo" — should not trigger high
        assert _score_interest("Here is a demo") == "neutral"

    def test_standalone_emo_is_high(self):
        # Chinese slang "emo了" — should trigger high
        assert _score_interest("好emo啊") == "high"


def test_group_wait_plan_is_preserved_before_first_reply(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id = "group-1"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="有人在聊火锅")

    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()
    generate_reply = AsyncMock(return_value="我也想吃火锅")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(
                action="wait", reason="先看看", thinking="群里还在聊", wait_seconds=20
            )
        ),
    ):
        result = asyncio.run(
            run_pfc_once(
                context=context,
                runtime_cfg=cfg,
                secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
                bot_name="小青",
                is_private=False,
                chat_id=chat_id,
                current_text="有人在聊火锅",
                memory_store=memory_store,
                action_history=action_history,
                memory_db=memory_db,
                pfc_state_store=pfc_state_store,
                generate_reply=generate_reply,
            )
        )

    assert result.action == "wait"
    assert result.reply == ""
    generate_reply.assert_not_awaited()


def test_group_wait_plan_is_preserved_after_bot_already_replied(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id = "group-2"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="刚刚说过了")
    memory_store.append(chat_id, role="user", name="Tester", content="收到")

    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="不该发送")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(action="wait", reason="先等等", thinking="刚说完", wait_seconds=20)
        ),
    ):
        result = asyncio.run(
            run_pfc_once(
                context=context,
                runtime_cfg=cfg,
                secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
                bot_name="小青",
                is_private=False,
                chat_id=chat_id,
                current_text="收到",
                memory_store=memory_store,
                action_history=action_history,
                memory_db=memory_db,
                pfc_state_store=pfc_state_store,
                generate_reply=generate_reply,
            )
        )

    assert result.action == "wait"
    assert result.reply == ""
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_store_get_async_loads_via_to_thread(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "threaded-load"
    store = MemoryStore(tmp_path)
    store.persist(chat_id)
    (tmp_path / f"{chat_id}.json").write_text(
        '[{"role":"user","name":"Tester","content":"hello","ts":1.0}]',
        encoding="utf-8",
    )

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch(
        "plugins.xiaoqing_chat.memory.memory.asyncio.to_thread",
        new=AsyncMock(side_effect=fake_to_thread),
    ) as mock_to_thread:
        history = await store.get_async(chat_id)

    assert mock_to_thread.await_count == 1
    assert len(history) == 1
    assert history[0].content == "hello"


@pytest.mark.asyncio
async def test_memory_store_reloads_after_binding_data_dir(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "rebinding-load"
    store = MemoryStore()

    assert await store.get_async(chat_id) == []

    store.bind_data_dir(tmp_path)
    (tmp_path / f"{chat_id}.json").write_text(
        '[{"role":"user","name":"Tester","content":"restored","ts":1.0}]',
        encoding="utf-8",
    )

    history = await store.get_async(chat_id)

    assert len(history) == 1
    assert history[0].content == "restored"


def test_memory_store_append_prunes_in_memory_history():
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    store = MemoryStore()
    chat_id = "bounded-history"

    for index in range(250):
        store.append(chat_id, role="user", name="Tester", content=f"msg-{index}")

    history = store.get(chat_id)
    assert len(history) == 200
    assert history[0].content == "msg-50"
    assert history[-1].content == "msg-249"


def test_get_data_dir_prefers_context_data_dir_even_when_plugin_data_exists(mock_context):
    from plugins.xiaoqing_chat.handlers import _get_data_dir

    assert _get_data_dir(mock_context) == mock_context.data_dir


@pytest.mark.asyncio
async def test_handle_internal_stats_uses_async_memory_read(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                brain_max_context_size=30,
                brain_think_level=2,
                brain_temperature=0.7,
            ),
            memory=SimpleNamespace(enable_memory_retrieval=True, top_k=5, min_score=0.1),
            expression=SimpleNamespace(
                enable_expression_learning=True, max_injected=5, max_store=200
            ),
            reply_probability_base=0.6,
            reply_probability_private=0.95,
            min_reply_interval_seconds=12.0,
            max_replies_per_minute=6,
            max_context_size=30,
        )
    )

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get.side_effect = AssertionError("sync memory read should not be used")
    state.bw_expr_store.load.return_value = []
    state.bw_jargon_store.load.return_value = {}
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.action_history.get_recent.return_value = []
    state.get_stats.return_value = {"replies": 1, "resets": 0}

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("统计", "", sample_group_event, mock_context)

    assert state.memory_store.get_async.await_count == 1
    assert "会话统计" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_internal_stats_uses_async_action_history_read(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                brain_max_context_size=30,
                brain_think_level=2,
                brain_temperature=0.7,
            ),
            memory=SimpleNamespace(enable_memory_retrieval=True, top_k=5, min_score=0.1),
            expression=SimpleNamespace(
                enable_expression_learning=True, max_injected=5, max_store=200
            ),
            reply_probability_base=0.6,
            reply_probability_private=0.95,
            min_reply_interval_seconds=12.0,
            max_replies_per_minute=6,
            max_context_size=30,
        )
    )

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.bw_expr_store.load.return_value = []
    state.bw_jargon_store.load.return_value = {}
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.action_history.get_recent.side_effect = AssertionError(
        "sync action history read should not be used"
    )
    state.get_stats.return_value = {"replies": 1, "resets": 0}

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("统计", "", sample_group_event, mock_context)

    assert state.action_history.get_recent_async.await_count == 1
    assert "近期行动记录" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_extract_and_learn_uses_async_memory_read(mock_context):
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )

    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")
    expr_store = MagicMock()
    recorder = MessageRecorder()

    changed = await extract_and_learn(
        context=mock_context,
        secrets={},
        bot_name="小青",
        chat_id="g1",
        memory_store=memory_store,
        expr_store=expr_store,
        jargon_store=None,
        recorder=recorder,
        personality=MagicMock(),
        min_interval_seconds=0.0,
        min_messages=10,
        self_reflect=True,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert changed == 0
    assert memory_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_tick_reflect_tracker_uses_async_memory_read(mock_context):
    from plugins.xiaoqing_chat.expression.bw_reflect_tracker import (
        ReflectTrackerState,
        tick_reflect_tracker,
    )

    tracker_store = MagicMock()
    tracker_store.get_tracker.return_value = ReflectTrackerState(
        operator_chat_id="g1",
        expression_id="expr-1",
        created_time=1.0,
        last_check_count=0,
    )
    expr_store = MagicMock()
    expr_store.load.return_value = []
    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")

    with patch("plugins.xiaoqing_chat.expression.bw_reflect_tracker.time.time", return_value=10.0):
        result = await tick_reflect_tracker(
            context=mock_context,
            operator_chat_id="g1",
            memory_store=memory_store,
            expr_store=expr_store,
            tracker_store=tracker_store,
            secrets={},
            timeout_seconds=1.0,
            max_retry=0,
            retry_interval_seconds=0.0,
            proxy="",
            endpoint_path="/v1/chat/completions",
            max_duration_seconds=900.0,
            max_message_count=30,
        )

    assert result is False
    assert memory_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_action_history_get_recent_async_loads_via_to_thread(tmp_path):
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    chat_id = "action-history-load"
    store = ActionHistoryStore()
    store.bind(tmp_path)
    action_dir = tmp_path / "action_history"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / f"{chat_id}.json").write_text(
        '[{"ts":1.0,"local_target":"u1","action":"reply","reasoning":"ok","detail":{},"executed":true}]',
        encoding="utf-8",
    )

    items = await store.get_recent_async(chat_id, max_items=10)

    assert len(items) == 1
    assert items[0].action == "reply"


@pytest.mark.asyncio
async def test_build_tool_info_block_uses_async_action_history_read(mock_context):
    from plugins.xiaoqing_chat.context_builder import _build_tool_info_block

    state = MagicMock()
    state.get_last_reply_ts.return_value = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value = []
    state.get_continuous_reply_count.return_value = 0
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.action_history.get_recent.side_effect = AssertionError(
        "sync action history read should not be used"
    )

    block = await _build_tool_info_block(
        runtime=MagicMock(),
        state=state,
        data_dir=mock_context.data_dir,
        bot_name="小青",
        chat_id="g1",
        event={"user_id": 1},
        goal="",
    )

    assert isinstance(block, str)
    assert state.action_history.get_recent_async.await_count == 1


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_uses_async_heartflow(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded

    runtime = MagicMock()
    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_user_message.side_effect = AssertionError(
        "sync heartflow update should not be used"
    )

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._get_data_dir", return_value=mock_context.data_dir),
        patch("plugins.xiaoqing_chat.handlers._bind_all_stores"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
    ):
        local_id = await _ensure_user_message_recorded(
            "你好", sample_group_event, mock_context, runtime
        )

    assert local_id
    assert state.heartflow.on_user_message_async.await_count == 1


@pytest.mark.asyncio
async def test_run_pfc_once_uses_async_pfc_state_store(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once

    chat_id = "pfc-async-state"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="你好")
    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = MagicMock()
    pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
        )
    )
    pfc_state_store.get.side_effect = AssertionError("sync pfc state read should not be used")
    pfc_state_store.save_async = AsyncMock()
    pfc_state_store.save.side_effect = AssertionError("sync pfc state write should not be used")
    generate_reply = AsyncMock(return_value="ok")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(action="wait", reason="先等等", thinking="观察中", wait_seconds=20)
        ),
    ):
        result = await run_pfc_once(
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="你好",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert result.action == "wait"
    assert pfc_state_store.get_async.await_count == 1


def test_pfc_state_set_state_refreshes_updated_at():
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore

    store = PFCStateStore()
    state = PFCConversationState(chat_id="g1", updated_at=1.0)

    before = state.updated_at
    store.set_state("g1", state)

    assert state.updated_at > before
    assert store._cache["g1"] is state


@pytest.mark.asyncio
async def test_smalltalk_goal_path_uses_async_goal_store(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.goal_store.set_async = AsyncMock()
    state.goal_store.set.side_effect = AssertionError("sync goal store write should not be used")
    state.pfc_state_store.get_async = AsyncMock(return_value=SimpleNamespace(goal_list=[]))

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event["_xc_command_forced"] = True

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async", new=AsyncMock(return_value="目标")
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="ok")),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "ok"}}]
    assert state.goal_store.set_async.await_count == 1


@pytest.mark.asyncio
async def test_should_reply_uses_async_goal_and_heartflow_state():
    from plugins.xiaoqing_chat.frequency_control import _should_reply

    runtime = MagicMock()
    runtime.cfg.goal.enable_goal = True
    runtime.cfg.min_reply_interval_seconds = 0.0
    runtime.cfg.max_replies_per_minute = 0
    runtime.cfg.reply_probability_private = 0.95
    runtime.cfg.reply_probability_base = 0.6
    runtime.cfg.heartflow.enable_heartflow = True
    runtime.cfg.heartflow.base_score = 0.2
    runtime.cfg.heartflow.threshold = 0.5
    runtime.cfg.heartflow.weight_private = 0.55
    runtime.cfg.heartflow.weight_mentioned = 0.45
    runtime.cfg.heartflow.weight_question = 0.12
    runtime.cfg.heartflow.weight_goal_match = 0.06
    runtime.cfg.heartflow.weight_short_text = -0.08
    runtime.cfg.heartflow.weight_no_reply_streak = 0.05
    runtime.cfg.heartflow.weight_long_silence = 0.08

    state = MagicMock()
    state.get_last_reply_ts.return_value = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value = []
    state.set_reply_timestamps = Mock()
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="回答用户问题"))
    state.goal_store.get.side_effect = AssertionError("sync goal store read should not be used")
    state.heartflow.score_async = AsyncMock(return_value=0.7)
    state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
    state.heartflow._load.side_effect = AssertionError("sync heartflow load should not be used")

    with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.0):
        result = await _should_reply(
            runtime, state, "g1", "你好？", False, False, False, interest="high"
        )

    assert result is True
    assert state.goal_store.get_async.await_count == 1
    assert state.heartflow.get_async.await_count == 1


@pytest.mark.asyncio
async def test_ordinary_group_turn_refreshes_goal_before_should_reply_blocks(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(return_value=SimpleNamespace(goal_list=[]))

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._score_interest", return_value="low"),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ) as mock_derive_goal,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    mock_derive_goal.assert_awaited_once()
    state.goal_store.set_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_group_with_planner_top_goal_skips_heuristic_goal_refresh_before_reply_gate(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[{"goal": "已有规划目标"}],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._score_interest", return_value="low"),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ) as mock_derive_goal,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    mock_derive_goal.assert_not_awaited()
    state.goal_store.set_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_group_with_empty_planner_goal_list_still_refreshes_goal_before_reply_gate(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._score_interest", return_value="low"),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ) as mock_derive_goal,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    mock_derive_goal.assert_awaited_once()
    state.goal_store.set_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_group_pre_pfc_block_still_updates_no_reply_adaptation(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(return_value=SimpleNamespace(goal_list=[]))

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._score_interest", return_value="low"),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    state.heartflow.on_no_reply_async.assert_awaited_once()


def test_follow_up_compact_prompt_explicitly_allows_brief_interjection_on_new_group_content():
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PROMPT_FOLLOW_UP_COMPACT

    assert "有新内容时可以简短插一句" in PROMPT_FOLLOW_UP_COMPACT


def test_long_non_question_turn_does_not_fallback_to_stale_topic_title():
    from plugins.xiaoqing_chat.planning.goal_state import _derive_goal_from_context

    goal = _derive_goal_from_context(
        current_text="今天我主要在整理部署流程、补文档、顺便看了下监控曲线，没有新的问题要问",
        planner_reasoning="",
        topic="昨晚旧话题",
    )

    assert goal == "自然聊天"


@pytest.mark.asyncio
async def test_topic_summarizer_refreshes_from_observed_traffic_not_only_exact_modulo(tmp_path):
    from plugins.xiaoqing_chat.llm.summarizer import maybe_update_topic_summary
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    data_dir = tmp_path
    chat_id = "g-refresh"
    cache_path = data_dir / "hippo_memorizer" / f"{chat_id}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        '[{"topic_id":"1","topic":"旧话题","keywords":["旧"],"summary":"旧摘要","key_points":["旧要点"],"updated_at":1.0}]',
        encoding="utf-8",
    )

    history = [
        StoredMessage(role="user", content="A", name="U", ts=1.0),
        StoredMessage(role="assistant", content="B", name="小青", ts=2.0),
        StoredMessage(role="user", content="C", name="U", ts=3.0),
        StoredMessage(role="assistant", content="D", name="小青", ts=4.0),
        StoredMessage(role="user", content="E", name="U", ts=5.0),
    ]

    memory_db = MagicMock()
    with patch(
        "plugins.xiaoqing_chat.llm.summarizer.chat_completions",
        new=AsyncMock(
            return_value='{"topic":"新话题","keywords":["新"],"summary":"新摘要","key_points":["要点"]}'
        ),
    ) as mock_chat:
        await maybe_update_topic_summary(
            data_dir=data_dir,
            memory_db=memory_db,
            http_session=AsyncMock(),
            secrets={"api_base": "http://test", "api_key": "k", "model": "m"},
            bot_name="小青",
            chat_id=chat_id,
            history=history,
            min_messages_per_update=4,
            max_cache_topics=5,
            temperature=0.6,
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=1.0,
            max_retry=0,
            retry_interval_seconds=0.1,
            proxy="",
            endpoint_path="/v1/chat/completions",
        )

    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_post_reply_bg_tasks_uses_async_goal_and_action_history_reads(mock_context):
    from plugins.xiaoqing_chat.handlers_helper import _spawn_post_reply_bg_tasks

    runtime = MagicMock()
    runtime.cfg.summarizer.enable_topic_summarizer = False
    runtime.cfg.expression.enable_expression_learning = False
    runtime.cfg.reflection.enable_review_sessions = True
    runtime.cfg.reflection.session_timeout_seconds = 60.0
    runtime.cfg.reflection.session_cooldown_seconds = 60.0
    runtime.cfg.reflection.operator_user_id = 1
    runtime.cfg.reflection.operator_group_id = 2
    runtime.cfg.reflection.resend_interval_seconds = 30.0

    state = MagicMock()
    state.review_store.cleanup_expired = Mock()
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.action_history.get_recent.side_effect = AssertionError(
        "sync action history read should not be used"
    )
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="自然聊天"))
    state.goal_store.get.side_effect = AssertionError("sync goal store read should not be used")
    state.get_continuous_reply_count.return_value = 0

    with (
        patch(
            "plugins.xiaoqing_chat.handlers_helper._spawn_bg_task",
            side_effect=lambda _context, coro, name=None: coro.close(),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers_helper.maybe_open_goal_strategy_review",
            return_value=None,
        ),
        patch("plugins.xiaoqing_chat.handlers_helper._log_step"),
    ):
        hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, chat_id="g1")
        await _spawn_post_reply_bg_tasks(
            hctx=hctx,
            history_snapshot=[],
            event={"message_type": "group"},
        )

    assert state.action_history.get_recent_async.await_count == 1
    assert state.goal_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_handle_internal_reset_uses_async_pfc_state_store(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = MagicMock()
    state = MagicMock()
    pfc_state = SimpleNamespace(
        ended=True,
        ignore_until_ts=1.0,
        last_successful_reply_action="reply",
        goal_list=[{"goal": "x"}],
        knowledge_list=[{"text": "y"}],
    )
    state.pfc_state_store.get_async = AsyncMock(return_value=pfc_state)
    state.pfc_state_store.get.side_effect = AssertionError("sync pfc state read should not be used")
    state.pfc_state_store.save_async = AsyncMock()
    state.pfc_state_store.save.side_effect = AssertionError(
        "sync pfc state write should not be used"
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.pfc_state_store.get_async.await_count == 1
    assert state.pfc_state_store.save_async.await_count == 1


@pytest.mark.asyncio
async def test_schedule_action_history_flush_uses_to_thread(mock_context):
    from plugins.xiaoqing_chat.task_scheduler import (
        _schedule_action_history_flush,
        _action_flush_tasks,
    )

    runtime = MagicMock()
    runtime.cfg.io_persist_debounce_seconds = 0.0
    state = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
        patch("plugins.xiaoqing_chat.task_scheduler._track_bg_task"),
        patch(
            "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
            new=AsyncMock(side_effect=fake_to_thread),
        ) as mock_to_thread,
    ):
        _schedule_action_history_flush(mock_context, runtime, chat_id="g1")
        task = _action_flush_tasks.get("g1")
        assert task is not None
        await task

    assert mock_to_thread.await_count == 1
    state.action_history.flush.assert_called_once_with("g1")
    # cleanup
    _action_flush_tasks.pop("g1", None)


@pytest.mark.asyncio
async def test_schedule_pfc_state_flush_uses_to_thread(mock_context):
    from plugins.xiaoqing_chat.task_scheduler import (
        _schedule_pfc_state_flush,
        _pfc_state_flush_tasks,
    )

    runtime = MagicMock()
    runtime.cfg.io_persist_debounce_seconds = 0.0
    state = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
        patch("plugins.xiaoqing_chat.task_scheduler._track_bg_task"),
        patch(
            "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
            new=AsyncMock(side_effect=fake_to_thread),
        ) as mock_to_thread,
    ):
        _schedule_pfc_state_flush(mock_context, runtime, chat_id="g1")
        task = _pfc_state_flush_tasks.get("g1")
        assert task is not None
        await task

    assert mock_to_thread.await_count == 1
    state.pfc_state_store.save.assert_called_once_with("g1")
    _pfc_state_flush_tasks.pop("g1", None)


@pytest.mark.asyncio
async def test_smalltalk_planner_runs_outside_chat_lock_but_commits_inside(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent.return_value = []
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
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
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    async def fake_run_pfc_once(**kwargs):
        assert not lock.locked()
        return SimpleNamespace(reply="planner-ok", action="reply", reason="ok", ended=False)

    async def fake_record_bot_reply(*args, **kwargs):
        assert lock.locked()
        return []

    def fake_set_state(chat_id, snapshot):
        assert lock.locked()

    def fake_schedule_pfc_state_flush(context, runtime, *, chat_id):
        assert not lock.locked()

    state.pfc_state_store.set_state = Mock(side_effect=fake_set_state)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply",
            new=AsyncMock(side_effect=fake_record_bot_reply),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
            create=True,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()


@pytest.mark.asyncio
async def test_smalltalk_forced_reply_generation_runs_outside_chat_lock_but_commits_inside(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.pfc_state_store.get_async = AsyncMock()
    state.pfc_state_store.set_state_async = AsyncMock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event["_xc_command_forced"] = True

    async def fake_generate_reply(**kwargs):
        assert not lock.locked()
        return "forced-ok"

    async def fake_record_bot_reply(*args, **kwargs):
        assert lock.locked()
        return []

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply",
            new=AsyncMock(side_effect=fake_generate_reply),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply",
            new=AsyncMock(side_effect=fake_record_bot_reply),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "forced-ok"}}]


@pytest.mark.asyncio
async def test_smalltalk_stale_generation_is_dropped_before_commit(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
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
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    reply="planner-ok", action="reply", reason="ok", ended=False
                )
            ),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u2"),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply", new=AsyncMock()
        ) as mock_record_bot_reply,
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            create=True,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == []
    mock_record_bot_reply.assert_not_awaited()
    state.heartflow.on_no_reply_async.assert_not_awaited()
    state.action_history.append.assert_not_called()
    state.pfc_state_store.set_state.assert_not_called()
    mock_schedule_pfc_state_flush.assert_not_called()


@pytest.mark.asyncio
async def test_smalltalk_no_reply_current_turn_schedules_pfc_state_flush(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
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
            personality=SimpleNamespace(states=[], state_probability=0.0),
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
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(
                return_value=SimpleNamespace(reply="", action="wait", reason="later", ended=False)
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
            create=True,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == []
    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()
    state.heartflow.on_no_reply_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_reply_record_fails(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent.return_value = []
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
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
            personality=SimpleNamespace(states=[], state_probability=0.0),
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
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    reply="planner-ok", action="reply", reason="ok", ended=False
                )
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_no_reply_hook_fails(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock(side_effect=RuntimeError("no-reply boom"))
    state.inc_stats = Mock()
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
            personality=SimpleNamespace(states=[], state_probability=0.0),
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
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(
                return_value=SimpleNamespace(reply="", action="wait", reason="later", ended=False)
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        with pytest.raises(RuntimeError, match="no-reply boom"):
            await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()
    state.action_history.append.assert_not_called()


@pytest.mark.asyncio
async def test_mode_indicator_only_applies_in_brain_chat(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                show_mode_indicator=True,
                brain_mode_indicator="[brain]",
            ),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event["_xc_command_forced"] = True

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="ok")),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "ok"}}]
    mock_context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_indicator_is_emitted_when_brain_chat_is_active(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=True,
                show_mode_indicator=True,
                brain_mode_indicator="[brain]",
            ),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event["_xc_command_forced"] = True

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=True),
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="ok")),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "[brain]\nok"}}]
    mock_context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_provider_denies_switch_for_non_admin(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_provider

    state = SimpleNamespace(active_provider=None)
    mock_context.is_admin = Mock(return_value=False)
    mock_context.check_permission = Mock(return_value=False)
    mock_context.admin_ids = []
    mock_context.secrets = {
        "admin_user_ids": [],
        "plugins": {
            "xiaoqing_chat": {
                "default": "deepseek",
                "providers": {
                    "deepseek": {"model": "deepseek-chat", "api_base": "http://a"},
                    "glm": {"model": "glm-4", "api_base": "http://b"},
                },
            }
        },
    }

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("glm", sample_group_event, mock_context)

    assert state.active_provider is None
    assert "管理员" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_list_mode_remains_public(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_provider

    state = SimpleNamespace(active_provider=None)
    mock_context.is_admin = Mock(return_value=False)
    mock_context.check_permission = Mock(return_value=False)
    mock_context.admin_ids = []
    mock_context.secrets = {
        "admin_user_ids": [],
        "plugins": {
            "xiaoqing_chat": {
                "default": "deepseek",
                "providers": {
                    "deepseek": {"model": "deepseek-chat", "api_base": "http://a"},
                    "glm": {"model": "glm-4", "api_base": "http://b"},
                },
            }
        },
    }

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("", sample_group_event, mock_context)

    assert state.active_provider is None
    assert "LLM 供应商" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_allows_switch_for_admin(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_provider

    state = SimpleNamespace(active_provider=None)
    mock_context.is_admin = Mock(return_value=True)
    mock_context.check_permission = Mock(return_value=False)
    mock_context.admin_ids = []
    mock_context.secrets = {
        "admin_user_ids": [],
        "plugins": {
            "xiaoqing_chat": {
                "default": "deepseek",
                "providers": {
                    "deepseek": {"model": "deepseek-chat", "api_base": "http://a"},
                    "glm": {"model": "glm-4", "api_base": "http://b"},
                },
            }
        },
    }

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("glm", sample_group_event, mock_context)

    assert state.active_provider == "glm"
    assert "已切换到" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_allows_switch_via_admin_ids_fallback(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_provider

    state = SimpleNamespace(active_provider=None)
    mock_context.is_admin = Mock(return_value=False)
    mock_context.check_permission = Mock(return_value=False)
    mock_context.admin_ids = [sample_group_event["user_id"]]
    mock_context.secrets = {
        "admin_user_ids": [],
        "plugins": {
            "xiaoqing_chat": {
                "default": "deepseek",
                "providers": {
                    "deepseek": {"model": "deepseek-chat", "api_base": "http://a"},
                    "glm": {"model": "glm-4", "api_base": "http://b"},
                },
            }
        },
    }

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("glm", sample_group_event, mock_context)

    assert state.active_provider == "glm"
    assert "已切换到" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_uses_passed_bound_state(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded

    state = MagicMock()
    state.review_store.cleanup_expired = Mock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.set_last_observe_ts = Mock()

    runtime = MagicMock()

    with (
        patch(
            "plugins.xiaoqing_chat.handlers._get_bound_state",
            side_effect=AssertionError("bound state should not be reloaded"),
        ),
        patch("plugins.xiaoqing_chat.handlers._next_local_id", return_value="m1"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        local_id = await _ensure_user_message_recorded(
            "你好",
            sample_group_event,
            mock_context,
            runtime,
            state=state,
        )

    assert local_id == "m1"
    called_chat_id, called_ts = state.set_last_observe_ts.call_args.args
    assert called_chat_id == "g67890"
    assert isinstance(called_ts, float)


def test_next_local_id_atomic():
    """fetch_and_increment_local_id should be atomic read-and-bump."""
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    result = state.fetch_and_increment_local_id("test_chat")
    assert result == 1
    result2 = state.fetch_and_increment_local_id("test_chat")
    assert result2 == 2


@pytest.mark.asyncio
async def test_smalltalk_new_user_turn_clears_sticky_pfc_ended_before_planner_runs(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=True,
            last_successful_reply_action="say_goodbye",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

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
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    captured: dict[str, Any] = {}

    async def fake_run_pfc_once(**kwargs):
        captured["state_override"] = kwargs["state_override"]
        return SimpleNamespace(reply="", action="wait", reason="再观察", ended=False)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("新消息", sample_group_event, mock_context)

    assert result == []
    assert captured["state_override"].ended is False


@pytest.mark.asyncio
async def test_smalltalk_commit_syncs_goal_store_to_top_planner_goal_after_state_override_update(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.goal_store.set_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[{"goal": "旧规划目标"}],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=10,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    async def fake_run_pfc_once(**kwargs):
        kwargs["state_override"].goal_list = [{"goal": "新规划目标"}]
        return SimpleNamespace(reply="planner-ok", action="reply", reason="ok", ended=False)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="旧门控目标"),
        ),
        patch("plugins.xiaoqing_chat.handlers._record_bot_reply", new=AsyncMock(return_value=[])),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续聊", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    assert state.goal_store.set_async.await_args_list[-1].kwargs == {
        "goal": "新规划目标",
        "source": "planner",
    }


@pytest.mark.asyncio
async def test_smalltalk_commit_clears_goal_store_when_planner_goal_list_becomes_empty(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk
    from plugins.xiaoqing_chat.planning.goal_state import GoalStore

    chat_id = "g67890"
    lock = asyncio.Lock()
    goal_store = GoalStore()
    await goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.goal_store = goal_store
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id=chat_id,
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[{"goal": "旧规划目标"}],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=10,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    async def fake_run_pfc_once(**kwargs):
        kwargs["state_override"].goal_list = []
        return SimpleNamespace(reply="planner-ok", action="reply", reason="ok", ended=False)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="围绕旧目标继续"),
        ),
        patch("plugins.xiaoqing_chat.handlers._record_bot_reply", new=AsyncMock(return_value=[])),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续聊", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    assert state.goal_store.get(chat_id).goal == ""


@pytest.mark.asyncio
async def test_smalltalk_pre_gate_clears_goal_store_when_no_goal_is_derived(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk
    from plugins.xiaoqing_chat.planning.goal_state import GoalStore

    chat_id = "g67890"
    lock = asyncio.Lock()
    goal_store = GoalStore()
    await goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.goal_store = goal_store
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id=chat_id,
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.heartflow.on_no_reply_async = AsyncMock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            personality=SimpleNamespace(states=[], state_probability=0.0),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.derive_goal_async", new=AsyncMock(return_value="")),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续聊", sample_group_event, mock_context)

    assert result == []
    assert state.goal_store.get(chat_id).goal == ""
    state.heartflow.on_no_reply_async.assert_awaited_once_with(chat_id=chat_id)


@pytest.mark.asyncio
async def test_handle_errors_uses_keyword_context_for_logging():
    context = SimpleNamespace(logger=MagicMock())

    @handle_errors("测试")
    async def failing_handler(*, context=None):
        raise RuntimeError("boom")

    result = await failing_handler(context=context)

    context.logger.exception.assert_called_once()
    assert result[0]["data"]["text"] == "❌ 测试出错: boom"


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_goal_heartflow_and_action_history(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.handlers import handle_internal
    from plugins.xiaoqing_chat.planning.action_history import ActionRecord
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
    from plugins.xiaoqing_chat.store_binding import _bind_all_stores

    chat_id = "g67890"
    runtime = MagicMock()
    state = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    state.memory_store.append(chat_id, role="user", name="Tester", content="hi")
    await state.goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")
    await state.heartflow.on_user_message_async(chat_id=chat_id)
    await state.heartflow.on_bot_reply_async(chat_id=chat_id)
    state.action_history.append(
        chat_id,
        ActionRecord(
            ts=time.time(),
            local_target="u1",
            action="reply",
            reasoning="old",
            detail={"source": "pfc"},
            executed=True,
        ),
    )
    state.set_continuous_reply_count(chat_id, 3)
    state.set_continuous_cooldown_until(chat_id, 600.0)

    pfc_st = await state.pfc_state_store.get_async(chat_id)
    pfc_st.ended = True
    pfc_st.ignore_until_ts = 123.0
    pfc_st.last_successful_reply_action = "say_goodbye"
    pfc_st.goal_list = [{"goal": "old"}]
    pfc_st.knowledge_list = [{"text": "old"}]
    await state.pfc_state_store.save_async(chat_id)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, data_dir=tmp_path)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.memory_store.get(chat_id) == []
    assert state.goal_store.get(chat_id).goal == ""
    heartflow_state = await state.heartflow.get_async(chat_id)
    assert heartflow_state.reply_streak == 0
    assert heartflow_state.no_reply_streak == 0
    assert state.get_continuous_reply_count(chat_id) == 0
    assert state.get_continuous_cooldown_until(chat_id) == 0.0
    assert state.action_history.get_recent(chat_id, max_items=20) == []


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_review_policy_and_sessions_for_current_chat(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.handlers import handle_internal
    from plugins.xiaoqing_chat.memory.review_sessions import ReviewPolicy
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
    from plugins.xiaoqing_chat.store_binding import _bind_all_stores

    chat_id = "g67890"
    runtime = MagicMock()
    state = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    state.review_store.save_policy(
        chat_id,
        ReviewPolicy(
            goal_override="旧目标覆写",
            strategy_note="旧策略备注",
            avoid_patterns=["避免句式A", "避免句式B"],
        ),
    )
    session = state.review_store.open_session_if_allowed(
        kind="goal_strategy",
        chat_id=chat_id,
        payload={"goal": "旧目标", "stats": "旧统计"},
        timeout_seconds=600.0,
        cooldown_seconds=0.0,
    )
    assert session is not None

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, data_dir=tmp_path)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]

    policy_after_reset = state.review_store.get_policy(chat_id)
    assert policy_after_reset.goal_override == ""
    assert policy_after_reset.strategy_note == ""
    assert policy_after_reset.avoid_patterns == []

    sessions_after_reset = [x for x in state.review_store.list_sessions() if x.chat_id == chat_id]
    assert sessions_after_reset == []


@pytest.mark.parametrize("planner_action", ["send_new_message", "say_goodbye"])
@pytest.mark.asyncio
async def test_smalltalk_action_history_preserves_real_planner_action_name(
    mock_context, sample_group_event, planner_action
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

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
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    reply="补一句", action=planner_action, reason="planner", ended=False
                )
            ),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("继续", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "补一句"}}]
    record = state.action_history.append.call_args.args[1]
    assert record.action == planner_action


@pytest.mark.asyncio
async def test_run_pfc_once_rethink_goal_passes_replanned_goal_focus_to_reply_generator(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id = "pfc-goal-replan"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="今天去哪家火锅店")
    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()

    generate_reply = AsyncMock(return_value="ok")

    with (
        patch(
            "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
            new=AsyncMock(
                side_effect=[
                    PFCPlan(
                        action="rethink_goal",
                        reason="当前目标过时，需要重设",
                        thinking="先确认预算和口味偏好",
                        wait_seconds=0,
                    ),
                    PFCPlan(
                        action="direct_reply",
                        reason="继续回复",
                        thinking="",
                        wait_seconds=0,
                    ),
                ]
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.planning.pfc_engine.analyze_goals",
            new=AsyncMock(
                return_value=[
                    {
                        "goal": "帮用户选一家合适的火锅店",
                        "focus": "预算与口味偏好",
                    }
                ]
            ),
        ),
    ):
        result = await run_pfc_once(
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="今天去哪家火锅店",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert result.reply == "ok"
    assert generate_reply.await_count == 1
    assert generate_reply.await_args is not None
    _, planner_reason, extra_reason = generate_reply.await_args.args
    merged_reason = (f"{planner_reason}\n{extra_reason}").strip()
    assert "帮用户选一家合适的火锅店" in merged_reason
    assert "预算与口味偏好" in merged_reason


@pytest.mark.asyncio
async def test_generate_reply_prefers_planner_goal_over_review_override_goal_store(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(multiple_reply_style=[], multiple_probability=0.0),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(enable_review_sessions=True),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=3.0,
            reply_check=SimpleNamespace(
                enable_reply_checker=True,
                enable_llm_checker=False,
                max_repeat_compare=5,
                similarity_threshold=0.9,
                max_assistant_in_row=3,
                max_regen=0,
            ),
            postprocess=SimpleNamespace(),
            rewrite=SimpleNamespace(),
        )
    )

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="复盘会话先问感受"))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
        },
    )

    action = PlannedAction(
        action="reply",
        target_message_id="u1",
        think_level=1,
        quote=False,
        reasoning="目标: 帮用户选一家合适的火锅店",
        question="",
        unknown_words=[],
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._get_bot_name", return_value="小青")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._chat_id", return_value="g1")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._is_private", return_value=False)
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
                return_value={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._resolve_llm_config", return_value=fg)
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_profile_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_knowledge_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_expression_block", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_jargon_explanation", return_value=""
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_memory_block",
                new=AsyncMock(return_value=""),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_policy_block",
                return_value="review-policy",
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_tool_info_block",
                new=AsyncMock(return_value=""),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_max_context", return_value=10
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_temperature", return_value=0.7
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.get_brain_chat_identity", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_reply_style", return_value=""
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_prompt_messages",
                return_value=[
                    ChatMessage(role="system", content="s"),
                    ChatMessage(role="user", content="u"),
                ],
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.chat_completions_with_fallback_paths",
                new=AsyncMock(return_value=("好的", "/v1/chat/completions")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.maybe_rewrite_reply",
                new=AsyncMock(return_value="好的"),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response", return_value=["好的"]
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="好的")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_dialogue_prompt",
                return_value="dialogue",
            )
        )
        mock_check_reply = stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.check_reply",
                new=AsyncMock(
                    return_value=ReplyCheckResult(suitable=True, reason="", need_replan=False)
                ),
            )
        )

        result = await _generate_reply(
            text="那你推荐哪家？",
            event={},
            context=mock_context,
            runtime=cast(Any, runtime),
            state=state,
            forced=False,
            action=action,
            plan_reasoning="目标: 帮用户选一家合适的火锅店",
            is_brain_chat=False,
        )

    assert result == "好的"
    assert mock_check_reply.await_count == 1
    assert mock_check_reply.await_args is not None
    assert "帮用户选一家合适的火锅店" in mock_check_reply.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_generate_reply_prefers_plan_reasoning_goal_when_action_reasoning_has_no_goal(
    mock_context,
):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(multiple_reply_style=[], multiple_probability=0.0),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(enable_review_sessions=False),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=3.0,
            reply_check=SimpleNamespace(
                enable_reply_checker=True,
                enable_llm_checker=False,
                max_repeat_compare=5,
                similarity_threshold=0.9,
                max_assistant_in_row=3,
                max_regen=0,
            ),
            postprocess=SimpleNamespace(),
            rewrite=SimpleNamespace(),
        )
    )

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="旧目标"))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
        },
    )

    action = PlannedAction(
        action="reply",
        target_message_id="u1",
        think_level=1,
        quote=False,
        reasoning="继续回复",
        question="",
        unknown_words=[],
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._get_bot_name", return_value="小青")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._chat_id", return_value="g1")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._is_private", return_value=False)
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
                return_value={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._resolve_llm_config", return_value=fg)
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_profile_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_knowledge_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_expression_block", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_jargon_explanation", return_value=""
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_memory_block",
                new=AsyncMock(return_value=""),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_tool_info_block",
                new=AsyncMock(return_value=""),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_max_context", return_value=10
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_temperature", return_value=0.7
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.get_brain_chat_identity", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_reply_style", return_value=""
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_prompt_messages",
                return_value=[
                    ChatMessage(role="system", content="s"),
                    ChatMessage(role="user", content="u"),
                ],
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.chat_completions_with_fallback_paths",
                new=AsyncMock(return_value=("好的", "/v1/chat/completions")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.maybe_rewrite_reply",
                new=AsyncMock(return_value="好的"),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response", return_value=["好的"]
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="好的")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_dialogue_prompt",
                return_value="dialogue",
            )
        )
        mock_check_reply = stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.check_reply",
                new=AsyncMock(
                    return_value=ReplyCheckResult(suitable=True, reason="", need_replan=False)
                ),
            )
        )

        result = await _generate_reply(
            text="那你推荐哪家？",
            event={},
            context=mock_context,
            runtime=cast(Any, runtime),
            state=state,
            forced=False,
            action=action,
            plan_reasoning="继续回复\n目标: 帮用户选一家合适的火锅店\n焦点: 预算和口味偏好",
            is_brain_chat=False,
        )

    assert result == "好的"
    assert mock_check_reply.await_args is not None
    assert "帮用户选一家合适的火锅店" in mock_check_reply.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_smalltalk_no_reply_preserves_wait_action_name_in_action_history(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

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
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(
                return_value=SimpleNamespace(reply="", action="wait", reason="再观察", ended=False)
            ),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续看看", sample_group_event, mock_context)

    assert result == []
    record = state.action_history.append.call_args.args[1]
    assert record.action == "wait"


def test_load_latest_topic_and_summary_skips_invalid_tail_entries(tmp_path):
    from plugins.xiaoqing_chat.planning.goal_state import load_latest_topic_and_summary

    data_dir = tmp_path
    chat_id = "goal-parser-dedup"
    summary_path = data_dir / "hippo_memorizer" / f"{chat_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        (
            "["
            '{"topic":"火锅选择","summary":"先看预算和口味","updated_at":1},'
            '{"topic":"","summary":""}'
            "]"
        ),
        encoding="utf-8",
    )

    topic, summary = load_latest_topic_and_summary(data_dir, chat_id)

    assert topic == "火锅选择"
    assert summary == "先看预算和口味"
