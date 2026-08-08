"""xiaoqing_chat 测试共享 fixture、导入和私有 helper。"""

import asyncio
import json
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.ai import AIModelInfo
from core.interfaces import PluginCapabilities, PluginPrincipal
from plugins.xiaoqing_chat import main as xiaoqing_chat
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.handler_context import HandlerContext, handle_errors
from plugins.xiaoqing_chat.message_parts import message_parts_to_legacy
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.payloads import text_reply_draft as _reply_draft
from tests.helpers.settings_snapshot import with_settings_reader

_MISSING_TEST_CONFIG = object()


PROJECT_ROOT = REPOSITORY_ROOT


def _explicit_test_attribute(target, name: str):
    values = vars(target)
    if name in values:
        return values[name]
    child = values.get("_mock_children", {}).get(name)
    return child if child is not None else _MISSING_TEST_CONFIG


def _fill_test_config_defaults(target, defaults) -> None:
    """让轻量测试替身遵守完整的生产配置契约。"""

    if type(target) is type(defaults):
        return
    for name in type(defaults).model_fields:
        default_value = getattr(defaults, name)
        current = _explicit_test_attribute(target, name)
        if current is _MISSING_TEST_CONFIG:
            setattr(target, name, deepcopy(default_value))
            continue
        if hasattr(type(default_value), "model_fields") and not hasattr(
            type(current), "model_fields"
        ):
            _fill_test_config_defaults(current, default_value)


def _complete_test_runtime_config(runtime):
    cfg = runtime.cfg
    if not isinstance(cfg, XiaoQingChatConfig):
        _fill_test_config_defaults(cfg, XiaoQingChatConfig())
    return runtime


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
    _complete_test_runtime_config(runtime)
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


def _set_context_principal(
    context,
    event: dict[str, Any],
    *,
    group_role: str = "member",
    is_bot_admin: bool = False,
) -> None:
    group_id = event.get("group_id")
    context.principal = PluginPrincipal(
        kind="user",
        user_id=event.get("user_id"),
        group_id=group_id,
        is_bot_admin=is_bot_admin,
        is_private=group_id in (None, ""),
        group_role=group_role if group_id not in (None, "") else "unknown",
    )
    context.capabilities = PluginCapabilities(
        is_bot_admin=is_bot_admin,
        ai=_provider_test_ai(),
    )
    context.config = {
        **dict(getattr(context, "config", {}) or {}),
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
        },
    }


def _provider_test_ai() -> SimpleNamespace:
    models = (
        AIModelInfo("deepseek-flash", "deepseek", "deepseek-chat", ("text",)),
        AIModelInfo("glm-5.2", "zhipu", "glm-5.2", ("text",)),
    )
    return SimpleNamespace(
        list_models=lambda route, **kwargs: models,
        complete=AsyncMock(),
    )


async def _ack_reply_delivery(result, *, delivered: bool = True):
    receipt = getattr(result, "delivery_receipt", None)
    assert receipt is not None
    await receipt.record(delivered)
    return receipt


def _build_xiaoqing_catalog():
    """从生产 manifest 构造测试使用的同一份 Core 目录快照。"""

    from core.models import PluginManifest
    from core.router import build_command_catalog_node

    manifest_path = PROJECT_ROOT / "plugins" / "xiaoqing_chat" / "plugin.json"
    manifest = PluginManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    return build_command_catalog_node(
        manifest.name,
        manifest.commands[0].model_dump(),
        root=True,
    )


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
    context.get_command_catalog = Mock(return_value=(_build_xiaoqing_catalog(),))
    context.list_plugins = Mock(return_value=["xiaoqing_chat"])
    context.current_user_id = 12345
    context.current_group_id = 67890
    context.request_id = "test-request-123"
    context.state = {}
    context.logger = MagicMock()
    context.session_manager = None
    context.config_manager = MagicMock()
    return with_settings_reader(context)


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


__all__ = (
    "AIModelInfo",
    "Any",
    "AsyncMock",
    "HandlerContext",
    "MagicMock",
    "Mock",
    "PROJECT_ROOT",
    "Path",
    "PluginCapabilities",
    "PluginPrincipal",
    "SimpleNamespace",
    "XiaoQingChatConfig",
    "_MISSING_TEST_CONFIG",
    "_ack_reply_delivery",
    "_build_xiaoqing_catalog",
    "_complete_test_runtime_config",
    "_explicit_test_attribute",
    "_fill_test_config_defaults",
    "_make_hctx",
    "_provider_test_ai",
    "_reply_draft",
    "_set_context_principal",
    "asyncio",
    "cast",
    "deepcopy",
    "handle_errors",
    "json",
    "message_parts_to_legacy",
    "mock_context",
    "patch",
    "pytest",
    "sample_group_event",
    "tempfile",
    "time",
    "xiaoqing_chat",
)
