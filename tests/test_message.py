"""
消息处理工具单元测试
"""

import pytest
from typing import Any

from core.message import (
    contains_bot_name,
    compile_bot_name_pattern,
    extract_text,
    has_at_mention,
    has_media_segment,
    iter_message_segments,
    normalize_message,
    parse_text_command_context,
    scan_message,
    strip_message_prefix,
)

# ============================================================
# extract_text 测试
# ============================================================

class TestExtractText:
    """extract_text() 函数测试"""

    def test_extract_from_string(self):
        """测试从字符串提取"""
        result = extract_text("Hello World")
        assert result == "Hello World"

    def test_extract_from_text_segment(self):
        """测试从文本消息段列表提取"""
        message = [
            {"type": "text", "data": {"text": "Hello "}},
            {"type": "text", "data": {"text": "World"}},
        ]
        result = extract_text(message)
        assert result == "Hello World"

    def test_extract_ignores_non_text(self):
        """测试忽略非文本消息段"""
        message = [
            {"type": "text", "data": {"text": "看这张图: "}},
            {"type": "image", "data": {"file": "test.png"}},
            {"type": "text", "data": {"text": " 好看吗?"}},
        ]
        result = extract_text(message)
        assert result == "看这张图:  好看吗?"

    def test_extract_from_at_message(self):
        """测试带 @ 的消息"""
        message = [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " 你好"}},
        ]
        result = extract_text(message)
        assert result == " 你好"

    def test_extract_empty_list(self):
        """测试空列表"""
        result = extract_text([])
        assert result == ""

    def test_extract_other_types(self):
        """测试其他类型返回空字符串"""
        assert extract_text(None) == ""
        assert extract_text(123) == ""
        assert extract_text({"type": "text"}) == ""


class TestMessageScan:
    def test_scan_message_collects_text_media_and_at(self):
        result = scan_message(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": "你好"}},
                {"type": "face", "data": {"id": "14"}},
            ],
            self_id="12345",
        )
        assert result.text == "你好"
        assert result.has_media is True
        assert result.is_at_me is True

    def test_iter_message_segments_filters_invalid_items(self):
        message = [{"type": "text", "data": {"text": "hi"}}, None, "bad", {"type": "face", "data": {}}]
        assert iter_message_segments(message) == (
            {"type": "text", "data": {"text": "hi"}},
            {"type": "face", "data": {}},
        )

    def test_has_at_mention_checks_event_and_raw_message(self):
        event = {
            "self_id": "12345",
            "raw_message": "[CQ:at,qq=12345]",
            "message": [{"type": "text", "data": {"text": ""}}],
        }
        assert has_at_mention(event, self_id="12345") is True

    def test_contains_bot_name_handles_empty_values(self):
        assert contains_bot_name("", "小青") is False
        assert contains_bot_name("你好小青", "") is False
        assert contains_bot_name("你好小青", "小青") is True

# ============================================================
# normalize_message 测试
# ============================================================

class TestNormalizeMessage:
    """normalize_message() 函数测试"""

    def test_normalize_group_message(self):
        """测试群消息解析"""
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "message": [{"type": "text", "data": {"text": "  /echo test  "}}],
        }
        
        text, user_id, group_id = normalize_message(event)
        
        assert text == "/echo test"
        assert user_id == 12345
        assert group_id == 67890

    def test_normalize_private_message(self):
        """测试私聊消息解析"""
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "message": "你好",
        }

        text, user_id, group_id = normalize_message(event)

        assert text == "你好"
        assert user_id == 12345
        assert group_id is None

    def test_normalize_strips_whitespace(self):
        """测试去除首尾空白"""
        event = {
            "message": "  \n  Hello World  \t  ",
            "user_id": 1,
        }

        text, _, _ = normalize_message(event)
        assert text == "Hello World"

    def test_normalize_missing_fields(self):
        """测试缺失字段处理"""
        event = {"message": "test"}

        text, user_id, group_id = normalize_message(event)

        assert text == "test"
        assert user_id is None
        assert group_id is None


class TestHasMediaSegment:
    def test_detects_image_segment(self):
        message = [{"type": "image", "data": {"file": "file:///tmp/test.png"}}]
        assert has_media_segment(message) is True

    def test_detects_face_segment(self):
        message = [{"type": "face", "data": {"id": "14"}}]
        assert has_media_segment(message) is True

    def test_ignores_text_and_at_only(self):
        message = [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": ""}},
        ]
        assert has_media_segment(message) is False

    def test_non_list_message_returns_false(self):
        assert has_media_segment("hello") is False

class TestParseTextCommandContext:
    def test_strips_bot_name_and_prefix(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "小青，/echo hi",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.is_at_me is False
        assert result.clean_text == "echo hi"
        assert result.has_bot_name is True
        assert result.has_command_prefix is False  # "/" not at start of raw text
        assert result.has_prefix is True            # union: has_bot_name
        assert result.is_only_bot_name is False
        assert result.is_url_only is False

    def test_detects_at_segment_as_mention(self):
        event = {
            "self_id": "12345",
            "message": [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 你好"}},
            ],
        }
        result = parse_text_command_context(
            "你好",
            event,
            bot_name="",
            prefixes=("/",),
            self_id="12345",
        )
        assert result.is_at_me is True
        assert result.clean_text == "你好"
        assert result.has_prefix is True            # union: is_at_me

    def test_strict_command_prefix_at_start(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "/help",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.has_command_prefix is True
        assert result.has_prefix is True
        assert result.has_bot_name is False
        assert result.is_at_me is False
        assert result.clean_text == "help"

    def test_bot_name_in_middle_counts_as_has_prefix(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "你好啊小青",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.has_command_prefix is False
        assert result.has_bot_name is True
        assert result.has_prefix is True
        assert result.is_at_me is False

    def test_plain_text_no_signals(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "在吗",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.has_command_prefix is False
        assert result.has_bot_name is False
        assert result.is_at_me is False
        assert result.has_prefix is False

    def test_is_only_bot_name(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "小青",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.is_only_bot_name is True

    def test_is_url_only_after_strip(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "小青 https://example.com",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.clean_text == "https://example.com"
        assert result.is_url_only is True
        assert result.has_prefix is True

    def test_is_url_only_bare_url(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "https://example.com",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.is_url_only is True
        assert result.has_prefix is False

    def test_url_with_extra_text_is_not_url_only(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        result = parse_text_command_context(
            "看看 https://example.com",
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert result.is_url_only is False

    def test_strip_message_prefix_with_cached_pattern(self):
        pattern = compile_bot_name_pattern("Bot")
        clean = strip_message_prefix(
            "Bot,  /help",
            bot_name="Bot",
            prefixes=("/",),
            bot_name_pattern=pattern,
        )
        assert clean == "help"

class TestIsCleanTextUrlOnly:
    def test_bare_url(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("https://example.com") is True

    def test_http_url(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("http://example.com/path?q=1") is True

    def test_url_with_surrounding_whitespace(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("  https://example.com  ") is True

    def test_text_before_url_rejected(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("看看 https://example.com") is False

    def test_text_after_url_rejected(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("https://example.com 看看") is False

    def test_multiple_urls_rejected(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("https://a.com https://b.com") is False

    def test_empty_rejected(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("") is False

    def test_non_http_scheme_rejected(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("ftp://example.com") is False

    def test_plain_text_rejected(self):
        from core.message import is_clean_text_url_only
        assert is_clean_text_url_only("你好") is False

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
