"""
Color 插件单元测试
"""

import pytest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock
import importlib.util

ROOT = Path(__file__).resolve().parent.parent.parent

# 动态加载 color 插件模块
spec_convert = importlib.util.spec_from_file_location("color_convert", ROOT / "plugins" / "color" / "convert.py")
color_convert = importlib.util.module_from_spec(spec_convert)
spec_convert.loader.exec_module(color_convert)

spec_query = importlib.util.spec_from_file_location("color_query", ROOT / "plugins" / "color" / "query.py")
color_query = importlib.util.module_from_spec(spec_query)
spec_query.loader.exec_module(color_query)

spec_data_manager = importlib.util.spec_from_file_location("color_data_manager", ROOT / "plugins" / "color" / "data_manager.py")
color_data_manager = importlib.util.module_from_spec(spec_data_manager)

# 设置 data_manager 的依赖
from core.plugin_base import load_json, write_json, ensure_dir
color_data_manager.load_json = load_json
color_data_manager.write_json = write_json
color_data_manager.ensure_dir = ensure_dir
spec_data_manager.loader.exec_module(color_data_manager)

# ============================================================
# convert 模块测试
# ============================================================

class TestColorConvert:
    """颜色转换测试"""

    def test_rgb_to_hex(self):
        """测试 RGB 转 HEX"""
        result = color_convert.rgb_to_hex([255, 128, 0])
        assert result.upper() == "#FF8000"

    def test_rgb_to_hex_normalized(self):
        """测试 RGB 转 HEX 规范化"""
        result = color_convert.rgb_to_hex([255, 255, 255])
        assert result.upper() == "#FFFFFF"

    def test_hex_to_rgb(self):
        """测试 HEX 转 RGB"""
        result = color_convert.hex_to_rgb("#FF8000")
        assert result == [255, 128, 0]

    def test_hex_to_rgb_without_hash(self):
        """测试无 # 前缀的 HEX 转 RGB"""
        result = color_convert.hex_to_rgb("FF8000")
        assert result == [255, 128, 0]

    def test_hex_to_rgb_lowercase(self):
        """测试小写 HEX 转 RGB"""
        result = color_convert.hex_to_rgb("#ff8000")
        assert result == [255, 128, 0]

    def test_rgb_to_cmyk(self):
        """测试 RGB 转 CMYK"""
        result = color_convert.rgb_to_cmyk([255, 0, 0])
        # 纯红色
        assert len(result) == 4
        assert all(isinstance(x, (int, float)) for x in result)

    def test_validate_rgb_valid(self):
        """测试有效 RGB 验证"""
        is_valid, error = color_convert.validate_rgb([255, 128, 0])
        assert is_valid is True
        assert error is None

    def test_validate_rgb_invalid_range(self):
        """测试无效 RGB 范围"""
        is_valid, error = color_convert.validate_rgb([300, 128, 0])
        assert is_valid is False
        assert error is not None

    def test_validate_rgb_wrong_length(self):
        """测试 RGB 长度错误"""
        is_valid, error = color_convert.validate_rgb([255, 128])
        assert is_valid is False

    def test_validate_cmyk_valid(self):
        """测试有效 CMYK 验证"""
        is_valid, error = color_convert.validate_cmyk([0, 100, 100, 0])
        assert is_valid is True

    def test_validate_cmyk_invalid_range(self):
        """测试无效 CMYK 范围"""
        is_valid, error = color_convert.validate_cmyk([0, 150, 100, 0])
        assert is_valid is False

# ============================================================
# query 模块测试
# ============================================================

class TestColorQuery:
    """颜色查询测试"""

    @pytest.fixture
    def sample_colors(self):
        """示例颜色数据"""
        return [
            {"name": "胭脂", "RGB": [213, 69, 71], "CMYK": [15, 85, 75, 0], "hex": "#D54547"},
            {"name": "朱砂", "RGB": [255, 89, 86], "CMYK": [0, 75, 70, 0], "hex": "#FF5956"},
            {"name": "海棠红", "RGB": [231, 113, 86], "CMYK": [8, 65, 75, 0], "hex": "#E77156"},
        ]

    def test_find_by_name_found(self, sample_colors):
        """测试按名称查找（找到）"""
        result = color_query.find_by_name(sample_colors, "胭脂")
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_name_not_found(self, sample_colors):
        """测试按名称查找（未找到）"""
        result = color_query.find_by_name(sample_colors, "不存在")
        assert result is None

    def test_find_by_rgb_found(self, sample_colors):
        """测试按 RGB 查找（找到）"""
        result = color_query.find_by_rgb(sample_colors, [213, 69, 71])
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_rgb_not_found(self, sample_colors):
        """测试按 RGB 查找（未找到）"""
        result = color_query.find_by_rgb(sample_colors, [0, 0, 0])
        assert result is None

    def test_find_by_hex_found(self, sample_colors):
        """测试按 HEX 查找（找到）"""
        result = color_query.find_by_hex(sample_colors, "#D54547")
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_hex_not_found(self, sample_colors):
        """测试按 HEX 查找（未找到）"""
        result = color_query.find_by_hex(sample_colors, "#000000")
        assert result is None

    def test_find_by_cmyk_found(self, sample_colors):
        """测试按 CMYK 查找（找到）"""
        result = color_query.find_by_cmyk(sample_colors, [15, 85, 75, 0])
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_keyword(self, sample_colors):
        """测试按关键词搜索"""
        results = color_query.find_by_keyword(sample_colors, "红")
        assert len(results) >= 1
        assert "海棠红" in [r["name"] for r in results]

# ============================================================
# data_manager 模块测试
# ============================================================

class TestColorDataManager:
    """颜色数据管理器测试"""

    def test_format_color_info(self):
        """测试格式化颜色信息"""
        color = {
            "name": "胭脂",
            "RGB": [213, 69, 71],
            "CMYK": [15, 85, 75, 0],
            "hex": "#D54547",
        }
        result = color_data_manager.format_color_info(color)
        assert "胭脂" in result
        assert "#D54547" in result or "213, 69, 71" in result

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
