"""
dict 插件单元测试
"""
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util

# 动态加载 dict 插件
spec = importlib.util.spec_from_file_location("dict_main", ROOT / "plugins" / "dict" / "main.py")
dict_plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dict_plugin)


@pytest.fixture
def mock_context():
    """模拟插件上下文"""
    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "dict"
            self.data_dir = self.plugin_dir / "data"
            self.logger = self._create_logger()

        def _create_logger(self):
            import logging
            return logging.getLogger("test")

    return MockContext()


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": "12345",
        "message": "test"
    }


class TestDictHelp:
    """测试帮助功能"""

    @pytest.mark.asyncio
    async def test_help_command(self, mock_context, mock_event):
        """测试 help 命令"""
        result = await dict_plugin.handle("dict", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "dict" in result_text.lower() or "词典" in result_text or "天文" in result_text

    @pytest.mark.asyncio
    async def test_empty_args(self, mock_context, mock_event):
        """测试空参数"""
        result = await dict_plugin.handle("dict", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0


class TestDictSearch:
    """测试词典查询功能"""

    @pytest.mark.asyncio
    async def test_search_english(self, mock_context, mock_event):
        """测试英文查询"""
        # 注意：这个测试可能需要实际的词典文件
        result = await dict_plugin.handle("dict", "galaxy", mock_event, mock_context)
        assert result is not None
        # 不应该抛出异常
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_search_chinese(self, mock_context, mock_event):
        """测试中文查询"""
        result = await dict_plugin.handle("dict", "星系", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_search_not_found(self, mock_context, mock_event):
        """测试查询不存在的词"""
        result = await dict_plugin.handle("dict", "xyzabc123notexist", mock_event, mock_context)
        assert result is not None
        # 应该返回未找到的提示
        assert len(result) > 0


class TestDictErrorHandling:
    """测试错误处理"""

    @pytest.mark.asyncio
    async def test_special_characters(self, mock_context, mock_event):
        """测试特殊字符"""
        result = await dict_plugin.handle("dict", "!@#$%", mock_event, mock_context)
        assert result is not None
        # 不应该抛出异常
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_very_long_query(self, mock_context, mock_event):
        """测试超长查询"""
        long_query = "a" * 200
        result = await dict_plugin.handle("dict", long_query, mock_event, mock_context)
        assert result is not None
        assert len(result) > 0


class TestDictDataLoading:
    """测试数据加载"""

    def test_load_dictionary_nonexistent(self):
        """测试加载不存在的词典"""
        nonexistent_path = Path("/nonexistent/path/dict.tsv")
        result = dict_plugin._load_dictionary(nonexistent_path)
        # 应该返回 None
        assert result is None


def test_init():
    """测试插件初始化"""
    dict_plugin.init()
    assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
