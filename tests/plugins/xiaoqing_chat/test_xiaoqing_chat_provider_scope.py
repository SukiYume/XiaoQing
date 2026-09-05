"""模型提供方权限和作用域。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.settings_snapshot import with_settings_reader
from tests.helpers.xiaoqing_chat_test_support import (
    Any,
    AsyncMock,
    MagicMock,
    Mock,
    PluginCapabilities,
    PluginPrincipal,
    SimpleNamespace,
    _make_hctx,
    _provider_test_ai,
    _set_context_principal,
    asyncio,
    patch,
    pytest,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_handle_provider_list_mode_remains_public(mock_context, sample_group_event):
    from core.config import ConfigSnapshot
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                = ChatRuntimeState()
    mock_context.secrets = ConfigSnapshot(
        config  = {},
        secrets = {},
    ).secrets
    _set_context_principal(mock_context, sample_group_event, group_role="member")

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("", sample_group_event, mock_context)

    assert state.get_chat_provider("g67890") is None
    assert "LLM 模型" in result[0]["data"]["text"]
    assert "当前会话覆盖" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_group_admin_switch_is_scoped_to_current_group(
    mock_context,
    sample_group_event,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.helper_utils import _get_ai_route_context
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                = ChatRuntimeState()
    mock_context.secrets = {}
    _set_context_principal(mock_context, sample_group_event, group_role="admin")

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.helper_utils._state", return_value=state),
    ):
        result = await handle_provider("glm", sample_group_event, mock_context)
        group_a = _get_ai_route_context(mock_context, chat_id="g67890")
        group_b = _get_ai_route_context(mock_context, chat_id="g99999")

    assert state.get_chat_provider("g67890") == "glm"
    assert state.global_active_provider is None
    assert group_a["_provider_name"] == "glm"
    assert group_b["_provider_name"] == "deepseek"
    assert "已将当前会话模型切换到" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_global_scope_requires_bot_admin(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.helper_utils import _get_ai_route_context
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                = ChatRuntimeState()
    mock_context.secrets = {}
    _set_context_principal(mock_context, sample_group_event, group_role="owner")

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.helper_utils._state", return_value=state),
    ):
        denied = await handle_provider("global glm", sample_group_event, mock_context)
        _set_context_principal(
            mock_context,
            sample_group_event,
            group_role   = "member",
            is_bot_admin = True,
        )
        allowed = await handle_provider("global glm", sample_group_event, mock_context)
        other_group = _get_ai_route_context(mock_context, chat_id="g99999")
        await handle_provider("deepseek", sample_group_event, mock_context)
        local = _get_ai_route_context(mock_context, chat_id="g67890")
        await handle_provider("default", sample_group_event, mock_context)
        inherited = _get_ai_route_context(mock_context, chat_id="g67890")
        await handle_provider("global default", sample_group_event, mock_context)
        reset_global = _get_ai_route_context(mock_context, chat_id="g99999")

    assert "Bot 全局管理员" in denied[0]["data"]["text"]
    assert "全局运行时模型" in allowed[0]["data"]["text"]
    assert other_group["_provider_name"] == "glm"
    assert local["_provider_name"] == "deepseek"
    assert inherited["_provider_name"] == "glm"
    assert reset_global["_provider_name"] == "deepseek"
    assert state.global_active_provider is None


@pytest.mark.asyncio
async def test_handle_provider_private_chat_requires_bot_admin(
    mock_context,
    sample_private_event,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                = ChatRuntimeState()
    mock_context.secrets = {}
    _set_context_principal(mock_context, sample_private_event)

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        denied = await handle_provider("glm", sample_private_event, mock_context)
        _set_context_principal(mock_context, sample_private_event, is_bot_admin=True)
        allowed = await handle_provider("glm", sample_private_event, mock_context)

    assert "管理员" in denied[0]["data"]["text"]
    assert "已将当前会话模型切换到" in allowed[0]["data"]["text"]
    assert state.get_chat_provider(f"u{sample_private_event['user_id']}") == "glm"


@pytest.mark.asyncio
async def test_handle_provider_does_not_trust_raw_sender_role(
    mock_context,
    sample_group_event,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    event = dict(sample_group_event)
    event["sender"] = dict(sample_group_event["sender"], role="owner")
    mock_context.secrets = {}
    _set_context_principal(mock_context, event, group_role="unknown")

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("glm", event, mock_context)

    assert "管理员" in result[0]["data"]["text"]
    assert state.get_chat_provider("g67890") is None


@pytest.mark.asyncio
async def test_handle_provider_rejects_private_principal_in_group_scope(
    mock_context,
    sample_group_event,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state                  = ChatRuntimeState()
    mock_context.secrets   = {}
    mock_context.principal = SimpleNamespace(
        kind       = "user",
        user_id    = sample_group_event["user_id"],
        group_id   = sample_group_event["group_id"],
        is_private = True,
        group_role = "owner",
    )
    mock_context.capabilities = PluginCapabilities(ai=_provider_test_ai())

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("glm", sample_group_event, mock_context)

    assert "管理员" in result[0]["data"]["text"]
    assert state.get_chat_provider("g67890") is None


@pytest.mark.asyncio
async def test_handle_provider_concurrent_groups_do_not_overwrite_each_other():
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state  = ChatRuntimeState()
    config = {
        "plugins": {
            "xiaoqing_chat": {
                "ai": {
                    "default_model_alias": "deepseek",
                    "model_aliases": {
                        "deepseek": "deepseek-flash",
                        "glm": "glm-5.2",
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

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
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


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_uses_passed_bound_state(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded

    state                              = MagicMock()
    state.review_store.cleanup_expired = Mock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.set_last_observe_ts             = Mock()

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

    state  = ChatRuntimeState()
    result = state.fetch_and_increment_local_id("test_chat")
    assert result == 1
    result2 = state.fetch_and_increment_local_id("test_chat")
    assert result2 == 2


def test_chat_id_requires_group_or_user_identifier():
    from plugins.xiaoqing_chat.helper_utils import _chat_id

    with pytest.raises(ValueError, match="missing chat identifier"):
        _chat_id({"message_id": 1})


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
        return_value=SimpleNamespace(
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
