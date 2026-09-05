"""共享常量的边界和相互关系测试。"""

import re

import pytest

from core import constants


@pytest.mark.unit
def test_runtime_defaults_are_bounded() -> None:
    """默认并发、超时和缓存预算必须是有限的正数。"""

    positive_defaults = (
        constants.DEFAULT_SESSION_TIMEOUT_SEC,
        constants.DEFAULT_MAX_CONCURRENCY,
        constants.DEFAULT_INBOUND_WS_MAX_WORKERS,
        constants.DEFAULT_INBOUND_WS_QUEUE_SIZE,
        constants.DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS,
        constants.DEFAULT_LOG_TRUNCATE_LEN,
        constants.DEFAULT_HTTP_TIMEOUT_SECONDS,
        constants.DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS,
        constants.DEFAULT_ONEBOT_HTTP_TIMEOUT_SECONDS,
        constants.DEFAULT_ONEBOT_WS_ACTION_TIMEOUT_SECONDS,
        constants.INBOUND_EVENT_DEDUP_TTL_SECONDS,
        constants.MAX_INBOUND_EVENT_DEDUP_KEYS,
    )
    assert all(value > 0 for value in positive_defaults)
    assert constants.DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS <= constants.DEFAULT_HTTP_TIMEOUT_SECONDS


@pytest.mark.unit
def test_time_units_are_consistent() -> None:
    """共享换算值应保持分钟、小时和天之间的一致关系。"""

    assert constants.SECONDS_PER_MINUTE == 60
    assert constants.SECONDS_PER_HOUR == 60 * constants.SECONDS_PER_MINUTE
    assert constants.SECONDS_PER_DAY == 24 * constants.SECONDS_PER_HOUR


@pytest.mark.unit
def test_session_exit_commands_are_complete_and_immutable() -> None:
    """会话退出词同时覆盖中文、英文和短命令，并保持不可变。"""

    assert constants.EXIT_COMMANDS_SET == frozenset({"退出", "取消", "exit", "quit", "q"})
    assert "not-a-command" not in constants.EXIT_COMMANDS_SET


@pytest.mark.unit
def test_default_bot_name_responses_are_available() -> None:
    """随机昵称回应池不能为空，且保留既有中文回应。"""

    assert constants.DEFAULT_BOT_NAME_RESPONSES_LIST
    assert {"叫我干嘛", "嗯？", "在的~", "有事吗？"} <= set(
        constants.DEFAULT_BOT_NAME_RESPONSES_LIST
    )


@pytest.mark.unit
def test_plugin_init_timeout_is_finite() -> None:
    """插件初始化必须有正的有限截止时间。"""

    assert constants.PLUGIN_INIT_TIMEOUT_SECONDS > 0


@pytest.mark.unit
def test_plugin_name_pattern_accepts_only_namespace_safe_names() -> None:
    """插件名只能使用可安全映射到 Python 命名空间的 ASCII 字符。"""

    pattern     = re.compile(constants.VALID_PLUGIN_NAME_PATTERN)
    valid_names = (
        "test",
        "test_plugin",
        "TestPlugin",
        "test123",
        "plugin_123",
        "_private",
        "Plugin",
    )
    invalid_names = (
        "test-plugin",
        "test.plugin",
        "test plugin",
        "test@plugin",
        "测试",
        "plugin!",
    )

    assert all(pattern.fullmatch(name) for name in valid_names)
    assert not any(pattern.fullmatch(name) for name in invalid_names)


@pytest.mark.unit
def test_message_limits_are_ordered() -> None:
    """短文本阈值必须小于平台单条消息上限。"""

    assert 0 < constants.MAX_SHORT_TEXT_LENGTH < constants.MAX_MESSAGE_TEXT_LENGTH
    assert constants.MESSAGE_SPLIT_DELAY >= 0
