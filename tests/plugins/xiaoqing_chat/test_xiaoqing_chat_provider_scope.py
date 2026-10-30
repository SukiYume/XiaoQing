"""模型提供方权限和作用域。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.router import build_command_catalog_node, command_access
from plugins.xiaoqing_chat.planning.pfc_engine import PFCRunResult
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader
from tests.helpers.xiaoqing_chat_test_support import (
    PluginCapabilities,
    PluginPrincipal,
    _make_hctx,
    _provider_test_ai,
    _set_context_principal,
    patch_turn,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_handle_provider_list_mode_shows_the_fallback_chain(mock_context, sample_group_event):
    from core.config import ConfigSnapshot
    from plugins.xiaoqing_chat.commands import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                = ChatRuntimeState()
    mock_context.secrets = ConfigSnapshot(
        config  = {},
        secrets = {},
    ).secrets
    _set_context_principal(mock_context, sample_group_event, group_role="admin")

    with patch("plugins.xiaoqing_chat.commands.get_state", return_value=state):
        result = await handle_provider("", sample_group_event, mock_context)

    assert state.get_chat_provider("g67890") is None
    assert "LLM 模型" in result[0]["data"]["text"]
    assert "当前会话覆盖" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_group_admin_switch_is_scoped_to_current_group(
    mock_context,
    sample_group_event,
):
    from plugins.xiaoqing_chat.chat_runtime import ai_route_context
    from plugins.xiaoqing_chat.commands import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                = ChatRuntimeState()
    mock_context.secrets = {}
    _set_context_principal(mock_context, sample_group_event, group_role="admin")

    with (
        patch("plugins.xiaoqing_chat.commands.get_state", return_value=state),
        patch("plugins.xiaoqing_chat.chat_runtime._state", return_value=state),
    ):
        result = await handle_provider("glm", sample_group_event, mock_context)
        group_a = ai_route_context(mock_context, chat_id="g67890")
        group_b = ai_route_context(mock_context, chat_id="g99999")

    assert state.get_chat_provider("g67890") == "glm"
    assert state.global_active_provider is None
    assert group_a["_provider_name"] == "glm"
    assert group_b["_provider_name"] == "deepseek"
    assert "已将当前会话模型切换到" in result[0]["data"]["text"]


def _xc_node(*path: str):
    manifest = json.loads(
        (REPOSITORY_ROOT / "plugins" / "xiaoqing_chat" / "plugin.json").read_text(encoding="utf-8")
    )
    node = build_command_catalog_node("xiaoqing_chat", manifest["commands"][0], root=True)
    for name in path:
        node = node.resolve_child(name)
        assert node is not None, path
    return node


def _group_user(role: str, *, admin: bool = False) -> PluginPrincipal:
    return PluginPrincipal(
        kind         = "user",
        user_id      = 12345,
        group_id     = 67890,
        is_bot_admin = admin,
        group_role   = role,
    )


@pytest.mark.parametrize(
    ("path", "principal", "allowed"),
    [
        (("model",), _group_user("owner"), True),
        (("model",), _group_user("admin"), True),
        (("model",), _group_user("member"), False),
        (("model",), _group_user("unknown"), False),
        (("model",), _group_user("member", admin=True), True),
        (("model",), PluginPrincipal(kind="user", user_id=12345, is_private=True), False),
        (
            ("model",),
            PluginPrincipal(kind="user", user_id=12345, is_private=True, is_bot_admin=True),
            True,
        ),
        (("model", "global"), _group_user("owner"), False),
        (("model", "global"), _group_user("member", admin=True), True),
    ],
)
def test_model_switching_permission_is_declared_in_the_manifest(path, principal, allowed):
    """谁能查看和切换模型写在 plugin.json 里，由 Core 的 command_access 在调用插件前判断。"""

    assert (command_access(_xc_node(*path), principal) is None) is allowed


@pytest.mark.asyncio
async def test_handle_provider_global_switch_is_separate_from_session_switch(
    mock_context,
    sample_group_event,
):
    from plugins.xiaoqing_chat.chat_runtime import ai_route_context
    from plugins.xiaoqing_chat.commands import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                = ChatRuntimeState()
    mock_context.secrets = {}
    _set_context_principal(
        mock_context,
        sample_group_event,
        group_role   = "member",
        is_bot_admin = True,
    )

    with (
        patch("plugins.xiaoqing_chat.commands.get_state", return_value=state),
        patch("plugins.xiaoqing_chat.chat_runtime._state", return_value=state),
    ):
        allowed = await handle_provider("global glm", sample_group_event, mock_context)
        other_group = ai_route_context(mock_context, chat_id="g99999")
        await handle_provider("deepseek", sample_group_event, mock_context)
        local = ai_route_context(mock_context, chat_id="g67890")
        await handle_provider("default", sample_group_event, mock_context)
        inherited = ai_route_context(mock_context, chat_id="g67890")
        await handle_provider("global default", sample_group_event, mock_context)
        reset_global = ai_route_context(mock_context, chat_id="g99999")

    assert "全局运行时模型" in allowed[0]["data"]["text"]
    assert other_group["_provider_name"] == "glm"
    assert local["_provider_name"] == "deepseek"
    assert inherited["_provider_name"] == "glm"
    assert reset_global["_provider_name"] == "deepseek"
    assert state.global_active_provider is None


@pytest.mark.asyncio
async def test_handle_provider_concurrent_groups_do_not_overwrite_each_other():
    from plugins.xiaoqing_chat.commands import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state  = ChatRuntimeState()
    config = {
        "plugins": {
            "xiaoqing_chat": {
                "ai": {
                    "default_model_alias": "deepseek",
                    "model_aliases"      : {
                        "deepseek": "deepseek-flash",
                        "glm"     : "glm-5.2",
                    },
                }
            }
        }
    }
    event_a   = {"user_id": 1, "group_id": 10}
    event_b   = {"user_id": 2, "group_id": 20}
    context_a = with_settings_reader(
        SimpleNamespace(
            secrets = {},
            config  = config,
            principal=PluginPrincipal(kind="user", user_id=1, group_id=10, group_role="admin"),
            capabilities=PluginCapabilities(ai=_provider_test_ai()),
            logger     = MagicMock(),
            request_id = "provider-a",
        )
    )
    context_b = with_settings_reader(
        SimpleNamespace(
            secrets = {},
            config  = config,
            principal=PluginPrincipal(kind="user", user_id=2, group_id=20, group_role="owner"),
            capabilities=PluginCapabilities(ai=_provider_test_ai()),
            logger     = MagicMock(),
            request_id = "provider-b",
        )
    )

    with patch("plugins.xiaoqing_chat.commands.get_state", return_value=state):
        await asyncio.gather(
            handle_provider("glm", event_a, context_a),
            handle_provider("deepseek", event_b, context_b),
        )

    assert state.get_chat_provider("g10") == "glm"
    assert state.get_chat_provider("g20") == "deepseek"


def test_provider_resolution_prunes_removed_provider_overrides() -> None:
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    state.set_chat_provider("g1", "removed")
    state.set_chat_provider("g2", "still-present")
    state.set_global_provider("removed")

    resolved = state.resolve_provider_name(
        "g1",
        ["default-provider", "still-present"],
        "default-provider",
    )

    assert resolved == "default-provider"
    assert state.global_active_provider is None
    assert state.get_chat_provider("g1") is None
    assert state.get_chat_provider("g2") == "still-present"


def test_next_local_id_increments_in_event_loop_order():
    """同步编号分配按事件循环调用顺序递增。"""
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state  = ChatRuntimeState()
    result = state.fetch_and_increment_local_id("test_chat")
    assert result == 1
    result2 = state.fetch_and_increment_local_id("test_chat")
    assert result2 == 2


def test_chat_id_requires_group_or_user_identifier():
    from plugins.xiaoqing_chat.chat_event import event_chat_id

    with pytest.raises(ValueError, match="missing chat identifier"):
        event_chat_id({"message_id": 1})


@pytest.mark.asyncio
async def test_smalltalk_new_user_turn_clears_sticky_pfc_ended_before_planner_runs(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                              = asyncio.Lock()
    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()
    state.pfc_state_store.get_async       = AsyncMock(
        return_value=PFCConversationState(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = True,
            last_successful_reply_action = "say_goodbye",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
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
            personality=SimpleNamespace(card=SimpleNamespace(moods=()), state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    captured: dict[str, Any] = {}

    async def fake_run_pfc_once(**kwargs):
        captured["state_override"] = kwargs["state_override"]
        return PFCRunResult(reply="", action="wait", reason="再观察", ended=False)

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
            new=AsyncMock(return_value=""),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_generation.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch(
            "plugins.xiaoqing_chat.turn_delivery.most_recent_user_local_id",
            return_value="u1",
        ),
        patch("plugins.xiaoqing_chat.turn_delivery.spawn_post_reply_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers.schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers.schedule_action_history_flush"),
        patch_turn("schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.turn_delivery.record_reply_frequency"),
        patch("plugins.xiaoqing_chat.handlers.log_step"),
    ):
        result = await _maybe_reply_smalltalk("新消息", sample_group_event, mock_context)

    assert result == []
    assert captured["state_override"].ended is False


def test_default_model_tracks_available_route_order_without_a_separate_default():
    from core.ai import AIModelInfo
    from plugins.xiaoqing_chat.chat_runtime import ai_route_context
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    models = [
        AIModelInfo("chatgpt", "openai", "chat-latest", ("text",)),
        AIModelInfo("deepseek-flash", "deepseek", "deepseek-flash", ("text",)),
    ]
    context = with_settings_reader(
        SimpleNamespace(
            config={
                "plugins": {
                    "xiaoqing_chat": {
                        "ai": {
                            "model_aliases": {"deepseek": "deepseek-flash"},
                        }
                    }
                }
            },
            secrets      = {},
            capabilities = PluginCapabilities(
                ai=SimpleNamespace(
                    list_models=lambda *args, **kwargs: tuple(models),
                )
            ),
        )
    )
    with patch("plugins.xiaoqing_chat.chat_runtime._state", return_value=ChatRuntimeState()):
        assert ai_route_context(context)["_default"] == "chatgpt"
        # Core omits the first entry while its provider has no key.
        models.pop(0)
        resolved = ai_route_context(context)
        assert resolved["_default"] == "deepseek"
        assert resolved["_profile"] == "deepseek-flash"
        assert resolved["_pinned_model"] is None
