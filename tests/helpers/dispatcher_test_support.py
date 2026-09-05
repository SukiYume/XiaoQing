"""消息分发器测试共享 fixture、导入和私有 helper。"""

import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from core.dispatcher import (
    Dispatcher,
    MessageContext,
    MessageParser,
)
from core.plugin_execution import PluginExecutionGate
from core.router import CommandRouter, CommandSpec
from tests.helpers.asyncio_tools import BlockingConcurrencyProbe


@pytest.fixture
def mock_config_provider():
    """模拟配置提供者"""
    mock        = MagicMock()
    mock.config = {
        "bot_name": "小青",
        "command_prefixes": ["/"],
        "require_bot_name_in_group": True,
        "plugins": {
            "smalltalk_provider": "smalltalk",
        },
    }
    return mock


@pytest.fixture
def mock_router():
    """模拟路由器"""
    mock = MagicMock(spec=CommandRouter)
    mock.resolve = Mock(return_value=None)  # 默认未匹配任何命令，防止 MagicMock 被误解包
    return mock


@pytest.fixture
def mock_admin_check():
    """模拟管理员检查"""
    mock = MagicMock()
    mock.is_admin = Mock(return_value=True)
    return mock


@pytest.fixture
def mock_context_factory():
    """模拟上下文工厂"""

    def _factory(*args, **kwargs):
        return MagicMock()

    return _factory


@pytest.fixture
def mock_session_manager():
    """模拟会话管理器"""
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)

    async def peek(user_id, group_id):
        return await mock.get(user_id, group_id)

    mock.peek = AsyncMock(side_effect=peek)
    mock.exists = AsyncMock(return_value=False)

    async def update(user_id, group_id, callback):
        session = await mock.get(user_id, group_id)
        if session is None:
            return None
        result = callback(session)
        if inspect.isawaitable(result):
            return await result
        return result

    mock.update = AsyncMock(side_effect=update)
    return mock


@pytest.fixture
def mock_metrics():
    """模拟指标收集器"""
    mock                         = MagicMock()
    mock.record_plugin_execution = AsyncMock()
    return mock


@pytest.fixture
def sample_message_context() -> MessageContext:
    """创建示例消息上下文"""
    return MessageContext(
        request_id         = "test_001",
        text               = "/echo hello",
        clean_text         = "echo hello",
        user_id            = 12345,
        group_id           = 67890,
        is_private         = False,
        has_bot_name       = False,
        has_prefix         = True,
        has_command_prefix = True,
        is_only_bot_name   = False,
        is_at_me           = False,
        is_url_only        = False,
        event              = {},
    )


@pytest.fixture
def dispatcher(
    mock_router: MagicMock,
    mock_config_provider: MagicMock,
    mock_plugin_registry: MagicMock,
    mock_admin_check: MagicMock,
    mock_context_factory: MagicMock,
    mock_session_manager: MagicMock,
    mock_metrics: MagicMock,
):
    """创建 Dispatcher 实例"""
    semaphore = asyncio.Semaphore(10)
    return Dispatcher(
        router          = mock_router,
        config_provider = mock_config_provider,
        plugin_registry = mock_plugin_registry,
        admin_check     = mock_admin_check,
        build_context   = mock_context_factory,
        semaphore       = semaphore,
        session_manager = mock_session_manager,
        metrics         = mock_metrics,
    )


__all__ = (
    "AsyncMock",
    "BlockingConcurrencyProbe",
    "CommandRouter",
    "CommandSpec",
    "Dispatcher",
    "MagicMock",
    "MessageContext",
    "MessageParser",
    "Mock",
    "PluginExecutionGate",
    "SimpleNamespace",
    "asyncio",
    "dispatcher",
    "inspect",
    "logging",
    "mock_admin_check",
    "mock_config_provider",
    "mock_context_factory",
    "mock_metrics",
    "mock_router",
    "mock_session_manager",
    "pytest",
    "sample_message_context",
)
