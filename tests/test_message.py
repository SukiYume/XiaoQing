"""
消息处理工具单元测试
"""

import pytest
from typing import Any

from core.message import (
    compile_bot_name_pattern,
    extract_text,
    normalize_message,
    parse_text_command_context,
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

class TestParseTextCommandContext:
    def test_strips_bot_name_and_prefix(self):
        event: dict[str, Any] = {"message": [{"type": "text", "data": {"text": "ignored"}}]}
        text = "小青，/echo hi"
        is_at_me, clean_text, has_bot_name, has_prefix, is_only_bot_name = parse_text_command_context(
            text,
            event,
            bot_name="小青",
            prefixes=("/",),
            self_id="",
        )
        assert is_at_me is False
        assert clean_text == "echo hi"
        assert has_bot_name is True
        assert has_prefix is False
        assert is_only_bot_name is False

    def test_detects_at_segment_as_mention(self):
        event = {
            "self_id": "12345",
            "message": [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 你好"}},
            ],
        }
        text = "你好"
        is_at_me, clean_text, _, _, _ = parse_text_command_context(
            text,
            event,
            bot_name="",
            prefixes=("/",),
            self_id="12345",
        )
        assert is_at_me is True
        assert clean_text == "你好"

    def test_strip_message_prefix_with_cached_pattern(self):
        pattern = compile_bot_name_pattern("Bot")
        clean = strip_message_prefix(
            "Bot,  /help",
            bot_name="Bot",
            prefixes=("/",),
            bot_name_pattern=pattern,
        )
        assert clean == "help"

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

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
