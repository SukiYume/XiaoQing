"""聊天生成、模型网关与公开错误边界。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.public_errors import PUBLIC_ERROR_PREFIX
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from tests.helpers.assertions import logged_events
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    chat_message_event,
    plan_answer,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


def test_brain_chat_is_active_in_private_chat_when_enabled() -> None:
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


@pytest.fixture(autouse=True)
def _fresh_stalled_notice():
    """“卡了”提示的十分钟限频记在进程内：每个用例前后清空，不漏给别的用例。"""

    from plugins.xiaoqing_chat import handlers

    handlers._last_stalled_notice.clear()
    yield
    handlers._last_stalled_notice.clear()


def _failing_model_script(call) -> str:
    raise RuntimeError("internal /tmp/secrets/token.txt leak")


@pytest.mark.asyncio
async def test_a_failed_private_turn_gets_one_in_character_line_and_no_error_code(
    mock_context, sample_private_event, caplog
) -> None:
    from plugins.xiaoqing_chat.handlers import _STALLED_REPLIES, handle_smalltalk

    model = ScriptedChatModel(reply=_failing_model_script)
    scripted_chat_context(mock_context, model)

    first = await handle_smalltalk(
        "你好", chat_message_event(sample_private_event, "你好"), mock_context
    )
    second = await handle_smalltalk(
        "在吗", chat_message_event(sample_private_event, "在吗", message_id=457), mock_context
    )

    assert [segment["data"]["text"] for segment in first] == [first[0]["data"]["text"]]
    assert first[0]["data"]["text"] in _STALLED_REPLIES
    # 故障还在持续：同一会话不再重复说，也不出现错误码和异常内容。
    assert second == []
    assert PUBLIC_ERROR_PREFIX not in str(first) and "internal" not in str(first)
    # 诊断信息照常记进日志，供排查。
    assert logged_events(caplog, "public_error")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_a_failed_group_interjection_is_silent(
    mock_context, sample_group_event, caplog
) -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    # 概率放行了这句插话，规划器决定接话，生成时模型出了故障。
    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply=_failing_model_script)
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)

    interjection = chat_message_event(sample_group_event, "今天好冷")
    assert await handle_smalltalk("今天好冷", interjection, mock_context) == []
    assert logged_events(caplog, "public_error")
    # 被点名时才说一句“卡了”。
    caplog.clear()
    addressed = await handle_smalltalk(
        "小青在吗", chat_message_event(sample_group_event, "小青在吗", message_id=2), mock_context
    )
    assert len(addressed) == 1 and PUBLIC_ERROR_PREFIX not in str(addressed)
    assert logged_events(caplog, "public_error")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_an_invalid_deployment_config_ends_a_private_turn_in_the_stalled_line(
    mock_context, sample_private_event, caplog
) -> None:
    from plugins.xiaoqing_chat.handlers import _STALLED_REPLIES, handle_smalltalk

    # 部署配置里的值不合格：这个插件目录第一次加载就失败，没有可退回的旧配置。
    model = ScriptedChatModel()
    scripted_chat_context(mock_context, model, {"reply_check": {"max_replan": "many"}})

    reply = await handle_smalltalk(
        "你好", chat_message_event(sample_private_event, "你好"), mock_context
    )

    assert [segment["data"]["text"] for segment in reply] == [reply[0]["data"]["text"]]
    assert reply[0]["data"]["text"] in _STALLED_REPLIES
    assert "max_replan" not in str(reply) and PUBLIC_ERROR_PREFIX not in str(reply)
    assert logged_events(caplog, "public_error")
    assert not model.calls


def test_llm_call_config_contains_only_runtime_controls() -> None:

    config = LLMCallConfig(timeout_seconds=3.0, max_retry=0, retry_interval_seconds=0.2)

    assert config.request_controls() == {
        "timeout_seconds"       : 3.0,
        "total_timeout_seconds" : 60.0,
        "max_retry"             : 0,
        "retry_interval_seconds": 0.2,
    }


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
