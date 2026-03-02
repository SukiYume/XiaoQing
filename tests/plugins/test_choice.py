"""测试choice插件"""

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("choice_main", ROOT / "plugins" / "choice" / "main.py")
choice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(choice)


class TestChoicePlugin:
    """测试choice插件"""

    @pytest.fixture
    def mock_context(self):
        """模拟插件上下文"""
        class MockContext:
            def __init__(self):
                self.plugin_dir = ROOT / "plugins" / "choice"
                self.data_dir = self.plugin_dir / "data"
                self.logger = __import__('logging').getLogger(__name__)

        return MockContext()

    @pytest.fixture
    def mock_event(self):
        """模拟事件"""
        return {
            "user_id": "12345",
            "message": "test",
            "message_type": "private"
        }


class TestParseChoiceArgs:
    """测试参数解析"""

    def test_parse_basic_args(self):
        """测试基本参数解析"""
        question, options, choice_count, unique = choice.parse_choice_args("问题 A B C")
        assert question == "问题"
        assert options == ["A", "B", "C"]
        assert choice_count == 1
        assert unique is False

    def test_parse_with_count_flag(self):
        """测试带数量标志的参数解析"""
        question, options, choice_count, unique = choice.parse_choice_args("问题 A B C -n 2")
        assert question == "问题"
        assert options == ["A", "B", "C"]
        assert choice_count == 2
        assert unique is False

    def test_parse_with_unique_flag(self):
        """测试带去重标志的参数解析"""
        question, options, choice_count, unique = choice.parse_choice_args("问题 A B C -u")
        assert question == "问题"
        assert options == ["A", "B", "C"]
        assert choice_count == 1
        assert unique is True

    def test_parse_with_unique_long_flag(self):
        """测试带长去重标志的参数解析"""
        question, options, choice_count, unique = choice.parse_choice_args("问题 A B C --unique")
        assert question == "问题"
        assert options == ["A", "B", "C"]
        assert choice_count == 1
        assert unique is True

    def test_parse_with_both_flags(self):
        """测试同时带数量和去重标志"""
        question, options, choice_count, unique = choice.parse_choice_args("问题 A B C D -n 3 -u")
        assert question == "问题"
        assert options == ["A", "B", "C", "D"]
        assert choice_count == 3
        assert unique is True

    def test_parse_empty_args(self):
        """测试空参数"""
        question, options, choice_count, unique = choice.parse_choice_args("")
        assert question is None
        assert options == []
        assert choice_count == 1
        assert unique is False

    def test_parse_single_option(self):
        """测试单个选项"""
        question, options, choice_count, unique = choice.parse_choice_args("问题 A")
        # 有2个tokens，所以能通过初步检查，但只有一个选项
        assert question == "问题"
        assert options == ["A"]
        assert choice_count == 1
        assert unique is False

    def test_parse_with_duplicate_options(self):
        """测试重复选项（用于加权选择）"""
        question, options, choice_count, unique = choice.parse_choice_args("问题 A A B C")
        assert question == "问题"
        assert options == ["A", "A", "B", "C"]


class TestValidateOptions:
    """测试选项验证"""

    @pytest.fixture
    def mock_context(self):
        class MockContext:
            logger = __import__('logging').getLogger(__name__)
        return MockContext()

    def test_valid_options(self, mock_context):
        """测试有效选项"""
        is_valid, error_msg = choice.validate_options(["A", "B"], 1, mock_context)
        assert is_valid is True
        assert error_msg is None

    def test_too_few_options(self, mock_context):
        """测试选项过少"""
        is_valid, error_msg = choice.validate_options(["A"], 1, mock_context)
        assert is_valid is False
        assert "至少需要" in error_msg

    def test_too_many_options(self, mock_context):
        """测试选项过多"""
        options = [f"option{i}" for i in range(51)]
        is_valid, error_msg = choice.validate_options(options, 1, mock_context)
        assert is_valid is False
        assert "选项过多" in error_msg or "最多支持" in error_msg

    def test_invalid_choice_count_zero(self, mock_context):
        """测试选择数量为零"""
        is_valid, error_msg = choice.validate_options(["A", "B"], 0, mock_context)
        assert is_valid is False
        assert "至少为" in error_msg

    def test_invalid_choice_count_too_high(self, mock_context):
        """测试选择数量过多"""
        is_valid, error_msg = choice.validate_options(["A", "B"], 11, mock_context)
        assert is_valid is False
        assert "选择数量过多" in error_msg or "最多支持" in error_msg

    def test_choice_count_exceeds_options(self, mock_context):
        """测试选择数量超过选项数量"""
        is_valid, error_msg = choice.validate_options(["A", "B"], 5, mock_context)
        # 应该返回有效，但会记录警告日志
        assert is_valid is True


class TestMakeChoice:
    """测试选择逻辑"""

    def test_single_choice(self):
        """测试单个选择"""
        options = ["A", "B", "C"]
        result = choice.make_choice(options, 1, False)
        assert len(result) == 1
        assert result[0] in options

    def test_multiple_choices(self):
        """测试多个选择"""
        options = ["A", "B", "C", "D", "E"]
        result = choice.make_choice(options, 3, False)
        assert len(result) == 3
        for item in result:
            assert item in options

    def test_unique_choices(self):
        """测试去重选择"""
        options = ["A", "B", "C", "D", "E"]
        result = choice.make_choice(options, 3, True)
        assert len(result) == 3
        assert len(set(result)) == 3  # 确保没有重复
        for item in result:
            assert item in options

    def test_choices_with_repetition_allowed(self):
        """测试允许重复的选择"""
        options = ["A", "B", "C", "D", "E"]
        result = choice.make_choice(options, 5, False)
        assert len(result) == 5
        for item in result:
            assert item in options
        # 允许重复时，可能得到重复项

    def test_choice_count_exceeds_options(self):
        """测试选择数量超过选项数量"""
        options = ["A", "B", "C"]
        result = choice.make_choice(options, 10, True)
        # 应该最多返回所有选项
        assert len(result) == 3
        assert set(result) == set(options)

    def test_all_options_selected(self):
        """测试选择所有选项"""
        options = ["A", "B", "C"]
        result = choice.make_choice(options, 3, True)
        assert len(result) == 3
        assert set(result) == set(options)


class TestFormatChoiceResult:
    """测试结果格式化"""

    def test_format_single_choice(self):
        """测试单个选择的格式化"""
        result = choice.format_choice_result("问题", ["A", "B"], ["A"], 2)
        assert "问题" in result
        assert "**A**" in result

    def test_format_multiple_choices(self):
        """测试多个选择的格式化"""
        result = choice.format_choice_result("问题", ["A", "B", "C", "D"], ["A", "B"], 4)
        assert "问题" in result
        assert "**A**" in result
        assert "**B**" in result
        # 应该包含统计信息
        assert "4 个选项" in result or "4" in result

    def test_format_includes_emoji(self):
        """测试结果包含 emoji"""
        result = choice.format_choice_result("问题", ["A"], ["A"], 1)
        # CHOICE_EMOJIS 中的任何一个
        has_emoji = any(emoji in result for emoji in choice.CHOICE_EMOJIS)
        assert has_emoji


class TestChoiceCommand:
    """测试命令处理"""

    @pytest.fixture
    def mock_context(self):
        class MockContext:
            plugin_dir = ROOT / "plugins" / "choice"
            data_dir = ROOT / "plugins" / "choice" / "data"
            logger = __import__('logging').getLogger(__name__)
        return MockContext()

    @pytest.fixture
    def mock_event(self):
        return {
            "user_id": "12345",
            "message": "test",
            "message_type": "private"
        }

    @pytest.mark.asyncio
    async def test_simple_choice(self, mock_context, mock_event):
        """测试简单选择"""
        result = await choice.handle("choice", "吃什么 火锅 烤肉 披萨", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        # 应该包含问题或选中的选项
        assert "吃什么" in result_text or "火锅" in result_text or "烤肉" in result_text or "披萨" in result_text

    @pytest.mark.asyncio
    async def test_help_command(self, mock_context, mock_event):
        """测试帮助命令"""
        result = await choice.handle("choice", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "帮助" in result_text or "随机选择" in result_text or "choice" in result_text.lower()

    @pytest.mark.asyncio
    async def test_chinese_help(self, mock_context, mock_event):
        """测试中文帮助命令"""
        result = await choice.handle("choice", "帮助", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_invalid_args_shows_help(self, mock_context, mock_event):
        """测试无效参数显示帮助"""
        result = await choice.handle("choice", "only_one_option", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_too_few_options_error(self, mock_context, mock_event):
        """测试选项过少错误"""
        result = await choice.handle("choice", "问题 A", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "至少需要" in result_text or "帮助" in result_text

    @pytest.mark.asyncio
    async def test_choice_with_n_flag(self, mock_context, mock_event):
        """测试带 -n 标志的选择"""
        result = await choice.handle("choice", "选择 A B C D -n 2", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_choice_with_u_flag(self, mock_context, mock_event):
        """测试带 -u 标志的去重选择"""
        result = await choice.handle("choice", "选择 A B C D -u", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_weighted_choice_duplicates(self, mock_context, mock_event):
        """测试加权选择（重复选项）"""
        result = await choice.handle("choice", "抽奖 A A A B C", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_empty_string_args(self, mock_context, mock_event):
        """测试空字符串参数"""
        result = await choice.handle("choice", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_whitespace_only_args(self, mock_context, mock_event):
        """测试仅包含空白的参数"""
        result = await choice.handle("choice", "   ", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_unicode_options(self, mock_context, mock_event):
        """测试 Unicode 选项"""
        result = await choice.handle("choice", "选择 OptionA OptionB 你好", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_emoji_options(self, mock_context, mock_event):
        """测试 Emoji 选项"""
        result = await choice.handle("choice", "选择 😀 🎉 ✨", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_special_characters_in_options(self, mock_context, mock_event):
        """测试选项中的特殊字符"""
        result = await choice.handle("choice", "选择 A! B@ C# D$", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0


def test_init():
    """测试插件初始化"""
    choice.init()
    assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
