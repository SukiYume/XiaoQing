"""命令、帮助和运行态边界。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, Mock

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.planning.action_history import ActionRecord
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState, get_state
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events, public_error_reply
from tests.helpers.xiaoqing_chat_test_support import (
    ScriptedChatModel,
    ack_reply_delivery,
    build_xiaoqing_catalog,
    chat_message_event,
    scripted_chat_context,
    settle_chat_background,
    xiaoqing_chat,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_init(mock_context, caplog):
    """Test plugin initialization"""
    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    # Should not raise
    await xiaoqing_chat.init(mock_context)
    assert logged_events(caplog, "plugin.init.done")


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_help_command(mock_context, sample_group_event):
    """Test handle with help subcommand"""
    result = await xiaoqing_chat.handle(
        command = "xc",
        args    = "help",
        event   = sample_group_event,
        context = mock_context,
    )

    assert len(result) > 0
    assert "智能对话" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_help_with_extra_arg_does_not_fall_through_to_chat(
    mock_context, sample_group_event
):
    model = ScriptedChatModel(reply="不该有这句")
    scripted_chat_context(mock_context, model)

    result = await xiaoqing_chat.handle(
        command = "xc",
        args    = "help extra",
        event   = chat_message_event(sample_group_event, "/xc help extra"),
        context = mock_context,
    )

    assert "不接受额外参数" in result[0]["data"]["text"]
    assert "/xc help" in result[0]["data"]["text"]
    assert model.calls == []
    await settle_chat_background(mock_context, model)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_unknown_command(mock_context, sample_group_event):
    """Test handle with unknown subcommand"""
    from plugins.xiaoqing_chat.main import handle

    model = ScriptedChatModel(reply="mock_response")
    scripted_chat_context(mock_context, model)

    result = await handle(
        command = "xc",
        args    = "unknown_subcommand",
        event   = chat_message_event(sample_group_event, "/xc unknown_subcommand"),
        context = mock_context,
    )

    # 不认识的子命令当作聊天内容：模型被请求回复一次，回复送回给用户。
    assert len(model.calls_of("reply")) == 1
    assert "unknown_subcommand" in model.calls_of("reply")[0].text
    assert [seg["data"]["text"] for seg in result] == ["mock_response"]
    await settle_chat_background(mock_context, model)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_empty_args(mock_context, sample_group_event):
    """Test handle with empty args"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command = "xc",
        args    = "",
        event   = sample_group_event,
        context = mock_context,
    )

    assert len(result) > 0
    text = result[0]["data"]["text"]
    assert "智能对话" in text or "可用命令" in text


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_exception(mock_context, sample_group_event):
    """Test handle handles exceptions gracefully"""
    from plugins.xiaoqing_chat.main import handle

    # Core 的命令目录查询失败。
    mock_context.get_command_catalog = Mock(side_effect=Exception("Test error"))
    result = await handle(
        command = "xc",
        args    = "test",
        event   = sample_group_event,
        context = mock_context,
    )

    assert result.command_outcome.state == "error"
    assert public_error_reply(result[0]["data"]["text"])["type_name"] == "Exception"
    assert "Test error" not in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_config_command(mock_context, sample_group_event):
    """Test handle with /xc 配置 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command = "xc",
        args    = "配置",
        event   = sample_group_event,
        context = mock_context,
    )

    assert len(result) > 0
    assert "配置" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_memory_command(mock_context, sample_group_event):
    """Test handle with /xc 记忆 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command = "xc",
        args    = "记忆",
        event   = sample_group_event,
        context = mock_context,
    )

    assert len(result) > 0
    assert "记忆" in result[0]["data"]["text"]
    assert "/xc 记忆 <关键词>" in result[0]["data"]["text"]
    assert "/xc记忆" not in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_expression_command(mock_context, sample_group_event):
    """Test handle with /xc 表达 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command = "xc",
        args    = "表达",
        event   = sample_group_event,
        context = mock_context,
    )

    assert len(result) > 0
    assert "表达" in result[0]["data"]["text"]


@pytest.mark.plugin
def test_xiaoqing_chat_show_help():
    """帮助直接由 Core 命令目录生成。"""
    from plugins.xiaoqing_chat.main import _help_text

    root      = build_xiaoqing_catalog()
    help_text = _help_text(
        SimpleNamespace(
            command_invocation  = None,
            get_command_catalog = lambda: (root,),
            get_settings_snapshot=lambda: SimpleNamespace(config={"bot_name": "小青"}),
        )
    )

    assert "智能对话" in help_text
    assert "/xc" in help_text
    assert "清空" in help_text
    assert "统计" in help_text
    assert "深度" in help_text


@pytest.mark.plugin
def test_xiaoqing_chat_show_help_contains_all_sections():
    """帮助包含全部结构化子命令码。"""
    from plugins.xiaoqing_chat.main import _help_text

    root      = build_xiaoqing_catalog()
    help_text = _help_text(
        SimpleNamespace(
            command_invocation  = None,
            get_command_catalog = lambda: (root,),
            get_settings_snapshot=lambda: SimpleNamespace(config={"bot_name": "小青"}),
        )
    )

    assert all(node.code in help_text for node in root.walk())


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_shutdown(mock_context, caplog):
    """Test plugin shutdown"""
    from plugins.xiaoqing_chat.main import shutdown

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    # Should not raise
    await shutdown(mock_context)
    assert logged_events(caplog, "plugin.shutdown.done")


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_shutdown_flushes_action_history_and_media_store(mock_context):
    from plugins.xiaoqing_chat.main import shutdown

    state = bound_state(mock_context)
    state.action_history.append(
        "g1",
        ActionRecord(
            ts=1.0, local_target="", action="reply", reasoning="", detail={}, executed=True
        ),
    )
    state.media_store.upsert_media_items(
        [{"kind": "image", "media_hash": "shutdown-flush", "marker": "[图片]"}]
    )
    history_file = mock_context.data_dir / "action_history" / "g1.json"
    media_file   = mock_context.data_dir / "media" / "index.json"
    assert not history_file.exists() and not media_file.exists()

    await shutdown(mock_context)

    assert history_file.exists()
    assert "shutdown-flush" in media_file.read_text(encoding="utf-8")


@pytest.mark.plugin
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alias", "reply_fragment"),
    [
        pytest.param("reset", "重置 确认", id="reset"),
        pytest.param("重置", "重置 确认", id="reset-zh"),
        pytest.param("清空", "重置 确认", id="reset-clear"),
        pytest.param("stats", "会话统计", id="stats"),
        pytest.param("统计", "会话统计", id="stats-zh"),
        pytest.param("状态", "会话统计", id="stats-status"),
        pytest.param("brain", "深度对话模式", id="brain"),
        pytest.param("深度", "深度对话模式", id="brain-zh"),
    ],
)
async def test_handle_routes_every_subcommand_alias_to_internal(
    mock_context, sample_group_event, alias, reply_fragment
):
    """/xc 的每个内部子命令别名都进入同一个内部命令：群里的重置先要求确认，统计和深度对话各出各的页面。"""
    from plugins.xiaoqing_chat.main import handle

    model = ScriptedChatModel()
    scripted_chat_context(mock_context, model)

    result = await handle(
        command = "xc",
        args    = alias,
        event   = chat_message_event(sample_group_event, f"/xc {alias}"),
        context = mock_context,
    )

    assert reply_fragment in result[0]["data"]["text"]
    assert model.calls == []
    await settle_chat_background(mock_context, model)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_smalltalk_called_with_correct_params(mock_context, sample_group_event):
    """/xc 后面的内容原样当作聊天消息（不带 /xc 前缀）进入聊天记录和模型请求，本轮算强制回复。"""
    from plugins.xiaoqing_chat.main import handle

    model = ScriptedChatModel(reply="收到")
    scripted_chat_context(mock_context, model)
    event = chat_message_event(sample_group_event, "/xc 测试消息")

    result = await handle(command="xc", args="测试消息", event=event, context=mock_context)
    await ack_reply_delivery(result)

    assert [seg["data"]["text"] for seg in result] == ["收到"]
    assert event["_xc_command_forced"] is True
    history = bound_state(mock_context).memory_store.get("g67890")
    assert [(m.role, m.content) for m in history] == [("user", "测试消息"), ("assistant", "收到")]
    assert "/xc" not in model.calls_of("reply")[0].text
    await settle_chat_background(mock_context, model)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_no_raw_message(mock_context, sample_group_event):
    """Test handle with event missing raw_message routes to smalltalk"""
    from plugins.xiaoqing_chat.main import handle

    model = ScriptedChatModel(reply="收到")
    scripted_chat_context(mock_context, model)
    event = chat_message_event(sample_group_event, "/xc test")
    del event["raw_message"]

    result = await handle(
        command = "xc",
        args    = "test",
        event   = event,
        context = mock_context,
    )

    # Should route to handle_smalltalk with forced flag
    assert [seg["data"]["text"] for seg in result] == ["收到"]
    assert len(model.calls_of("reply")) == 1
    assert event.get("_xc_command_forced") is True
    await settle_chat_background(mock_context, model)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_whitespace_args(mock_context, sample_group_event):
    """Test handle with whitespace-only args"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command = "xc",
        args    = "   ",
        event   = sample_group_event,
        context = mock_context,
    )

    # Should handle gracefully and return help
    assert len(result) > 0


@pytest.mark.asyncio
async def test_runtime_cleanup_eviction_also_cleans_locks_and_persist_tasks():
    capacity = ChatRuntimeState._MAX_TRACKED_CHATS
    state    = ChatRuntimeState()
    loop     = asyncio.get_running_loop()
    for i in range(capacity + 100):
        cid = f"g{i}"
        state.get_lock(cid)
        state.set_last_reply_ts(cid, float(i))
        done_task = loop.create_future()
        done_task.set_result(None)
        state.persist_tasks[cid] = cast(Any, done_task)

    state.cleanup_stale_chats()

    assert len(state._per_chat.locks) <= capacity
    assert len(state._per_chat.persist_tasks) <= capacity


@pytest.mark.asyncio
async def test_runtime_cleanup_stale_prefers_recent_observe_activity():
    capacity = ChatRuntimeState._MAX_TRACKED_CHATS
    state    = ChatRuntimeState()
    state.get_lock("g_observe")
    state.get_lock("g_reply")
    state.set_last_reply_ts("g_reply", 100.0)
    state.set_last_observe_ts("g_reply", 100.0)
    state.set_last_observe_ts("g_observe", 200.0)
    # 其余会话都比这两个更近活跃：超出上限的恰好一个，淘汰最久没有动静的那个。
    for i in range(capacity - 1):
        state.set_last_observe_ts(f"busy{i}", 1000.0 + i)

    state.cleanup_stale_chats()

    assert "g_observe" in state._per_chat.locks
    assert "g_reply" not in state._per_chat.locks


@pytest.mark.asyncio
async def test_action_history_flush_task_entry_removed_after_task_done(tmp_path):
    from plugins.xiaoqing_chat.task_scheduler import (
        _action_flush_tasks,
        schedule_action_history_flush,
    )

    context = MagicMock()
    runtime = cast(Any, SimpleNamespace(cfg=SimpleNamespace(io_persist_debounce_seconds=0.0)))
    state = get_state()
    state.action_history.bind(tmp_path)
    state.action_history.append(
        "g1",
        ActionRecord(
            ts=1.0, local_target="", action="reply", reasoning="", detail={}, executed=True
        ),
    )

    _action_flush_tasks.clear()
    try:
        schedule_action_history_flush(context, runtime, chat_id="g1")
        task = _action_flush_tasks["g1"]
        await asyncio.wait_for(task, timeout=5)

        assert "g1" not in _action_flush_tasks
        assert (tmp_path / "action_history" / "g1.json").exists()
    finally:
        _action_flush_tasks.clear()
