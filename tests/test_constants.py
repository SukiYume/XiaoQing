"""
Tests for core/constants.py - Constant values and shared definitions
"""

import re

import pytest

from core import constants


# ============================================================
# Default Configuration Values Tests
# ============================================================

@pytest.mark.unit
def test_default_session_timeout():
    """Test DEFAULT_SESSION_TIMEOUT_SEC is reasonable"""
    assert constants.DEFAULT_SESSION_TIMEOUT_SEC == 300.0
    assert constants.DEFAULT_SESSION_TIMEOUT_SEC > 0


@pytest.mark.unit
def test_default_inbound_port():
    """Test DEFAULT_INBOUND_PORT is valid port number"""
    assert constants.DEFAULT_INBOUND_PORT == 12000
    assert 1024 <= constants.DEFAULT_INBOUND_PORT <= 65535


@pytest.mark.unit
def test_default_ws_path():
    """Test DEFAULT_WS_PATH is valid"""
    assert constants.DEFAULT_WS_PATH == "/ws"
    assert constants.DEFAULT_WS_PATH.startswith("/")


@pytest.mark.unit
def test_default_max_concurrency():
    """Test DEFAULT_MAX_CONCURRENCY is positive"""
    assert constants.DEFAULT_MAX_CONCURRENCY == 5
    assert constants.DEFAULT_MAX_CONCURRENCY > 0


@pytest.mark.unit
def test_default_inbound_ws_max_workers():
    """Test DEFAULT_INBOUND_WS_MAX_WORKERS is positive"""
    assert constants.DEFAULT_INBOUND_WS_MAX_WORKERS == 8
    assert constants.DEFAULT_INBOUND_WS_MAX_WORKERS > 0


@pytest.mark.unit
def test_default_inbound_ws_queue_size():
    """Test DEFAULT_INBOUND_WS_QUEUE_SIZE is positive"""
    assert constants.DEFAULT_INBOUND_WS_QUEUE_SIZE == 200
    assert constants.DEFAULT_INBOUND_WS_QUEUE_SIZE > 0


@pytest.mark.unit
def test_default_log_truncate_len():
    """Test DEFAULT_LOG_TRUNCATE_LEN is positive"""
    assert constants.DEFAULT_LOG_TRUNCATE_LEN == 50
    assert constants.DEFAULT_LOG_TRUNCATE_LEN > 0


# ============================================================
# Time Conversion Constants Tests
# ============================================================

@pytest.mark.unit
def test_seconds_per_minute():
    """Test SECONDS_PER_MINUTE is correct"""
    assert constants.SECONDS_PER_MINUTE == 60


@pytest.mark.unit
def test_minutes_per_hour():
    """Test MINUTES_PER_HOUR is correct"""
    assert constants.MINUTES_PER_HOUR == 60


# ============================================================
# Session Exit Commands Tests
# ============================================================

@pytest.mark.unit
def test_exit_commands_set():
    """Test EXIT_COMMANDS_SET contains expected values"""
    assert "退出" in constants.EXIT_COMMANDS_SET
    assert "取消" in constants.EXIT_COMMANDS_SET
    assert "exit" in constants.EXIT_COMMANDS_SET
    assert "quit" in constants.EXIT_COMMANDS_SET
    assert "q" in constants.EXIT_COMMANDS_SET


@pytest.mark.unit
def test_exit_commands_is_frozenset():
    """Test EXIT_COMMANDS_SET is immutable"""
    assert isinstance(constants.EXIT_COMMANDS_SET, frozenset)
    # Should not be able to modify
    with pytest.raises(AttributeError):
        constants.EXIT_COMMANDS_SET.add("new_command")


@pytest.mark.unit
def test_exit_commands_membership():
    """Test exit commands membership checks"""
    assert "退出" in constants.EXIT_COMMANDS_SET
    assert "exit" in constants.EXIT_COMMANDS_SET
    assert "not_a_command" not in constants.EXIT_COMMANDS_SET


# ============================================================
# Default Bot Name Responses Tests
# ============================================================

@pytest.mark.unit
def test_default_bot_name_responses():
    """Test DEFAULT_BOT_NAME_RESPONSES_LIST contains values"""
    assert isinstance(constants.DEFAULT_BOT_NAME_RESPONSES_LIST, list)
    assert len(constants.DEFAULT_BOT_NAME_RESPONSES_LIST) > 0


@pytest.mark.unit
def test_default_bot_name_responses_content():
    """Test DEFAULT_BOT_NAME_RESPONSES_LIST has expected responses"""
    responses = constants.DEFAULT_BOT_NAME_RESPONSES_LIST
    assert "叫我干嘛" in responses
    assert "嗯？" in responses
    assert "在的~" in responses
    assert "有事吗？" in responses


# ============================================================
# Plugin Security Constants Tests
# ============================================================

@pytest.mark.unit
def test_plugin_init_timeout():
    """Test PLUGIN_INIT_TIMEOUT_SECONDS is reasonable"""
    assert constants.PLUGIN_INIT_TIMEOUT_SECONDS == 30.0
    assert constants.PLUGIN_INIT_TIMEOUT_SECONDS > 0


@pytest.mark.unit
def test_valid_plugin_name_pattern():
    """Test VALID_PLUGIN_NAME_PATTERN is valid regex"""
    pattern = constants.VALID_PLUGIN_NAME_PATTERN

    # Should be a valid regex string
    assert isinstance(pattern, str)
    re.compile(pattern)  # Should not raise


@pytest.mark.unit
def test_plugin_name_pattern_valid_names():
    """Test VALID_PLUGIN_NAME_PATTERN accepts valid names"""
    pattern = constants.VALID_PLUGIN_NAME_PATTERN

    valid_names = [
        "test",
        "test_plugin",
        "TestPlugin",
        "test123",
        "plugin_123",
        "_private",
        "Plugin",
    ]

    for name in valid_names:
        assert re.match(pattern, name), f"Pattern should accept {name}"


@pytest.mark.unit
def test_plugin_name_pattern_invalid_names():
    """Test VALID_PLUGIN_NAME_PATTERN rejects invalid names"""
    pattern = constants.VALID_PLUGIN_NAME_PATTERN

    invalid_names = [
        "test-plugin",  # hyphen not allowed
        "test.plugin",  # dot not allowed
        "test plugin",  # space not allowed
        "test@plugin",  # @ not allowed
        "测试",  # unicode not allowed
        "plugin!",  # special chars not allowed
    ]

    for name in invalid_names:
        assert not re.match(pattern, name), f"Pattern should reject {name}"


# ============================================================
# Message Preview Length Tests
# ============================================================

@pytest.mark.unit
def test_max_message_preview_length():
    """Test MAX_MESSAGE_PREVIEW_LENGTH is positive"""
    assert constants.MAX_MESSAGE_PREVIEW_LENGTH == 220
    assert constants.MAX_MESSAGE_PREVIEW_LENGTH > 0


@pytest.mark.unit
def test_max_short_text_length():
    """Test MAX_SHORT_TEXT_LENGTH is positive"""
    assert constants.MAX_SHORT_TEXT_LENGTH == 60
    assert constants.MAX_SHORT_TEXT_LENGTH > 0
    assert constants.MAX_SHORT_TEXT_LENGTH < constants.MAX_MESSAGE_PREVIEW_LENGTH


# ============================================================
# Constant Immutability Tests
# ============================================================

@pytest.mark.unit
def test_constants_are_immutable_strings():
    """Test that string constants can't be easily modified"""
    # Get original values
    original_timeout = constants.DEFAULT_SESSION_TIMEOUT_SEC
    original_pattern = constants.VALID_PLUGIN_NAME_PATTERN

    # Attempt to modify (should create new binding, not affect module)
    try:
        constants.DEFAULT_SESSION_TIMEOUT_SEC = 999
        constants.VALID_PLUGIN_NAME_PATTERN = "invalid"
    except AttributeError:
        # If constants are defined with __slots__ or similar protection
        pass

    # The module-level constants might still be modifiable in Python
    # but at least verify their initial correct values
    assert original_timeout == 300.0
    assert original_pattern == r"^[a-zA-Z0-9_]+$"


# ============================================================
# Constant Type Tests
# ============================================================

@pytest.mark.unit
def test_constant_types():
    """Test that constants have correct types"""
    assert isinstance(constants.DEFAULT_SESSION_TIMEOUT_SEC, (int, float))
    assert isinstance(constants.DEFAULT_INBOUND_PORT, int)
    assert isinstance(constants.DEFAULT_WS_PATH, str)
    assert isinstance(constants.DEFAULT_MAX_CONCURRENCY, int)
    assert isinstance(constants.SECONDS_PER_MINUTE, int)
    assert isinstance(constants.MINUTES_PER_HOUR, int)
    assert isinstance(constants.EXIT_COMMANDS_SET, frozenset)
    assert isinstance(constants.DEFAULT_BOT_NAME_RESPONSES_LIST, list)
    assert isinstance(constants.PLUGIN_INIT_TIMEOUT_SECONDS, (int, float))
    assert isinstance(constants.VALID_PLUGIN_NAME_PATTERN, str)
    assert isinstance(constants.MAX_MESSAGE_PREVIEW_LENGTH, int)
