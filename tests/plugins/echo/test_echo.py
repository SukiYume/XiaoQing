"""Echo 插件的命令、长度和身份边界测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.constants import MAX_MESSAGE_TEXT_LENGTH
from plugins.echo import main as echo


@pytest.fixture
def context() -> SimpleNamespace:
    return SimpleNamespace(request_id="echo-test", secrets={})


@pytest.fixture
def event() -> dict[str, Any]:
    return {"user_id": 12345, "message": "test", "message_type": "private"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        ("echo", "hello world", "hello world"),
        ("回显", "测试文本", "测试文本"),
        ("echo", "  hello world  ", "hello world"),
        ("echo", "第一行\n\t第二行", "第一行\n\t第二行"),
        ("echo", "!@#$%^&*()_+-=[]{}|;':\",./<>?", "!@#$%^&*()_+-=[]{}|;':\",./<>?"),
    ],
)
async def test_echo_returns_cleaned_text(
    context: SimpleNamespace,
    event: dict[str, Any],
    command: str,
    args: str,
    expected: str,
) -> None:
    assert await echo.handle(command, args, event, context) == echo.segments(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "   ", "\n\t"])
async def test_empty_echo_displays_help(
    context: SimpleNamespace,
    event: dict[str, Any],
    args: str,
) -> None:
    assert await echo.handle("echo", args, event, context) == echo.segments(echo.HELP_TEXT)


@pytest.mark.asyncio
async def test_echo_enforces_message_length(
    context: SimpleNamespace,
    event: dict[str, Any],
) -> None:
    accepted = "x" * MAX_MESSAGE_TEXT_LENGTH
    assert await echo.handle("echo", accepted, event, context) == echo.segments(accepted)
    rejected = await echo.handle("echo", accepted + "x", event, context)
    assert str(MAX_MESSAGE_TEXT_LENGTH) in str(rejected)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["before\x00after", "before\x7fafter", "before\x85after"])
async def test_echo_rejects_non_display_controls(
    context: SimpleNamespace,
    event: dict[str, Any],
    value: str,
) -> None:
    result = await echo.handle("echo", value, event, context)
    assert "控制字符" in str(result)
    assert value not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["hello", "你好"])
async def test_hello_uses_validated_user_id(
    context: SimpleNamespace,
    event: dict[str, Any],
    command: str,
) -> None:
    assert await echo.handle(command, "", event, context) == echo.segments("你好，12345！👋")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_id",
    [None, True, 0, -1, 2**63, "", "abc", "0", "1" * 20, {"secret": "value"}],
)
async def test_hello_does_not_stringify_invalid_user_ids(
    context: SimpleNamespace,
    event: dict[str, Any],
    user_id: object,
) -> None:
    event["user_id"] = user_id
    result = await echo.handle("hello", "", event, context)
    assert result == echo.segments("你好，未知用户！👋")
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_hello_normalizes_decimal_string_user_id(
    context: SimpleNamespace,
    event: dict[str, Any],
) -> None:
    event["user_id"] = "00123"
    assert await echo.handle("hello", "", event, context) == echo.segments("你好，123！👋")


@pytest.mark.asyncio
async def test_hello_rejects_extra_arguments(
    context: SimpleNamespace,
    event: dict[str, Any],
) -> None:
    assert await echo.handle("hello", "extra", event, context) == echo.segments("用法：/hello")


@pytest.mark.asyncio
async def test_unknown_command_does_not_echo_command_text(
    context: SimpleNamespace,
    event: dict[str, Any],
) -> None:
    result = await echo.handle("sensitive-command", "", event, context)
    assert result == echo.segments("未知命令；请使用 /echo 或 /hello")
    assert "sensitive-command" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "args", "event_value"),
    [
        (None, "text", {}),
        ("echo", None, {}),
        ("hello", "", None),
    ],
)
async def test_invalid_runtime_types_use_public_error_boundary(
    context: SimpleNamespace,
    command: object,
    args: object,
    event_value: object,
) -> None:
    result = await echo.handle(command, args, event_value, context)  # type: ignore[arg-type]
    assert "XQ-PLUGIN-UNEXPECTED" in str(result)
