"""插件身份、能力和服务边界。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import tests.helpers.app_test_support as _fixture_support
from core.router import build_command_catalog_node, command_access
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState, get_state, reset_global_state
from tests.helpers.app_test_support import (
    PROBE,
    UNUSED_COMMAND_SOURCE,
    DeliveryTarget,
    OneBotActionOutcomeUnknown,
    PluginPrincipal,
    XiaoQingApp,
    command_declaration,
    load_shipped_plugin,
    load_test_plugin,
    plugin_context_for,
    service_declaration,
    set_app_config,
)
from tests.helpers.paths import REPOSITORY_ROOT

temp_app_root                               = _fixture_support.temp_app_root
_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

CODEX_ARXIV_SERVICE = service_declaration(
    "codex.enqueue_arxiv_summary",
    "enqueue_arxiv_summary",
    ("arxiv_filter",),
    required_capability="codex_arxiv_summary",
)


async def _publish_secret_command(app: XiaoQingApp) -> None:
    """装入声明了 /set_secret（只给 Bot 管理员、仅私聊）的 bot_core；秘密管理能力按它授权。"""

    await load_test_plugin(
        app,
        "bot_core",
        UNUSED_COMMAND_SOURCE,
        commands=[command_declaration("set_secret", permission="bot_admin", contexts=("private",))],
    )


async def _publish_codex_command(app: XiaoQingApp) -> None:
    """只声明 /codex（只给 Bot 管理员）的 codex 插件；Codex 能力按它授权。"""

    await load_test_plugin(
        app,
        "codex",
        UNUSED_COMMAND_SOURCE,
        commands=[command_declaration("codex", permission="bot_admin")],
    )


VOICE_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def synthesize(value, context):
    PROBE.log("synthesize").append(context)
    return [{"type": "text", "data": {"text": value}}]

async def shutdown():
    raise RuntimeError("shutdown failed")
"""

CHAT_REPLY_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def reply(text, event, context):
    PROBE.log("reply").append(context)
    return [{"type": "text", "data": {"text": f"{text}:{event['user_id']}"}}]
"""

# ``{result}`` 是这一代插件把请求记成什么、并交回给调用方的值。
CODEX_ENQUEUE_SOURCE = (
    UNUSED_COMMAND_SOURCE
    + """
from tests.helpers.plugin_probe import PROBE

async def enqueue_arxiv_summary(date, links, user_id, group_id, context):
    PROBE.log("enqueue").append(
        (context, {{"date": date, "links": links, "user_id": user_id, "group_id": group_id}})
    )
    PROBE.log("enqueue_value").append("{result}")
    return "{result}"
"""
)


@pytest.fixture
def chat_runtime_state():
    """聊天插件的进程内运行状态是单例：每条用到它的测试从全新状态开始、结束后清掉。"""

    reset_global_state()
    yield get_state()
    reset_global_state()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_grants_only_plugin_scoped_capabilities(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    secrets = {
        "admin_user_ids": [12345],
        "onebot_token"  : "top-level-token",
        "inbound_token" : "inbound-token",
        "plugins"       : {
            "bot_core"     : {"own_key": "own-value"},
            "other"        : {"hidden_key": "hidden-value"},
            "xiaoqing_chat": {"chat_key": "chat-value"},
        },
    }
    app.config_manager.secrets_path.write_text(json.dumps(secrets), encoding="utf-8")
    app.config_manager.reload()
    app.identity_service.load_admins(secrets)
    await _publish_secret_command(app)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id    = 12345,
        group_id   = None,
        is_private = True,
    )

    name_only_context = plugin_context_for(
        app,
        "bot_core",
        user_id   = 12345,
        principal = principal,
    )
    assert name_only_context.capabilities.secret_admin is None

    bot_context = plugin_context_for(
        app,
        "bot_core",
        user_id               = 12345,
        principal             = principal,
        manifest_capabilities = frozenset({"secret_admin"}),
    )
    assert set(bot_context.secrets["plugins"]) == {"bot_core"}
    assert "onebot_token" not in bot_context.secrets
    assert bot_context.capabilities.is_bot_admin is True
    assert bot_context.capabilities.is_system is False
    assert bot_context.capabilities.secret_admin is not None
    assert bot_context.capabilities.secret_admin.get("plugins.other.hidden_key") == "hidden-value"

    ordinary_context = plugin_context_for(
        app,
        "other",
        user_id   = 12345,
        principal = principal,
    )
    assert set(ordinary_context.secrets["plugins"]) == {"other"}
    assert ordinary_context.capabilities.secret_admin is None
    assert ordinary_context.capabilities.onebot_media is None
    assert ordinary_context.capabilities.config_subscription is None

    media_context = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id               = 12345,
        principal             = principal,
        manifest_capabilities = frozenset({"onebot_media"}),
    )
    assert media_context.capabilities.onebot_media is not None
    assert "onebot_http_base" not in media_context.config
    assert "onebot_token" not in media_context.secrets

    pendo_context = plugin_context_for(
        app,
        "pendo",
        user_id               = 12345,
        principal             = principal,
        manifest_capabilities = frozenset({"config_subscription"}),
    )
    assert pendo_context.capabilities.config_subscription is not None
    assert pendo_context.capabilities.secret_admin is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ai_capability_reads_one_fresh_snapshot_per_call(temp_app_root: Path) -> None:
    from core.ai import AICompletionResult

    app = XiaoQingApp(temp_app_root)
    app.http_session = SimpleNamespace(closed=False)

    def install(model: str, api_key: str) -> None:
        config       = app.config_manager.snapshot().mutable_config()
        config["ai"] = {
            "providers": {
                "test": {
                    "api_base"     : "https://llm.example/v1",
                    "endpoint_path": "/chat/completions",
                }
            },
            "models": {
                "profile": {
                    "provider"  : "test",
                    "model"     : model,
                    "modalities": ["text"],
                }
            },
        }
        config["plugins"] = {"demo": {"ai": {"routes": {"chat": {"models": ["profile"]}}}}}
        secrets           = app.config_manager.snapshot().mutable_secrets()
        secrets["ai"]     = {"providers": {"test": {"api_key": api_key}}}
        app.config_manager._replace_snapshot(config, secrets)

    install("model-v1", "key-v1")
    context = plugin_context_for(app, "demo")
    service = context.capabilities.ai
    assert service is not None
    # 插件自身的配置/密钥视图不包含全局 registry 凭据。
    assert "ai" not in context.secrets

    observed: list[tuple[str, str]] = []

    async def fake_complete_configured_route(**kwargs: Any) -> AICompletionResult:
        observed.append(
            (
                kwargs["config"]["ai"]["models"]["profile"]["model"],
                kwargs["secrets"]["ai"]["providers"]["test"]["api_key"],
            )
        )
        return AICompletionResult(
            response      = {"choices": [{"message": {"content": "ok"}}]},
            profile       = "profile",
            provider      = "test",
            model         = observed[-1][0],
            finish_reason = "stop",
            attempts      = 1,
        )

    with patch(
        "core.app_plugin_context.complete_configured_route",
        new=AsyncMock(side_effect=fake_complete_configured_route),
    ):
        await service.complete("chat", [{"role": "user", "content": "first"}])
        install("model-v2", "key-v2")
        await service.complete("chat", [{"role": "user", "content": "second"}])

    assert observed == [("model-v1", "key-v1"), ("model-v2", "key-v2")]


@pytest.mark.unit
def test_app_rejects_forged_copied_and_mismatched_principals(temp_app_root: Path):
    app    = XiaoQingApp(temp_app_root)
    issued = app.issue_user_principal(
        {"user_id": 12345},
        user_id    = 12345,
        group_id   = None,
        is_private = True,
    )
    forged = PluginPrincipal(
        kind         = "user",
        user_id      = 12345,
        is_bot_admin = True,
        is_private   = True,
    )
    copied      = copy.copy(issued)
    deep_copied = copy.deepcopy(issued)
    assert copied is not issued
    assert deep_copied is not issued

    for principal in (forged, copied, deep_copied, PluginPrincipal(kind="scheduled_system")):
        with pytest.raises(PermissionError, match="not issued"):
            plugin_context_for(
                app,
                "test",
                user_id   = principal.user_id,
                group_id  = principal.group_id,
                principal = principal,
            )

    with pytest.raises(PermissionError, match="do not match"):
        plugin_context_for(app, "test", user_id=67890, principal=issued)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_recomputes_admin_capability_after_revocation(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    await _publish_secret_command(app)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id    = 12345,
        group_id   = None,
        is_private = True,
    )
    before = plugin_context_for(
        app,
        "bot_core",
        user_id               = 12345,
        principal             = principal,
        manifest_capabilities = frozenset({"secret_admin"}),
    )
    assert before.capabilities.is_bot_admin is True
    assert before.capabilities.secret_admin is not None

    app.identity_service.load_admins({})
    after = plugin_context_for(
        app,
        "bot_core",
        user_id               = 12345,
        principal             = principal,
        manifest_capabilities = frozenset({"secret_admin"}),
    )
    assert after.capabilities.is_bot_admin is False
    assert after.capabilities.secret_admin is None


@pytest.mark.unit
def test_app_issues_group_role_only_for_matching_sender(temp_app_root: Path):
    app      = XiaoQingApp(temp_app_root)
    matching = app.issue_user_principal(
        {"sender": {"user_id": 111, "role": "admin"}},
        user_id    = 111,
        group_id   = 222,
        is_private = False,
    )
    mismatched = app.issue_user_principal(
        {"sender": {"user_id": 999, "role": "owner"}},
        user_id    = 111,
        group_id   = 222,
        is_private = False,
    )
    private = app.issue_user_principal(
        {"sender": {"user_id": 111, "role": "owner"}},
        user_id    = 111,
        group_id   = None,
        is_private = True,
    )

    assert matching.can_manage_group(222) is True
    assert matching.can_manage_group(333) is False
    assert mismatched.group_role == "unknown"
    assert mismatched.can_manage_group(222) is False
    assert private.group_role == "unknown"
    assert private.can_manage_group(222) is False


@pytest.mark.unit
def test_pendo_config_subscription_is_scoped_and_unsubscribable(temp_app_root: Path):
    app               = XiaoQingApp(temp_app_root)
    config            = app.config_manager.snapshot().mutable_config()
    config["plugins"] = {
        "pendo": {"web_demo_enabled": True},
        "other": {"private_option": "hidden"},
    }
    app.config_manager.config_path.write_text(json.dumps(config), encoding="utf-8")
    app.config_manager.reload()
    context = plugin_context_for(
        app,
        "pendo",
        manifest_capabilities=frozenset({"config_subscription"}),
    )
    subscription = context.capabilities.config_subscription
    assert subscription is not None
    received    = []
    unsubscribe = subscription.subscribe(received.append)

    app.config_manager.update_secret("admin_user_ids", [12345])
    unsubscribe()
    unsubscribe()
    app.config_manager.update_secret("admin_user_ids", [12345, 67890])

    assert len(received) == 1
    assert set(received[0]["plugins"]) == {"pendo"}
    assert received[0]["plugins"]["pendo"]["web_demo_enabled"] is True
    assert "other" not in received[0]["plugins"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bot_core_secret_admin_works_with_production_context(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    secrets = {
        "admin_user_ids": [12345],
        "onebot_token"  : "",
        "inbound_token" : "",
        "plugins"       : {"bot_core": {"managed": "before", "values": [1, 2]}},
    }
    app.config_manager.secrets_path.write_text(json.dumps(secrets), encoding="utf-8")
    app.config_manager.reload()
    app.identity_service.load_admins(secrets)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id    = 12345,
        group_id   = None,
        is_private = True,
    )
    bot_core = (await load_shipped_plugin(app, "bot_core")).module
    context  = app.plugin_manager.build_context(
        "bot_core",
        user_id   = 12345,
        principal = principal,
    )
    secret_admin = context.capabilities.secret_admin
    assert secret_admin is not None
    assert secret_admin.get("plugins.bot_core.managed") == "before"
    detached_values = secret_admin.get("plugins.bot_core.values")
    detached_values.append(3)
    assert app.config_manager.snapshot().secrets["plugins"]["bot_core"]["values"] == (1, 2)

    result = await bot_core.handle(
        "set_secret",
        "plugins.bot_core.managed after",
        {"user_id": 12345, "message_type": "private"},
        context,
    )

    assert "已更新" in result[0]["data"]["text"]
    assert app.config_manager.snapshot().secrets["plugins"]["bot_core"]["managed"] == "after"
    assert "after" not in repr(context.secrets)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_onebot_request_uses_correlated_outbound_transport_only(temp_app_root: Path):
    app             = XiaoQingApp(temp_app_root)
    failed_response = {"status": "failed", "retcode": 100, "data": {}}
    app.ws_client   = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        request_action=AsyncMock(return_value=failed_response),
    )
    app.http_sender = SimpleNamespace(
        http_base           = "http://onebot",
        credentials_trusted = True,
        request_action=AsyncMock(return_value={"status": "ok", "retcode": 0}),
    )
    app.inbound_manager = SimpleNamespace(broadcast=AsyncMock())

    result = await app._request_onebot_action("get_msg", {"message_id": 42})

    assert result is failed_response
    app.http_sender.request_action.assert_not_awaited()
    app.inbound_manager.broadcast.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_onebot_request_falls_back_to_http_on_ws_transport_failure(temp_app_root: Path):
    app           = XiaoQingApp(temp_app_root)
    http_response = {"status": "ok", "retcode": 0, "data": {"message_id": 42}}
    app.ws_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        request_action=AsyncMock(return_value=None),
    )
    app.http_sender = SimpleNamespace(
        http_base           = "http://onebot",
        credentials_trusted = True,
        request_action=AsyncMock(return_value=http_response),
    )

    result = await app._request_onebot_action("get_msg", {"message_id": 42})

    assert result is http_response
    app.http_sender.request_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_onebot_request_does_not_fallback_after_committed_ws_outcome_unknown(
    temp_app_root: Path,
):
    app           = XiaoQingApp(temp_app_root)
    app.ws_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        request_action=AsyncMock(side_effect=OneBotActionOutcomeUnknown("get_msg")),
    )
    app.http_sender = SimpleNamespace(
        http_base           = "http://onebot",
        credentials_trusted = True,
        request_action=AsyncMock(return_value={"status": "ok", "retcode": 0}),
    )

    assert await app._request_onebot_action("get_msg", {"message_id": 42}) is None

    app.ws_client.request_action.assert_awaited_once()
    app.http_sender.request_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_xiaoqing_media_capability_validates_and_crops_onebot_responses(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    # OneBot 连接是边界：Core 的 OneBot 请求经真实的 `_request_onebot_action` 到达它。
    request_action = AsyncMock(
        side_effect=[
            {"status": "ok", "retcode": 0, "data": {"message_id": 7, "raw": "ok"}},
            {"status": "ok", "retcode": 0, "data": {"file": "cached.png"}},
            {"status": "failed", "retcode": 100, "data": {"secret": "ignored"}},
        ]
    )
    app.ws_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        request_action      = request_action,
    )
    context = plugin_context_for(
        app,
        "xiaoqing_chat",
        manifest_capabilities=frozenset({"onebot_media"}),
    )
    media = context.capabilities.onebot_media
    assert media is not None

    assert await media.get_message(7) == {"message_id": 7, "raw": "ok"}
    assert await media.get_image(file_id="abc") == {"file": "cached.png"}
    assert await media.get_image(file="bad") == {}
    assert request_action.await_args_list[0].args == (
        {"action": "get_msg", "params": {"message_id": 7}},
    )
    assert request_action.await_args_list[1].args == (
        {"action": "get_image", "params": {"file_id": "abc"}},
    )

    with pytest.raises(ValueError):
        await media.get_message(True)
    with pytest.raises(ValueError):
        await media.get_image()
    with pytest.raises(ValueError):
        await media.get_image(file_id="a", file="b")
    with pytest.raises(ValueError):
        await media.get_image(file_id=1)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_onebot_actions_follow_the_qq_command_and_are_rechecked_on_each_call(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    # /qq 只给 Bot 管理员、仅私聊；OneBot 操作能力按它授权。
    await load_test_plugin(
        app,
        "xiaoqing_chat",
        UNUSED_COMMAND_SOURCE,
        commands=[command_declaration("qq", permission="bot_admin", contexts=("private",))],
    )
    response = {"status": "ok", "retcode": 0, "data": [{"group_id": 42}]}
    request_action = AsyncMock(return_value=response)
    app.ws_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        request_action      = request_action,
    )

    def granted(user_id: int, group_id: int | None = None):
        principal = app.issue_user_principal(
            {"user_id": user_id},
            user_id    = user_id,
            group_id   = group_id,
            is_private = group_id is None,
        )
        context = plugin_context_for(
            app,
            "xiaoqing_chat",
            user_id               = user_id,
            group_id              = group_id,
            principal             = principal,
            manifest_capabilities = frozenset({"onebot_actions"}),
        )
        return context.capabilities.onebot_actions

    assert granted(99999) is None
    assert granted(12345, group_id=42) is None
    onebot = granted(12345)
    assert onebot is not None

    assert await onebot.call("get_group_list") == response
    assert request_action.await_args.args == ({"action": "get_group_list", "params": {}},)
    with pytest.raises(ValueError):
        await onebot.call("get group list")
    with pytest.raises(TypeError):
        await onebot.call("get_msg", [7])  # type: ignore[arg-type]

    # 撤掉 Bot 管理员后，已经拿到手的能力也不能再用。
    app.identity_service.load_admins({})
    with pytest.raises(PermissionError):
        await onebot.call("get_group_list")
    assert request_action.await_count == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_declared_voice_service_preserves_signed_principal_and_target_scope(
    temp_app_root: Path,
):
    app     = XiaoQingApp(temp_app_root)
    secrets = {
        "admin_user_ids": [12345],
        "onebot_token"  : "",
        "inbound_token" : "",
        "plugins"       : {
            "smalltalk": {"source_key": "s"},
            "voice"    : {"target_key": "t"},
            "chat"     : {"chat_key": "c"},
        },
    }
    config            = app.config_manager.snapshot().mutable_config()
    config["plugins"] = {
        "smalltalk": {"source_option": 1},
        "voice"    : {"target_option": 2},
        "chat"     : {"chat_option": 3},
    }
    app.config_manager._replace_snapshot(config, secrets)
    app.identity_service.load_admins(secrets)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id    = 12345,
        group_id   = None,
        is_private = True,
    )
    seen_contexts = PROBE.log("synthesize")
    await load_test_plugin(
        app,
        "voice",
        VOICE_SOURCE,
        services    = [service_declaration("voice.synthesize_text", "synthesize", ("smalltalk",))],
        concurrency = "sequential",
    )
    source = plugin_context_for(
        app,
        "smalltalk",
        user_id       = 12345,
        principal     = principal,
        uses_services = frozenset({"chat.reply", "voice.synthesize_text"}),
    )
    service      = source.capabilities.voice_synthesis
    chat_service = source.capabilities.chat_reply
    assert service is not None
    assert chat_service is not None
    assert not hasattr(source, "call_plugin")

    chat_contexts = PROBE.log("reply")
    await load_test_plugin(
        app,
        "chat",
        CHAT_REPLY_SOURCE,
        services=[service_declaration("chat.reply", "reply", ("smalltalk",))],
    )
    assert await chat_service.reply("hello", {"user_id": 12345}) == [
        {"type": "text", "data": {"text": "hello:12345"}},
    ]
    assert chat_contexts[-1].plugin_name == "chat"

    assert await service.synthesize_text("first") == [
        {"type": "text", "data": {"text": "first"}},
    ]
    first = seen_contexts[-1]
    assert first.principal is principal
    assert first.capabilities.is_bot_admin is True
    assert set(first.secrets["plugins"]) == {"voice"}
    assert set(first.config["plugins"]) == {"voice"}
    assert first.state is app.plugin_manager._plugin_states["voice"]

    app.identity_service.load_admins({})
    assert await service.synthesize_text("second") == [
        {"type": "text", "data": {"text": "second"}},
    ]
    assert seen_contexts[-1].capabilities.is_bot_admin is False

    # 关闭钩子失败的插件卸载后留在隔离状态：它导出的服务不再接受调用。
    await app.plugin_manager.unload_plugin("voice")
    with pytest.raises(RuntimeError, match="not accepting calls"):
        await service.synthesize_text("third")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_arxiv_codex_capability_uses_real_target_context_and_rechecks_admin(
    temp_app_root: Path,
):
    app     = XiaoQingApp(temp_app_root)
    secrets = {
        "admin_user_ids": [12345],
        "onebot_token"  : "",
        "inbound_token" : "",
        "plugins"       : {"arxiv_filter": {"source": "s"}, "codex": {"target": "t"}},
    }
    config            = app.config_manager.snapshot().mutable_config()
    config["plugins"] = {"arxiv_filter": {"source_option": 1}, "codex": {"target_option": 2}}
    app.config_manager._replace_snapshot(config, secrets)
    app.identity_service.load_admins(secrets)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id    = 12345,
        group_id   = None,
        is_private = True,
    )
    captured = PROBE.log("enqueue")
    await load_test_plugin(
        app,
        "codex",
        CODEX_ENQUEUE_SOURCE.format(result="queued"),
        commands=[command_declaration("codex", permission="bot_admin")],
        services    = [CODEX_ARXIV_SERVICE],
        concurrency = "sequential",
    )
    source = plugin_context_for(
        app,
        "arxiv_filter",
        user_id       = 12345,
        principal     = principal,
        uses_services = frozenset({"codex.enqueue_arxiv_summary"}),
    )
    service = source.capabilities.codex_arxiv_summary
    assert service is not None

    assert (
        await service.enqueue_or_replay(
            date  = "2026-07-11",
            links = ["https://arxiv.org/abs/2607.00001"],
        )
        == "queued"
    )
    target, kwargs = captured[-1]
    assert target.plugin_name == "codex"
    assert target.principal is principal
    assert target.request_id == "capability-test"
    assert target.state is app.plugin_manager._plugin_states["codex"]
    assert set(target.config["plugins"]) == {"codex"}
    assert set(target.secrets["plugins"]) == {"codex"}
    assert kwargs["user_id"] == 12345

    app.identity_service.load_admins({})
    with pytest.raises(PermissionError, match="no longer authorized"):
        await service.enqueue_or_replay(
            date  = "2026-07-11",
            links = ["https://arxiv.org/abs/2607.00001"],
        )
    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_arxiv_codex_capability_is_source_scoped_and_resolves_current_loaded_gate(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    await _publish_codex_command(app)
    principal = app.issue_user_principal(
        {"user_id": 12345},
        user_id    = 12345,
        group_id   = None,
        is_private = True,
    )
    assert (
        plugin_context_for(
            app,
            "other",
            user_id   = 12345,
            principal = principal,
        ).capabilities.codex_arxiv_summary
        is None
    )
    service = plugin_context_for(
        app,
        "arxiv_filter",
        user_id       = 12345,
        principal     = principal,
        uses_services = frozenset({"codex.enqueue_arxiv_summary"}),
    ).capabilities.codex_arxiv_summary
    assert service is not None

    with pytest.raises(RuntimeError, match="unavailable"):
        await service.enqueue_or_replay(
            date  = "2026-07-11",
            links = ["https://arxiv.org/abs/2607.00001"],
        )

    calls = PROBE.log("enqueue_value")

    async def install(value: str) -> None:
        """改写插件源码再加载一次：和热重载一样，服务绑定换成新一代，/codex 随之重新发布。"""

        await load_test_plugin(
            app,
            "codex",
            CODEX_ENQUEUE_SOURCE.format(result=value),
            commands=[command_declaration("codex", permission="bot_admin")],
            services    = [CODEX_ARXIV_SERVICE],
            concurrency = "sequential",
        )

    await install("first")
    assert (
        await service.enqueue_or_replay(
            date  = "2026-07-11",
            links = ["https://arxiv.org/abs/2607.00001"],
        )
        == "first"
    )
    await install("second")
    assert (
        await service.enqueue_or_replay(
            date  = "2026-07-11",
            links = ["https://arxiv.org/abs/2607.00001"],
        )
        == "second"
    )
    assert calls == ["first", "second"]

    # 插件卸载后，/codex 的声明随之撤销，已经发出的能力下一次调用就被拒绝。
    await app.plugin_manager.unload_plugin("codex")
    with pytest.raises(PermissionError, match="no longer authorized"):
        await service.enqueue_or_replay(
            date  = "2026-07-11",
            links = ["https://arxiv.org/abs/2607.00001"],
        )


# 计划任务到期时记下 Core 交给插件的上下文和投递目标，返回 ``PROBE.script("scheduled", ...)`` 预设的结果。
SCHEDULED_CONTEXT_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def scheduled(context):
    PROBE.log("contexts").append(context)
    PROBE.log("targets").append(context.principal.delivery_targets)
    return PROBE.next_result("scheduled")
"""


def _schedule_entry(schedule_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": schedule_id, "handler": "scheduled", "cron": {"minute": "*"}, **fields}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_run_job_builds_real_system_capability_context(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    await load_test_plugin(
        app,
        "scheduled_test",
        SCHEDULED_CONTEXT_SOURCE,
        schedule=[_schedule_entry("poll", group_ids=[123, 456])],
    )
    app._reschedule("startup")
    assert app.scheduler.scheduler is not None
    (job,) = app.scheduler.scheduler.get_jobs()

    await job.func()

    (context,) = PROBE.log("contexts")
    assert context.principal.kind == "scheduled_system"
    assert context.principal.user_id is None
    assert context.principal.group_id is None
    assert context.principal.delivery_targets == (
        DeliveryTarget("group", 123),
        DeliveryTarget("group", 456),
    )
    assert context.capabilities.is_system is True
    assert context.capabilities.is_bot_admin is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scheduled_job_freezes_default_explicit_and_empty_delivery_targets(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    set_app_config(app, default_group_ids=[9])
    PROBE.script("scheduled", "scheduled result", "scheduled result", "scheduled result")
    app.http_sender = SimpleNamespace(
        http_base           = "http://onebot",
        credentials_trusted = True,
        send_action=AsyncMock(return_value=True),
    )
    await load_test_plugin(
        app,
        "scheduled_test",
        SCHEDULED_CONTEXT_SOURCE,
        schedule=[
            _schedule_entry("default"),
            _schedule_entry("empty", group_ids=[]),
            _schedule_entry("explicit", group_ids=[11, 22]),
        ],
    )
    app._reschedule("startup")
    assert app.scheduler.scheduler is not None
    jobs = {job.id: job for job in app.scheduler.scheduler.get_jobs()}

    for schedule_id in ("default", "empty", "explicit"):
        await jobs[f"plugin.scheduled_test.{schedule_id}"].func()

    assert PROBE.log("targets") == [
        (DeliveryTarget("group", 9),),
        (),
        (DeliveryTarget("group", 11), DeliveryTarget("group", 22)),
    ]
    sent = app.http_sender.send_action.await_args_list  # type: ignore[attr-defined]
    assert [call.args[0]["params"]["group_id"] for call in sent] == [9, 11, 22]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_xiaoqing_provider_scope_uses_production_principal_capabilities(
    temp_app_root: Path,
    chat_runtime_state: ChatRuntimeState,
):
    from plugins.xiaoqing_chat.chat_runtime import ai_route_context
    from plugins.xiaoqing_chat.commands import handle_provider

    app          = XiaoQingApp(temp_app_root)
    config       = app.config_manager.snapshot().mutable_config()
    config["ai"] = {
        "providers": {
            "deepseek": {
                "api_base"     : "https://a.example",
                "endpoint_path": "/chat/completions",
            },
            "zhipu": {
                "api_base"     : "https://b.example",
                "endpoint_path": "/chat/completions",
            },
        },
        "models": {
            "deepseek-flash": {
                "provider"  : "deepseek",
                "model"     : "deepseek-chat",
                "modalities": ["text"],
            },
            "glm-5.2": {
                "provider"  : "zhipu",
                "model"     : "glm-5.2",
                "modalities": ["text"],
            },
        },
    }
    config["plugins"] = {
        "xiaoqing_chat": {
            "ai": {
                "model_aliases": {
                    "deepseek": "deepseek-flash",
                    "glm"     : "glm-5.2",
                },
                "routes": {"chat": {"models": ["deepseek-flash", "glm-5.2"]}},
            }
        }
    }
    secrets       = app.config_manager.snapshot().mutable_secrets()
    secrets["ai"] = {
        "providers": {
            "deepseek": {"api_key": "<DEEPSEEK_API_KEY>"},
            "zhipu"   : {"api_key": "<ZHIPU_API_KEY>"},
        }
    }
    app.config_manager._replace_snapshot(config, secrets)
    app.identity_service.load_admins(secrets)
    group_admin_event = {
        "user_id" : 777,
        "group_id": 100,
        "sender"  : {"user_id": 777, "role": "admin"},
    }
    group_admin = app.issue_user_principal(
        group_admin_event,
        user_id    = 777,
        group_id   = 100,
        is_private = False,
    )
    group_context = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id   = 777,
        group_id  = 100,
        principal = group_admin,
    )
    state = chat_runtime_state

    local_result = await handle_provider("glm", group_admin_event, group_context)
    group_a = ai_route_context(group_context, chat_id="g100")
    group_b = ai_route_context(group_context, chat_id="g200")

    bot_admin_event = {
        "user_id" : 12345,
        "group_id": 200,
        "sender"  : {"user_id": 12345, "role": "member"},
    }
    bot_admin = app.issue_user_principal(
        bot_admin_event,
        user_id    = 12345,
        group_id   = 200,
        is_private = False,
    )
    bot_context = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id   = 12345,
        group_id  = 200,
        principal = bot_admin,
    )
    global_result = await handle_provider("global glm", bot_admin_event, bot_context)

    assert "当前会话模型" in local_result[0]["data"]["text"]
    # 全局切换只给 Bot 管理员：写在 plugin.json 里，Core 用签发的身份在调用插件前判断。
    manifest = json.loads(
        (REPOSITORY_ROOT / "plugins" / "xiaoqing_chat" / "plugin.json").read_text(encoding="utf-8")
    )
    xc_root = build_command_catalog_node("xiaoqing_chat", manifest["commands"][0], root=True)
    global_node = xc_root.resolve_child("model").resolve_child("global")
    assert command_access(global_node, group_admin) == "bot_admin"
    assert command_access(global_node, bot_admin) is None
    assert group_a["_provider_name"] == "glm"
    assert group_b["_provider_name"] == "deepseek"
    assert "全局运行时模型" in global_result[0]["data"]["text"]
    assert state.global_active_provider == "glm"


@pytest.mark.unit
def test_inbound_backlog_capability_is_scoped_to_the_current_sender(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)

    def message(message_id: int, user_id: int, group_id: int | None) -> dict[str, Any]:
        event: dict[str, Any] = {
            "post_type" : "message",
            "user_id"   : user_id,
            "message_id": message_id,
            "message"   : [{"type": "text", "data": {"text": f"消息{message_id}"}}],
        }
        if group_id is not None:
            event["group_id"] = group_id
        return event

    app.inbound_backlog.arrived(message(1, 12345, 777))
    app.inbound_backlog.arrived(message(2, 99999, 777))
    app.inbound_backlog.arrived(message(3, 12345, None))

    context = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id               = 12345,
        group_id              = 777,
        manifest_capabilities = frozenset({"inbound_backlog"}),
    )
    backlog = context.capabilities.inbound_backlog
    assert backlog is not None
    # 只看得到这位发送者在这个群里排着的消息：别人的、他在私聊里的都不在。
    assert [item["message_id"] for item in backlog.waiting()] == [1]

    # 没在 Manifest 里声明的插件拿不到这项能力。
    undeclared = plugin_context_for(app, "xiaoqing_chat", user_id=12345, group_id=777)
    assert undeclared.capabilities.inbound_backlog is None
