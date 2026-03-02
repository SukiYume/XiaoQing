"""
memo 插件单元测试
"""
import json
import pytest
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("memo_main", ROOT / "plugins" / "memo" / "main.py")
memo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memo)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_data_dir():
    """创建临时数据目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        yield data_dir


@pytest.fixture
def mock_context(temp_data_dir):
    """模拟插件上下文"""
    class MockContext:
        def __init__(self, data_dir):
            self.data_dir = data_dir
            self.secrets = {"admin_user_ids": [12345, 67890]}

    return MockContext(temp_data_dir)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {"user_id": 12345}


@pytest.fixture
def mock_admin_event():
    """模拟管理员事件"""
    return {"user_id": 12345}  # 12345 is in admin_user_ids


# ============================================================
# Test Add Memo
# ============================================================

class TestAddMemo:
    """测试添加笔记功能"""

    @pytest.mark.asyncio
    async def test_add_simple_memo(self, mock_context, mock_event):
        """测试添加简单笔记"""
        result = await memo.handle("memo", "待办 买菜", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "待办" in result_text or "成功" in result_text or "已添加" in result_text

        # 验证数据已保存
        data = memo._load_memo(mock_context)
        assert "待办" in data
        assert len(data["待办"]) == 1
        assert data["待办"][0]["content"] == "买菜"

    @pytest.mark.asyncio
    async def test_add_multiple_memos(self, mock_context, mock_event):
        """测试添加多条笔记"""
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)
        await memo.handle("memo", "待办 做饭", mock_event, mock_context)
        await memo.handle("memo", "shopping 打酱油", mock_event, mock_context)

        data = memo._load_memo(mock_context)
        assert len(data["待办"]) == 2
        assert len(data["shopping"]) == 1

    @pytest.mark.asyncio
    async def test_add_memo_with_special_chars(self, mock_context, mock_event):
        """测试添加包含特殊字符的笔记"""
        special_content = "测试 @#$%^&*() 内容"
        result = await memo.handle("memo", f"special {special_content}", mock_event, mock_context)
        assert result is not None

        data = memo._load_memo(mock_context)
        assert data["special"][0]["content"] == special_content

    @pytest.mark.asyncio
    async def test_add_memo_with_chinese_punctuation(self, mock_context, mock_event):
        """测试添加包含中文标点的笔记"""
        content = "会议：下午三点，记得带材料！"
        result = await memo.handle("memo", f"工作 {content}", mock_event, mock_context)
        assert result is not None

        data = memo._load_memo(mock_context)
        assert data["工作"][0]["content"] == content

    @pytest.mark.asyncio
    async def test_add_empty_content_after_category(self, mock_context, mock_event):
        """测试分类后只有空格的情况"""
        # 先创建分类
        await memo.handle("memo", "test 有效内容", mock_event, mock_context)
        # 然后尝试添加空内容到同一分类
        # 由于 parse() 会去除空格，"test    " 只有一个参数，所以会查看分类
        result = await memo.handle("memo", "test    ", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        # 因为 "test" 分类存在，所以会显示该分类的内容
        assert "test" in result_text
        assert "有效内容" in result_text


# ============================================================
# Test View Memos
# ============================================================

class TestViewMemos:
    """测试查看笔记功能"""

    @pytest.mark.asyncio
    async def test_list_categories(self, mock_context, mock_event):
        """测试列出所有分类"""
        # 先添加一些笔记
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)
        await memo.handle("memo", "shopping 买米", mock_event, mock_context)

        result = await memo.handle("memo", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "待办" in result_text
        assert "shopping" in result_text or "笔记" in result_text

    @pytest.mark.asyncio
    async def test_list_empty_memos(self, mock_context, mock_event):
        """测试列出空笔记列表"""
        result = await memo.handle("memo", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "暂无" in result_text or "提示" in result_text

    @pytest.mark.asyncio
    async def test_view_category(self, mock_context, mock_event):
        """测试查看指定分类"""
        # 先添加笔记
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)
        await memo.handle("memo", "待备 做饭", mock_event, mock_context)

        result = await memo.handle("memo", "待办", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "买菜" in result_text
        assert "2 条" in result_text or "2" in result_text

    @pytest.mark.asyncio
    async def test_view_nonexistent_category(self, mock_context, mock_event):
        """测试查看不存在的分类"""
        result = await memo.handle("memo", "不存在的分类", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "不存在" in result_text or "无" in result_text

    @pytest.mark.asyncio
    async def test_fuzzy_match_category(self, mock_context, mock_event):
        """测试分类模糊匹配"""
        await memo.handle("memo", "待办事项 买菜", mock_event, mock_context)

        result = await memo.handle("memo", "待办", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "买菜" in result_text


# ============================================================
# Test Delete Memo
# ============================================================

class TestDeleteMemo:
    """测试删除笔记功能"""

    @pytest.mark.asyncio
    async def test_delete_single_memo(self, mock_context, mock_event):
        """测试删除单条笔记"""
        # 先添加笔记
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)
        await memo.handle("memo", "待办 做饭", mock_event, mock_context)

        # 删除第一条
        result = await memo.handle("memo", "del 待办 1", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "已删除" in result_text or "成功" in result_text

        # 验证删除结果
        data = memo._load_memo(mock_context)
        assert len(data["待办"]) == 1
        assert data["待办"][0]["content"] == "做饭"

    @pytest.mark.asyncio
    async def test_delete_category(self, mock_context, mock_event):
        """测试删除整个分类"""
        # 先添加笔记
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)
        await memo.handle("memo", "shopping 买米", mock_event, mock_context)

        # 删除整个分类
        result = await memo.handle("memo", "del shopping", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "已删除" in result_text or "成功" in result_text

        # 验证删除结果
        data = memo._load_memo(mock_context)
        assert "shopping" not in data
        assert "待办" in data

    @pytest.mark.asyncio
    async def test_delete_invalid_index(self, mock_context, mock_event):
        """测试删除无效序号"""
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)

        result = await memo.handle("memo", "del 待办 99", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "无效" in result_text or "序号" in result_text

    @pytest.mark.asyncio
    async def test_delete_nonexistent_category(self, mock_context, mock_event):
        """测试删除不存在的分类"""
        result = await memo.handle("memo", "del 不存在的分类", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "不存在" in result_text

    @pytest.mark.asyncio
    async def test_delete_last_memo_removes_category(self, mock_context, mock_event):
        """测试删除最后一条笔记时移除分类"""
        await memo.handle("memo", "temp 临时笔记", mock_event, mock_context)

        # 删除唯一的笔记
        await memo.handle("memo", "del temp 1", mock_event, mock_context)

        # 分类应该被移除
        data = memo._load_memo(mock_context)
        assert "temp" not in data


# ============================================================
# Test Search
# ============================================================

class TestSearch:
    """测试搜索功能"""

    @pytest.mark.asyncio
    async def test_search_keyword(self, mock_context, mock_event):
        """测试关键词搜索"""
        # 添加笔记
        await memo.handle("memo", "工作 下午开会", mock_event, mock_context)
        await memo.handle("memo", "生活 买牛奶", mock_event, mock_context)
        await memo.handle("memo", "会议 准备PPT", mock_event, mock_context)

        result = await memo.handle("memo", "search 会", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "开会" in result_text or "会议" in result_text

    @pytest.mark.asyncio
    async def test_search_no_results(self, mock_context, mock_event):
        """测试搜索无结果"""
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)

        result = await memo.handle("memo", "search 不存在的关键词xyz", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "未找到" in result_text or "没有" in result_text

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self, mock_context, mock_event):
        """测试空关键词搜索"""
        result = await memo.handle("memo", "search", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "请提供" in result_text or "关键词" in result_text


# ============================================================
# Test Export
# ============================================================

class TestExport:
    """测试导出功能"""

    @pytest.mark.asyncio
    async def test_export_all(self, mock_context, mock_event):
        """测试导出所有笔记"""
        # 添加一些笔记
        await memo.handle("memo", "待办 买菜", mock_event, mock_context)
        await memo.handle("memo", "shopping 买米", mock_event, mock_context)

        result = await memo.handle("memo", "export", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "待办" in result_text or "shopping" in result_text
        assert "共" in result_text or "条" in result_text

    @pytest.mark.asyncio
    async def test_export_empty(self, mock_context, mock_event):
        """测试导出空笔记"""
        result = await memo.handle("memo", "export", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "暂无" in result_text


# ============================================================
# Test Clear
# ============================================================

class TestClear:
    """测试清空功能"""

    @pytest.mark.asyncio
    async def test_clear_all_as_admin(self, mock_context, mock_admin_event):
        """测试管理员清空所有笔记"""
        # 添加一些笔记
        await memo.handle("memo", "待办 买菜", mock_admin_event, mock_context)
        await memo.handle("memo", "shopping 买米", mock_admin_event, mock_context)

        # 清空
        result = await memo.handle("memo", "clear", mock_admin_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "已清空" in result_text or "成功" in result_text

        # 验证清空结果
        data = memo._load_memo(mock_context)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_clear_as_non_admin(self, mock_context, mock_event):
        """测试非管理员尝试清空"""
        # 修改 mock_event 为非管理员用户
        mock_event["user_id"] = 99999

        result = await memo.handle("memo", "clear", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "仅管理员" in result_text or "权限" in result_text


# ============================================================
# Test Help
# ============================================================

class TestHelp:
    """测试帮助功能"""

    @pytest.mark.asyncio
    async def test_help_command(self, mock_context, mock_event):
        """测试 help 命令"""
        result = await memo.handle("memo", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_question_mark(self, mock_context, mock_event):
        """测试 ? 命令"""
        result = await memo.handle("memo", "?", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_chinese_help(self, mock_context, mock_event):
        """测试中文帮助命令"""
        result = await memo.handle("memo", "帮助", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0


# ============================================================
# Test Data Persistence
# ============================================================

class TestDataPersistence:
    """测试数据持久化"""

    @pytest.mark.asyncio
    async def test_data_saved_to_file(self, mock_context, mock_event):
        """测试数据保存到文件"""
        await memo.handle("memo", "test 测试笔记", mock_event, mock_context)

        # 检查文件是否存在
        memo_file = mock_context.data_dir / "memo.json"
        assert memo_file.exists()

        # 读取并验证内容
        content = memo_file.read_text(encoding="utf-8")
        data = json.loads(content)
        assert "test" in data
        assert data["test"][0]["content"] == "测试笔记"

    @pytest.mark.asyncio
    async def test_data_loaded_from_file(self, mock_context, mock_event):
        """测试从文件加载数据"""
        # 直接创建数据文件
        memo_file = mock_context.data_dir / "memo.json"
        test_data = {
            "existing": [{"content": "已有笔记", "time": "2024-01-01 12:00", "user": 12345}]
        }
        memo_file.write_text(json.dumps(test_data, ensure_ascii=False), encoding="utf-8")

        # 加载并验证
        data = memo._load_memo(mock_context)
        assert "existing" in data
        assert data["existing"][0]["content"] == "已有笔记"

    @pytest.mark.asyncio
    async def test_old_format_compatibility(self, mock_context, mock_event):
        """测试旧格式兼容性"""
        # 创建旧格式的数据文件（纯字符串列表）
        memo_file = mock_context.data_dir / "memo.json"
        old_data = {"old_category": ["note1", "note2"]}
        memo_file.write_text(json.dumps(old_data, ensure_ascii=False), encoding="utf-8")

        # 加载应该转换为新格式
        data = memo._load_memo(mock_context)
        assert "old_category" in data
        # 旧格式应该被转换
        assert isinstance(data["old_category"][0], dict)
        assert data["old_category"][0]["content"] == "note1"


# ============================================================
# Test User Metadata
# ============================================================

class TestUserMetadata:
    """测试用户元数据"""

    @pytest.mark.asyncio
    async def test_user_id_tracked(self, mock_context, mock_event):
        """测试用户ID被记录"""
        await memo.handle("memo", "test 测试", mock_event, mock_context)

        data = memo._load_memo(mock_context)
        assert data["test"][0]["user"] == 12345

    @pytest.mark.asyncio
    async def test_time_tracked(self, mock_context, mock_event):
        """测试时间被记录"""
        await memo.handle("memo", "test 测试", mock_event, mock_context)

        data = memo._load_memo(mock_context)
        assert "time" in data["test"][0]
        assert len(data["test"][0]["time"]) > 0


# ============================================================
# Test Edge Cases
# ============================================================

class TestEdgeCases:
    """测试边界情况"""

    @pytest.mark.asyncio
    async def test_very_long_memo(self, mock_context, mock_event):
        """测试超长笔记"""
        long_content = "A" * 1000
        result = await memo.handle("memo", f"long {long_content}", mock_event, mock_context)
        assert result is not None

        data = memo._load_memo(mock_context)
        assert data["long"][0]["content"] == long_content

    @pytest.mark.asyncio
    async def test_unicode_memo(self, mock_context, mock_event):
        """测试Unicode笔记"""
        unicode_content = "Hello 世界 🎉 مرحبا"
        result = await memo.handle("memo", f"unicode {unicode_content}", mock_event, mock_context)
        assert result is not None

        data = memo._load_memo(mock_context)
        assert data["unicode"][0]["content"] == unicode_content

    @pytest.mark.asyncio
    async def test_newlines_in_memo(self, mock_context, mock_event):
        """测试包含换行的笔记"""
        # parse() 会将换行符替换为空格，所以这里测试的是实际存储的内容
        content = "第一行\n第二行\n第三行"
        result = await memo.handle("memo", f"multiline {content}", mock_event, mock_context)
        assert result is not None

        data = memo._load_memo(mock_context)
        # parse() 会将 \n 转换为空格
        stored_content = content.replace("\n", " ")
        assert data["multiline"][0]["content"] == stored_content


# ============================================================
# Test Init
# ============================================================

def test_init():
    """测试插件初始化"""
    memo.init()
    assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
