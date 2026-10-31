"""core/exceptions.py 的异常层级与消息格式。"""

from pathlib import Path

import pytest

from core.exceptions import (
    ConfigError,
    ConfigLoadError,
    PluginError,
    PluginLifecycleFatalError,
    PluginLoadError,
    XiaoQingError,
)


@pytest.mark.unit
def test_plugin_error_carries_plugin_name_and_cause():
    cause = ValueError("Original error")
    exc = PluginError("test_plugin", "Something went wrong", cause=cause)
    assert isinstance(exc, XiaoQingError)
    assert exc.plugin_name == "test_plugin"
    assert exc.cause is cause
    assert str(exc) == "[test_plugin] Something went wrong"


@pytest.mark.unit
def test_plugin_load_error_is_plugin_error():
    exc = PluginLoadError("my_plugin", "Failed to load module")
    assert isinstance(exc, PluginError)
    assert "my_plugin" in str(exc)


@pytest.mark.unit
def test_plugin_lifecycle_fatal_error_keeps_original():
    original = KeyboardInterrupt("stop")
    exc      = PluginLifecycleFatalError("demo", original)
    assert isinstance(exc, PluginError)
    assert exc.original is original
    assert "KeyboardInterrupt: stop" in str(exc)


@pytest.mark.unit
def test_config_load_error_message_or_path():
    plain = ConfigLoadError("Failed to load config.json")
    assert isinstance(plain, ConfigError)
    assert plain.path is None and plain.original is None
    assert str(plain) == "Failed to load config.json"

    original = OSError("denied")
    wrapped  = ConfigLoadError("config/config.json", original)
    assert wrapped.path == Path("config/config.json")
    assert wrapped.original is original
    assert "denied" in str(wrapped)
