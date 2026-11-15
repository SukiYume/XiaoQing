"""聊天生成、记忆组装、模型网关与公开错误边界。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig


def test_is_brain_chat_active_keeps_private_brain_mode_when_forced() -> None:
    from plugins.xiaoqing_chat.brain_chat import is_brain_chat_active
    from plugins.xiaoqing_chat.config.config import BrainChatConfig, XiaoQingChatConfig
    from plugins.xiaoqing_chat.runtime_state import ChatRuntime

    runtime = ChatRuntime(
        cfg=XiaoQingChatConfig(
            brain_chat=BrainChatConfig(enable_private_brain_chat=True),
        ),
        compiled_ban_regex=[],
    )

    assert is_brain_chat_active(runtime, is_private=True) is True


def _failing_smalltalk(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(
        "plugins.xiaoqing_chat.handlers._maybe_reply_smalltalk",
        AsyncMock(side_effect=RuntimeError("internal /tmp/secrets/token.txt leak")),
    )
    mp.setattr("plugins.xiaoqing_chat.handlers._last_stalled_notice", {})


@pytest.mark.asyncio
async def test_a_failed_private_turn_gets_one_in_character_line_and_no_error_code() -> None:
    from plugins.xiaoqing_chat.handlers import _STALLED_REPLIES, handle_smalltalk

    context        = MagicMock()
    context.logger = MagicMock()
    event          = {"message_type": "private", "user_id": 123, "message_id": 456}

    with pytest.MonkeyPatch.context() as mp:
        _failing_smalltalk(mp)
        first  = await handle_smalltalk("你好", event, context)
        second = await handle_smalltalk("在吗", {**event, "message_id": 457}, context)

    assert [segment["data"]["text"] for segment in first] == [first[0]["data"]["text"]]
    assert first[0]["data"]["text"] in _STALLED_REPLIES
    # 故障还在持续：同一会话不再重复说，也不出现错误码和异常内容。
    assert second == []
    assert "XQ-PLUGIN" not in str(first) and "internal" not in str(first)
    # 诊断信息照常记进日志，供排查。
    assert context.logger.error.called


@pytest.mark.asyncio
async def test_a_failed_group_interjection_is_silent() -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    context        = MagicMock()
    context.logger = MagicMock()
    event          = {"message_type": "group", "group_id": 9, "user_id": 123, "message_id": 456}

    with pytest.MonkeyPatch.context() as mp:
        _failing_smalltalk(mp)
        assert await handle_smalltalk("今天好冷", event, context) == []
        # 被点名时才说一句“卡了”。
        addressed = await handle_smalltalk("小青在吗", {**event, "_xc_forced": True}, context)
    assert len(addressed) == 1 and "XQ-PLUGIN" not in str(addressed)
    assert context.logger.error.called


def test_llm_call_config_contains_only_runtime_controls() -> None:

    config = LLMCallConfig(timeout_seconds=3.0, max_retry=0, retry_interval_seconds=0.2)

    assert config.request_controls() == {
        "timeout_seconds"       : 3.0,
        "total_timeout_seconds" : 60.0,
        "max_retry"             : 0,
        "retry_interval_seconds": 0.2,
    }


def test_memory_db_save_logs_write_failures(tmp_path) -> None:
    from plugins.xiaoqing_chat.memory import memory_db as memory_db_module
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    logger = MagicMock()

    def fail_write_vector_store_files(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(memory_db_module, "_logger", logger, raising=False)
        mp.setattr(
            memory_db_module,
            "write_vector_store_files",
            fail_write_vector_store_files,
            raising=True,
        )
        db.save()

    assert db.is_dirty() is True
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_xiaoqing_gateway_delegates_retry_policy_to_core() -> None:
    from core.ai import AICompletionResult
    from plugins.xiaoqing_chat.llm.gateway import complete_raw

    service = SimpleNamespace(
        complete=AsyncMock(
            return_value=AICompletionResult(
                response      = {"choices": [{"message": {"content": "ok"}}]},
                profile       = "primary",
                provider      = "provider",
                model         = "model",
                finish_reason = "stop",
                attempts      = 1,
            )
        )
    )
    await complete_raw(
        secrets                = {"_ai": service, "_route": "chat"},
        messages               = [{"role": "user", "content": "hi"}],
        temperature            = 0.7,
        top_p                  = 0.9,
        max_tokens             = 128,
        timeout_seconds        = 3.0,
        max_retry              = 2,
        retry_interval_seconds = 0.5,
    )

    kwargs = service.complete.await_args.kwargs
    assert kwargs["max_retry"] == 2
    assert kwargs["retry_interval_seconds"] == 0.5
    assert kwargs["timeout_seconds"] == 3.0


@pytest.mark.asyncio
async def test_gateway_forwards_extra_payload_without_streaming() -> None:
    from core.ai import AICompletionResult
    from plugins.xiaoqing_chat.llm.gateway import complete_raw

    service = SimpleNamespace(
        complete=AsyncMock(
            return_value=AICompletionResult(
                response      = {"choices": [{"message": {"content": "ok"}}]},
                profile       = "primary",
                provider      = "provider",
                model         = "model",
                finish_reason = "stop",
                attempts      = 1,
            )
        )
    )
    messages = [{"role": "user", "content": "hi"}]

    await complete_raw(
        secrets                = {"_ai": service, "_route": "chat"},
        messages               = messages,
        temperature            = 0.7,
        top_p                  = 0.9,
        max_tokens             = 128,
        timeout_seconds        = 3.0,
        max_retry              = 0,
        retry_interval_seconds = 0.5,
        extra_payload          = {
            "thinking"        : {"type": "enabled"},
            "reasoning_effort": "high",
        },
    )

    kwargs = service.complete.await_args.kwargs
    assert kwargs["extra_payload"] == {
        "thinking"        : {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert service.complete.await_args.args[1] == messages


@pytest.mark.asyncio
async def test_xiaoqing_uses_safe_core_model_metadata(tmp_path) -> None:
    from core.ai import AIModelInfo
    from core.interfaces import PluginCapabilities
    from plugins.xiaoqing_chat.chat_runtime import ai_route_context
    from plugins.xiaoqing_chat.config.config import load_xiaoqing_chat_config
    from plugins.xiaoqing_chat.runtime_state import get_state

    state      = get_state()
    old_active = state.global_active_provider
    state.set_global_provider(None)
    try:
        config = {
            "plugins": {
                "xiaoqing_chat": {
                    "ai": {
                        "routes": {"chat": {"models": ["deepseek-flash"], "timeout_seconds": 23}},
                        "model_aliases": {"deepseek": "deepseek-flash"},
                    },
                }
            }
        }
        service = SimpleNamespace(
            list_models=lambda route, **kwargs: (
                AIModelInfo(
                    "deepseek-flash",
                    "deepseek",
                    "deepseek-v4-flash",
                    ("text",),
                ),
            )
        )
        from tests.helpers.settings_snapshot import with_settings_reader

        context = with_settings_reader(
            SimpleNamespace(
                config  = config,
                secrets = {"plugins": {"xiaoqing_chat": {"api_key": "<LEGACY_API_KEY>"}}},
                capabilities=PluginCapabilities(ai=service),
            )
        )

        secrets = ai_route_context(context)
        loaded  = load_xiaoqing_chat_config(
            context_config = config,
            plugin_dir     = tmp_path,
        )
    finally:
        state.set_global_provider(old_active)

    assert secrets["model"] == "deepseek-v4-flash"
    assert secrets["_profile"] == "deepseek-flash"
    assert secrets["_pinned_model"] is None
    assert "api_key" not in secrets and "api_base" not in secrets
    assert loaded.llm_config("chat").timeout_seconds == 23


@pytest.mark.asyncio
async def test_explicit_reasoning_route_ignores_chat_pin():
    from plugins.xiaoqing_chat.llm.gateway import complete_raw

    service = SimpleNamespace(complete=AsyncMock())
    await complete_raw(
        secrets  = {"_ai": service, "_route": "chat", "_pinned_model": "chatgpt"},
        route    = "reasoning",
        messages = [{"role": "user", "content": "plan"}],
    )
    assert service.complete.await_args.args[0] == "reasoning"
    assert service.complete.await_args.kwargs["pinned_model"] is None
    await complete_raw(
        secrets  = {"_ai": service, "_route": "chat", "_pinned_model": "chatgpt"},
        messages = [{"role": "user", "content": "chat"}],
    )
    assert service.complete.await_args.args[0] == "chat"
    assert service.complete.await_args.kwargs["pinned_model"] == "chatgpt"
