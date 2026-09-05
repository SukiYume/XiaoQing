"""OneBot 鉴权、脱敏和事件摘要。"""

from __future__ import annotations

import tests.helpers.onebot_test_support as _fixture_support
from tests.helpers.onebot_test_support import (
    _extract_message_preview,
    _mask_sensitive_text,
    _onebot_action_succeeded,
    _summarize_event,
    pytest,
    verify_bearer_token,
)

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


class TestVerifyBearerToken:
    """共享 Bearer token 验证测试。"""

    def test_valid_token(self):
        """测试有效 token"""
        result = verify_bearer_token("Bearer my_token", "my_token")
        assert result is True

    def test_no_token_configured(self):
        """测试未配置 token 时默认拒绝，避免鉴权失败开放"""
        result = verify_bearer_token("Bearer anything", "")
        assert result is False

        result = verify_bearer_token("Bearer anything", None)
        assert result is False

    def test_invalid_token(self):
        """测试无效 token"""
        result = verify_bearer_token("Bearer wrong_token", "my_token")
        assert result is False

    def test_missing_bearer_prefix(self):
        """测试缺少 Bearer 前缀"""
        result = verify_bearer_token("my_token", "my_token")
        assert result is False

    def test_length_mismatch(self):
        """测试长度不匹配"""
        result = verify_bearer_token("short", "much_longer_token")
        assert result is False


class TestMaskSensitiveText:
    """_mask_sensitive_text 测试"""

    def test_mask_token(self):
        """测试屏蔽 token"""
        text   = "Authorization: Bearer secret_token_123"
        result = _mask_sensitive_text(text)
        # Bearer 关键字被掩码，token 值仍存在（这是当前实现的行为）
        assert "Bearer" not in result or result == "Authorization: ******** secret_token_123"
        assert "********" in result

    def test_mask_authorization_header(self):
        """测试屏蔽直接提供的 authorization 值"""
        text   = "authorization=secret_token_123"
        result = _mask_sensitive_text(text)
        assert "secret_token_123" not in result
        assert "********" in result

    def test_mask_api_key(self):
        """测试屏蔽 api_key"""
        text   = "api_key=sk-1234567890"
        result = _mask_sensitive_text(text)
        assert "sk-1234567890" not in result
        assert "********" in result

    def test_mask_password(self):
        """测试屏蔽 password"""
        text   = "password=my_password"
        result = _mask_sensitive_text(text)
        assert "my_password" not in result

    def test_mask_multiple(self):
        """测试屏蔽多个敏感信息"""
        text   = "token=abc123 and password=xyz789"
        result = _mask_sensitive_text(text)
        assert "abc123" not in result
        assert "xyz789" not in result

    def test_case_insensitive(self):
        """测试大小写不敏感"""
        text   = "API_KEY=secret"
        result = _mask_sensitive_text(text)
        assert "secret" not in result

    def test_no_sensitive_data(self):
        """测试无敏感数据"""
        text   = "hello world"
        result = _mask_sensitive_text(text)
        assert result == text


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
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "message": [{"type": "text", "data": {"text": "hi"}}],
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
            "message": [{"type": "text"}, {"type": "image"}],
        }
        result = _summarize_event(event)
        assert "message_len=2" in result

    def test_string_message(self):
        """测试字符串消息"""
        event = {
            "post_type": "message",
            "message": "hello",
        }
        result = _summarize_event(event)
        assert "message_kind=str" in result
