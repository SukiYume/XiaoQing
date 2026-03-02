"""
Tests for core/exceptions.py - Custom exception classes
"""

import pytest

from core.exceptions import (
    XiaoQingError,
    PluginError,
    PluginLoadError,
    PluginExecutionError,
    PluginTimeoutError,
    CommandError,
    CommandNotFoundError,
    CommandPermissionError,
    CommandArgumentError,
    ConfigError,
    ConfigLoadError,
    ConfigValidationError,
    SessionError,
    SessionNotFoundError,
    SessionExpiredError,
    CommunicationError,
    OneBotError,
    AuthenticationError,
)


# ============================================================
# Base Exception Tests
# ============================================================

@pytest.mark.unit
def test_xiaoqing_error_base():
    """Test XiaoQingError is a proper exception"""
    exc = XiaoQingError("Base error")
    assert isinstance(exc, Exception)
    assert str(exc) == "Base error"


@pytest.mark.unit
def test_xiaoqing_error_can_be_raised():
    """Test XiaoQingError can be raised and caught"""
    with pytest.raises(XiaoQingError) as exc_info:
        raise XiaoQingError("Test error")
    assert str(exc_info.value) == "Test error"


# ============================================================
# Plugin Exceptions Tests
# ============================================================

@pytest.mark.unit
def test_plugin_error_base():
    """Test PluginError creation"""
    exc = PluginError("test_plugin", "Something went wrong")
    assert isinstance(exc, XiaoQingError)
    assert exc.plugin_name == "test_plugin"
    assert "test_plugin" in str(exc)
    assert "Something went wrong" in str(exc)


@pytest.mark.unit
def test_plugin_error_with_cause():
    """Test PluginError with underlying cause"""
    cause = ValueError("Original error")
    exc = PluginError("test_plugin", "Wrapper message", cause=cause)

    assert exc.plugin_name == "test_plugin"
    assert exc.cause is cause
    assert isinstance(exc, XiaoQingError)


@pytest.mark.unit
def test_plugin_load_error():
    """Test PluginLoadError"""
    exc = PluginLoadError("my_plugin", "Failed to load module")

    assert isinstance(exc, PluginError)
    assert isinstance(exc, XiaoQingError)
    assert exc.plugin_name == "my_plugin"


@pytest.mark.unit
def test_plugin_execution_error():
    """Test PluginExecutionError"""
    exc = PluginExecutionError("my_plugin", "Handler raised exception")

    assert isinstance(exc, PluginError)
    assert isinstance(exc, XiaoQingError)
    assert exc.plugin_name == "my_plugin"


@pytest.mark.unit
def test_plugin_timeout_error():
    """Test PluginTimeoutError"""
    exc = PluginTimeoutError("my_plugin", "Execution timed out")

    assert isinstance(exc, PluginError)
    assert isinstance(exc, XiaoQingError)
    assert exc.plugin_name == "my_plugin"


@pytest.mark.unit
def test_plugin_exception_hierarchy():
    """Test all plugin exceptions inherit correctly"""
    exc1 = PluginLoadError("p", "msg")
    exc2 = PluginExecutionError("p", "msg")
    exc3 = PluginTimeoutError("p", "msg")

    for exc in [exc1, exc2, exc3]:
        assert isinstance(exc, PluginError)
        assert isinstance(exc, XiaoQingError)


# ============================================================
# Command Exceptions Tests
# ============================================================

@pytest.mark.unit
def test_command_error_base():
    """Test CommandError"""
    exc = CommandError("Command failed")
    assert isinstance(exc, XiaoQingError)
    assert "Command failed" in str(exc)


@pytest.mark.unit
def test_command_not_found_error():
    """Test CommandNotFoundError"""
    exc = CommandNotFoundError("nonexistent_cmd")

    assert isinstance(exc, CommandError)
    assert exc.command == "nonexistent_cmd"
    assert "nonexistent_cmd" in str(exc)


@pytest.mark.unit
def test_command_permission_error():
    """Test CommandPermissionError"""
    exc = CommandPermissionError("admin_cmd", 12345)

    assert isinstance(exc, CommandError)
    assert exc.command == "admin_cmd"
    assert exc.user_id == 12345
    assert "admin_cmd" in str(exc)
    assert "12345" in str(exc)


@pytest.mark.unit
def test_command_argument_error():
    """Test CommandArgumentError"""
    exc = CommandArgumentError("search", "Missing query parameter")

    assert isinstance(exc, CommandError)
    assert exc.command == "search"
    assert "search" in str(exc)
    assert "Missing query parameter" in str(exc)


# ============================================================
# Config Exceptions Tests
# ============================================================

@pytest.mark.unit
def test_config_error_base():
    """Test ConfigError"""
    exc = ConfigError("Configuration error")
    assert isinstance(exc, XiaoQingError)


@pytest.mark.unit
def test_config_load_error():
    """Test ConfigLoadError"""
    exc = ConfigLoadError("Failed to load config.json")

    assert isinstance(exc, ConfigError)
    assert isinstance(exc, XiaoQingError)


@pytest.mark.unit
def test_config_validation_error():
    """Test ConfigValidationError"""
    exc = ConfigValidationError("Invalid bot_name")

    assert isinstance(exc, ConfigError)
    assert isinstance(exc, XiaoQingError)


# ============================================================
# Session Exceptions Tests
# ============================================================

@pytest.mark.unit
def test_session_error_base():
    """Test SessionError"""
    exc = SessionError("Session error")
    assert isinstance(exc, XiaoQingError)


@pytest.mark.unit
def test_session_not_found_error_private():
    """Test SessionNotFoundError for private session"""
    exc = SessionNotFoundError(12345)

    assert isinstance(exc, SessionError)
    assert exc.user_id == 12345
    assert exc.group_id is None
    assert "12345" in str(exc)
    assert "private" in str(exc)


@pytest.mark.unit
def test_session_not_found_error_group():
    """Test SessionNotFoundError for group session"""
    exc = SessionNotFoundError(12345, 67890)

    assert isinstance(exc, SessionError)
    assert exc.user_id == 12345
    assert exc.group_id == 67890
    assert "12345" in str(exc)
    assert "67890" in str(exc)


@pytest.mark.unit
def test_session_expired_error():
    """Test SessionExpiredError"""
    exc = SessionExpiredError("Session has expired")

    assert isinstance(exc, SessionError)
    assert isinstance(exc, XiaoQingError)


# ============================================================
# Communication Exceptions Tests
# ============================================================

@pytest.mark.unit
def test_communication_error_base():
    """Test CommunicationError"""
    exc = CommunicationError("Communication failed")

    assert isinstance(exc, XiaoQingError)


@pytest.mark.unit
def test_onebot_error():
    """Test OneBotError"""
    exc = OneBotError("OneBot protocol error")

    assert isinstance(exc, CommunicationError)
    assert isinstance(exc, XiaoQingError)


@pytest.mark.unit
def test_authentication_error():
    """Test AuthenticationError"""
    exc = AuthenticationError("Invalid token")

    assert isinstance(exc, CommunicationError)
    assert isinstance(exc, XiaoQingError)


# ============================================================
# Exception Catching Tests
# ============================================================

@pytest.mark.unit
def test_catch_plugin_error_by_base():
    """Test catching PluginError catches specific types"""
    with pytest.raises(PluginError):
        raise PluginLoadError("p", "msg")

    with pytest.raises(PluginError):
        raise PluginExecutionError("p", "msg")

    with pytest.raises(PluginError):
        raise PluginTimeoutError("p", "msg")


@pytest.mark.unit
def test_catch_command_error_by_base():
    """Test catching CommandError catches specific types"""
    with pytest.raises(CommandError):
        raise CommandNotFoundError("cmd")

    with pytest.raises(CommandError):
        raise CommandPermissionError("cmd", 123)

    with pytest.raises(CommandError):
        raise CommandArgumentError("cmd", "msg")


@pytest.mark.unit
def test_catch_config_error_by_base():
    """Test catching ConfigError catches specific types"""
    with pytest.raises(ConfigError):
        raise ConfigLoadError("msg")

    with pytest.raises(ConfigError):
        raise ConfigValidationError("msg")


@pytest.mark.unit
def test_catch_session_error_by_base():
    """Test catching SessionError catches specific types"""
    with pytest.raises(SessionError):
        raise SessionNotFoundError(123)

    with pytest.raises(SessionError):
        raise SessionExpiredError("msg")


@pytest.mark.unit
def test_catch_communication_error_by_base():
    """Test catching CommunicationError catches specific types"""
    with pytest.raises(CommunicationError):
        raise OneBotError("msg")

    with pytest.raises(CommunicationError):
        raise AuthenticationError("msg")


@pytest.mark.unit
def test_catch_xiaoqing_error_catches_all():
    """Test catching XiaoQingError catches all custom exceptions"""
    exceptions = [
        PluginLoadError("p", "msg"),
        PluginExecutionError("p", "msg"),
        PluginTimeoutError("p", "msg"),
        CommandNotFoundError("cmd"),
        CommandPermissionError("cmd", 123),
        CommandArgumentError("cmd", "msg"),
        ConfigLoadError("msg"),
        ConfigValidationError("msg"),
        SessionNotFoundError(123),
        SessionExpiredError("msg"),
        OneBotError("msg"),
        AuthenticationError("msg"),
    ]

    for exc in exceptions:
        with pytest.raises(XiaoQingError):
            raise exc


# ============================================================
# Exception __all__ Tests
# ============================================================

@pytest.mark.unit
def test_exceptions_all_exports():
    """Test that __all__ contains expected exceptions"""
    from core.exceptions import __all__ as exports

    expected = [
        "XiaoQingError",
        "PluginError",
        "PluginLoadError",
        "PluginExecutionError",
        "PluginTimeoutError",
        "CommandError",
        "CommandNotFoundError",
        "CommandPermissionError",
        "CommandArgumentError",
        "ConfigError",
        "ConfigLoadError",
        "ConfigValidationError",
        "SessionError",
        "SessionNotFoundError",
        "SessionExpiredError",
        "CommunicationError",
        "OneBotError",
        "AuthenticationError",
    ]

    for name in expected:
        assert name in exports


# ============================================================
# Exception Message Format Tests
# ============================================================

@pytest.mark.unit
def test_plugin_error_message_format():
    """Test PluginError message format"""
    exc = PluginError("my_plugin", "Error message")
    message = str(exc)
    assert message.startswith("[my_plugin]")
    assert "Error message" in message


@pytest.mark.unit
def test_command_permission_error_message_format():
    """Test CommandPermissionError message format"""
    exc = CommandPermissionError("admin_only", 999)
    message = str(exc)
    assert "admin_only" in message
    assert "999" in message


@pytest.mark.unit
def test_session_not_found_private_message_format():
    """Test SessionNotFoundError message for private"""
    exc = SessionNotFoundError(12345)
    message = str(exc)
    assert "12345" in message
    assert "private" in message


@pytest.mark.unit
def test_session_not_found_group_message_format():
    """Test SessionNotFoundError message for group"""
    exc = SessionNotFoundError(12345, 67890)
    message = str(exc)
    assert "12345" in message
    assert "67890" in message


# ============================================================
# Exception Re-raising Tests
# ============================================================

@pytest.mark.unit
def test_exception_from_cause():
    """Test exception can be created from another exception"""
    original = ValueError("Original error")
    exc = PluginError("plugin", "Wrapper", cause=original)

    assert exc.cause is original
    assert str(exc.cause) == "Original error"


@pytest.mark.unit
def test_exception_chaining():
    """Test exception chaining works"""
    try:
        try:
            raise ValueError("Inner error")
        except ValueError as e:
            raise PluginExecutionError("plugin", "Outer") from e
    except PluginExecutionError as e:
        assert e.__cause__ is not None
        assert isinstance(e.__cause__, ValueError)
