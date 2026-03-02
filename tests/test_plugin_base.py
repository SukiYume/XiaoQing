"""
plugin_base 工具函数单元测试
"""

import pytest
from pathlib import Path

from core.plugin_base import (
    text,
    image,
    image_url,
    segments,
    build_action,
    load_json,
    write_json,
    ensure_dir,
    split_message_segments,
)

# ============================================================
# 消息段构建测试
# ============================================================

class TestMessageSegments:
    """消息段构建函数测试"""

    def test_text_segment(self):
        """测试文本消息段"""
        seg = text("Hello World")
        assert seg == {"type": "text", "data": {"text": "Hello World"}}

    def test_text_segment_with_unicode(self):
        """测试中文文本消息段"""
        seg = text("你好世界 🌍")
        assert seg == {"type": "text", "data": {"text": "你好世界 🌍"}}

    def test_image_segment(self):
        """测试本地图片消息段"""
        seg = image("/path/to/image.png")
        # 使用 Path 生成标准 file URI（相对路径会被 resolve 为绝对路径）
        expected_uri = Path("/path/to/image.png").resolve().as_uri()
        assert seg == {"type": "image", "data": {"file": expected_uri}}

    def test_image_url_segment(self):
        """测试网络图片消息段"""
        seg = image_url("https://example.com/image.png")
        assert seg == {"type": "image", "data": {"file": "https://example.com/image.png"}}

# ============================================================
# segments 转换测试
# ============================================================

class TestSegmentsConversion:
    """segments() 函数测试"""

    def test_segments_from_string(self):
        """测试字符串转换"""
        result = segments("Hello")
        assert result == [{"type": "text", "data": {"text": "Hello"}}]

    def test_segments_from_list(self):
        """测试列表直接返回"""
        input_list = [
            {"type": "text", "data": {"text": "Hello"}},
            {"type": "image", "data": {"file": "test.png"}},
        ]
        result = segments(input_list)
        assert result == input_list

    def test_segments_from_none(self):
        """测试 None 返回空列表"""
        result = segments(None)
        assert result == []

    def test_segments_empty_string(self):
        """测试空字符串"""
        result = segments("")
        assert result == [{"type": "text", "data": {"text": ""}}]

# ============================================================
# build_action 测试
# ============================================================

class TestBuildAction:
    """build_action() 函数测试"""

    def test_build_group_message(self):
        """测试构建群消息"""
        segs = [{"type": "text", "data": {"text": "Hello"}}]
        action = build_action(segs, user_id=12345, group_id=67890)
        
        assert action is not None
        assert action["action"] == "send_group_msg"
        assert action["params"]["group_id"] == 67890
        assert action["params"]["message"] == segs

    def test_build_private_message(self):
        """测试构建私聊消息"""
        segs = [{"type": "text", "data": {"text": "Hello"}}]
        action = build_action(segs, user_id=12345, group_id=None)
        
        assert action is not None
        assert action["action"] == "send_private_msg"
        assert action["params"]["user_id"] == 12345
        assert action["params"]["message"] == segs

    def test_build_action_prefers_group(self):
        """测试同时有 user_id 和 group_id 时优先群消息"""
        segs = [{"type": "text", "data": {"text": "Hello"}}]
        action = build_action(segs, user_id=12345, group_id=67890)
        
        assert action["action"] == "send_group_msg"

    def test_build_action_empty_segments(self):
        """测试空消息段返回 None"""
        action = build_action([], user_id=12345, group_id=None)
        assert action is None

    def test_build_action_no_target(self):
        """测试无目标返回 None"""
        segs = [{"type": "text", "data": {"text": "Hello"}}]
        action = build_action(segs, user_id=None, group_id=None)
        assert action is None

# ============================================================
# JSON 工具函数测试
# ============================================================

class TestJsonUtils:
    """JSON 工具函数测试"""

    def test_load_json_nonexistent(self, tmp_path: Path):
        """测试加载不存在的文件"""
        result = load_json(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_json_with_default(self, tmp_path: Path):
        """测试加载不存在的文件使用默认值"""
        result = load_json(tmp_path / "nonexistent.json", {"key": "value"})
        assert result == {"key": "value"}

    def test_write_and_load_json(self, tmp_path: Path):
        """测试写入和读取 JSON"""
        json_path = tmp_path / "test.json"
        data = {"name": "测试", "count": 42, "items": [1, 2, 3]}
        
        write_json(json_path, data)
        loaded = load_json(json_path)
        
        assert loaded == data

    def test_load_invalid_json(self, tmp_path: Path):
        """测试加载无效 JSON 文件"""
        json_path = tmp_path / "invalid.json"
        json_path.write_text("not valid json {{{", encoding="utf-8")
        
        result = load_json(json_path)
        assert result == {}

# ============================================================
# 目录工具函数测试
# ============================================================

class TestDirUtils:
    """目录工具函数测试"""

    def test_ensure_dir_creates_directory(self, tmp_path: Path):
        """测试创建目录"""
        new_dir = tmp_path / "new" / "nested" / "dir"
        assert not new_dir.exists()
        
        ensure_dir(new_dir)
        
        assert new_dir.exists()
        assert new_dir.is_dir()

    def test_ensure_dir_existing(self, tmp_path: Path):
        """测试对已存在目录无影响"""
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()
        
        # 应该不报错
        ensure_dir(existing_dir)
        
        assert existing_dir.exists()

# ============================================================
# 长消息拆分测试
# ============================================================

class TestSplitMessageSegments:
    """split_message_segments() 函数测试"""

    def test_short_message_no_split(self):
        """短消息不拆分"""
        segs = segments("Hello World")
        result = split_message_segments(segs)
        assert len(result) == 1
        assert result[0] == segs

    def test_empty_segments(self):
        """空消息段不拆分"""
        result = split_message_segments([])
        assert len(result) == 1

    def test_long_text_splits(self):
        """超长文本按行拆分"""
        # 构造一段超过默认限制的文本（每行 50 字符，100 行 = 5000+ 字符）
        lines = [f"Line {i:04d}: {'x' * 42}" for i in range(100)]
        long_text = "\n".join(lines)
        segs = segments(long_text)
        result = split_message_segments(segs, max_length=500)
        
        # 应该拆分为多条
        assert len(result) > 1
        # 每条消息的文本长度不应超过限制
        for chunk in result:
            chunk_text = "".join(
                seg["data"]["text"] for seg in chunk if seg["type"] == "text"
            )
            assert len(chunk_text) <= 500

    def test_single_long_line_force_split(self):
        """单行超长文本按字符强制拆分"""
        long_line = "A" * 1000
        segs = segments(long_line)
        result = split_message_segments(segs, max_length=300)
        
        # 应该拆分为多条
        assert len(result) >= 4  # 1000 / 300 = ~4
        # 拼接后应该与原始文本相同
        reconstructed = "".join(
            seg["data"]["text"]
            for chunk in result
            for seg in chunk
            if seg["type"] == "text"
        )
        assert reconstructed == long_line

    def test_mixed_content_no_split(self):
        """包含非文本段（如图片）的消息不拆分"""
        segs = [
            {"type": "text", "data": {"text": "A" * 5000}},
            {"type": "image", "data": {"file": "test.png"}},
        ]
        result = split_message_segments(segs, max_length=300)
        assert len(result) == 1
        assert result[0] == segs

    def test_preserves_content_integrity(self):
        """拆分后拼接内容与原始内容一致"""
        lines = [f"文件{i}.txt" for i in range(50)]
        original = "\n".join(lines)
        segs = segments(original)
        result = split_message_segments(segs, max_length=100)
        
        reconstructed = "\n".join(
            seg["data"]["text"]
            for chunk in result
            for seg in chunk
            if seg["type"] == "text"
        )
        assert reconstructed == original

    def test_none_segments(self):
        """None 消息段"""
        result = split_message_segments(segments(None))
        assert len(result) == 1

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
