"""OneBot 鉴权、脱敏和事件摘要。"""

from __future__ import annotations

import pytest

import tests.helpers.onebot_test_support as _fixture_support
from core.onebot import (
    _extract_message_preview,
    _mask_sensitive_text,
    _onebot_action_succeeded,
    _summarize_event,
)
from tests.helpers.onebot_test_support import verify_bearer_token

bounded_transport_adapter = _fixture_support.bounded_transport_adapter


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"status": "ok", "retcode": 0}, True),
        ({"status": "ok", "retcode": False}, False),
        ({"status": "ok", "retcode": 0.0}, False),
        ({"status": "failed", "retcode": 0}, False),
        (None, False),
    ],
)
def test_onebot_action_success_requires_exact_integer_retcode(response, expected):
    assert _onebot_action_succeeded(response) is expected


@pytest.mark.parametrize(
    ("header", "configured", "expected"),
    [
        pytest.param("Bearer my_token", "my_token", True, id="valid"),
        pytest.param("Bearer anything", "", False, id="no-token-configured-empty"),
        pytest.param("Bearer anything", None, False, id="no-token-configured-none"),
        pytest.param("Bearer wrong_token", "my_token", False, id="wrong-token"),
        pytest.param("my_token", "my_token", False, id="missing-bearer-prefix"),
        pytest.param("short", "much_longer_token", False, id="length-mismatch"),
    ],
)
def test_verify_bearer_token(header, configured, expected):
    """未配置 token 时默认拒绝，避免鉴权失败开放。"""
    assert verify_bearer_token(header, configured) is expected


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        pytest.param("Authorization: Bearer secret_token_123", "secret_token_123", id="header"),
        pytest.param(
            "authorization:bearer   secret_token_123",
            "secret_token_123",
            id="header-lowercase-spaced",
        ),
        pytest.param(
            "AUTHORIZATION = Basic secret_token_123", "secret_token_123", id="header-basic"
        ),
        pytest.param("header Bearer secret_token_123", "secret_token_123", id="bare-bearer"),
        pytest.param(
            "authorization=secret_token_123", "secret_token_123", id="authorization-value"
        ),
        pytest.param("api_key=sk-1234567890", "sk-1234567890", id="api-key"),
    ],
)
def test_mask_credential_is_replaced_with_marker(text, secret):
    result = _mask_sensitive_text(text)
    assert secret not in result
    assert "********" in result


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        pytest.param("password=my_password", ("my_password",), id="password"),
        pytest.param("token=abc123 and password=xyz789", ("abc123", "xyz789"), id="multiple"),
        pytest.param("API_KEY=secret", ("secret",), id="case-insensitive"),
    ],
)
def test_mask_sensitive_values(text, secrets):
    result = _mask_sensitive_text(text)
    for secret in secrets:
        assert secret not in result


def test_mask_leaves_plain_text_unchanged():
    text = "hello world"
    assert _mask_sensitive_text(text) == text


class TestExtractMessagePreview:
    """_extract_message_preview 测试"""

    def test_empty_message(self):
        """测试空消息"""
        result = _extract_message_preview([])
        assert result == "(empty)"

    def test_text_only(self):
        """测试纯文本"""
        message = [{"type": "text", "data": {"text": "Hello world"}}]
        result  = _extract_message_preview(message)
        assert result == "Hello world"

    def test_text_with_image(self):
        """测试文本和图片"""
        message = [
            {"type": "text", "data": {"text": "Check this: "}},
            {"type": "image", "data": {"file": "test.png"}},
        ]
        result = _extract_message_preview(message)
        assert "Check this:" in result
        assert "[图片]" in result

    def test_text_with_emoji_image(self):
        """测试带 emoji 元数据的图片预览"""
        message = [
            {"type": "text", "data": {"text": "Mood: "}},
            {"type": "emoji", "data": {"file": "emoji.png"}},
        ]
        result = _extract_message_preview(message)
        assert "Mood:" in result
        assert "[表情包]" in result

    def test_at_mention(self):
        """测试 @ 提及"""
        message = [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " hello"}},
        ]
        result = _extract_message_preview(message)
        assert "[@12345]" in result

    def test_unknown_segment_type(self):
        """测试未知消息段类型"""
        message = [{"type": "unknown", "data": {}}]
        result  = _extract_message_preview(message)
        assert "[unknown]" in result

    def test_truncation(self):
        """测试截断"""
        long_text = "a" * 100
        message   = [{"type": "text", "data": {"text": long_text}}]
        result = _extract_message_preview(message, max_len=20)
        assert result.endswith("...")
        assert len(result) <= 25  # 20 + "..."

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ([{"type": "text", "data": {"text": 123}}], "123"),
            ([{"type": "text", "data": "invalid"}], ""),
            (["invalid"], "[invalid-segment]"),
            ({"type": "text"}, "[invalid-message]"),
        ],
    )
    def test_malformed_preview_input_never_raises(self, message, expected):
        assert _extract_message_preview(message) == expected


class TestSummarizeEvent:
    """_summarize_event 测试"""

    def test_full_event(self):
        """测试完整事件"""
        event = {
            "post_type"   : "message",
            "message_type": "group",
            "user_id"     : 12345,
            "group_id"    : 67890,
            "message"     : [{"type": "text", "data": {"text": "hi"}}],
        }
        result = _summarize_event(event)
        assert "post_type=message" in result
        assert "message_type=group" in result
        assert "user_id=12345" in result
        assert "group_id=67890" in result

    def test_minimal_event(self):
        """测试最小事件"""
        event  = {"post_type": "notice"}
        result = _summarize_event(event)
        assert "post_type=notice" in result

    def test_message_length(self):
        """测试消息长度"""
        event = {
            "post_type": "message",
            "message"  : [{"type": "text"}, {"type": "image"}],
        }
        result = _summarize_event(event)
        assert "message_len=2" in result

    def test_string_message(self):
        """测试字符串消息"""
        event = {
            "post_type": "message",
            "message"  : "hello",
        }
        result = _summarize_event(event)
        assert "message_kind=str" in result
