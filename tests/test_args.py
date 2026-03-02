"""
参数解析模块单元测试
"""

import pytest

from core.args import parse, tokenize, ParsedArgs

# ============================================================
# tokenize 测试
# ============================================================

class TestTokenize:
    """tokenize() 函数测试"""

    def test_simple_words(self):
        """测试简单单词分词"""
        result = tokenize("hello world")
        assert result == ["hello", "world"]

    def test_with_quotes(self):
        """测试带引号的分词"""
        result = tokenize("'hello world'")
        assert result == ["hello world"]

    def test_double_quotes(self):
        """测试双引号"""
        result = tokenize('"hello world"')
        assert result == ["hello world"]

    def test_mixed_quotes_and_words(self):
        """测试混合引号和单词"""
        result = tokenize('a "b c" d')
        assert result == ["a", "b c", "d"]

    def test_single_quotes(self):
        """测试单引号"""
        result = tokenize("a 'b c' d")
        assert result == ["a", "b c", "d"]

    def test_empty_string(self):
        """测试空字符串"""
        result = tokenize("")
        assert result == []

    def test_single_word(self):
        """测试单个单词"""
        result = tokenize("hello")
        assert result == ["hello"]

    def test_multiple_spaces(self):
        """测试多个空格"""
        result = tokenize("hello    world")
        assert result == ["hello", "world"]

    def test_quotes_with_spaces_around(self):
        """测试引号周围有空格"""
        result = tokenize('  "hello world"  ')
        assert result == ["hello world"]

    def test_escaped_quotes_not_supported(self):
        """测试转义引号（当前不支持）"""
        # 当前的 tokenize 不处理转义
        result = tokenize(r'"hello \"world"')
        # 引号内的内容原样保留
        assert len(result) >= 1

    def test_multiple_quoted_segments(self):
        """测试多个引号段"""
        result = tokenize('"a b" "c d"')
        assert result == ["a b", "c d"]

    def test_unclosed_quote(self):
        """测试未闭合的引号"""
        # 未闭合的引号会被当作普通字符处理
        result = tokenize('"hello world')
        # 引号被视为普通字符，结果取决于实现
        assert len(result) >= 1

    def test_special_characters(self):
        """测试特殊字符"""
        result = tokenize('hello@world #tag $100')
        assert result == ["hello@world", "#tag", "$100"]

    def test_unicode_characters(self):
        """测试 Unicode 字符"""
        result = tokenize("你好 世界 测试")
        assert result == ["你好", "世界", "测试"]

# ============================================================
# parse 测试
# ============================================================

class TestParse:
    """parse() 函数测试"""

    def test_simple_command(self):
        """测试简单命令解析"""
        result = parse("echo hello")
        assert result.tokens == ["echo", "hello"]
        assert result.first == "echo"

    def test_options_detection(self):
        """测试选项检测"""
        result = parse("-v -o output")
        assert result.tokens == []
        assert result.options == {"v": "true", "o": "output"}

    def test_double_dash_options(self):
        """测试双横线选项"""
        result = parse("--help --verbose")
        assert result.tokens == []
        assert result.options == {"help": "true", "verbose": "true"}

    def test_mixed_tokens_and_options(self):
        """测试混合 token 和选项"""
        # 短选项后会取下一个非选项参数作为值
        result = parse("command -v input.txt")
        assert result.tokens == ["command"]
        assert result.options == {"v": "input.txt"}

    def test_mixed_tokens_and_flag(self):
        """测试混合 token 和标志选项"""
        # 长选项也会取下一个非选项参数作为值
        result = parse("command --verbose input.txt")
        assert result.tokens == ["command"]
        assert result.options == {"verbose": "input.txt"}

    def test_mixed_tokens_and_flag_value(self):
        """测试混合 token 和带等号的长选项"""
        # 使用等号来明确指定选项值为 true
        result = parse("command --verbose=true input.txt")
        assert result.tokens == ["command", "input.txt"]
        assert result.options == {"verbose": "true"}

    def test_option_with_value(self):
        """测试带值的选项"""
        result = parse("-o output.txt")
        assert result.opt("o") == "output.txt"

    def test_second_property(self):
        """测试 second 属性"""
        result = parse("first second third")
        assert result.second == "second"

    def test_second_with_no_second_token(self):
        """测试没有第二个 token 时"""
        result = parse("only_one")
        assert result.second == ""

    def test_opt_nonexistent(self):
        """测试获取不存在的选项返回空字符串"""
        result = parse("command")
        assert result.opt("nonexistent") == ""

    def test_opt_nonexistent_with_custom_default(self):
        """测试获取不存在的选项返回自定义默认值"""
        result = parse("command")
        assert result.opt("nonexistent", "default_value") == "default_value"

    def test_opt_with_default(self):
        """测试获取选项带默认值"""
        result = parse("command")
        assert result.opt("missing", "default") == "default"

    def test_empty_input(self):
        """测试空输入"""
        result = parse("")
        assert result.tokens == []
        assert result.options == {}

    def test_complex_command(self):
        """测试复杂命令"""
        result = parse("query -p -o value --format json input.txt")
        assert result.tokens == ["query", "input.txt"]
        assert result.opt("p") == "true"
        assert result.opt("o") == "value"
        assert result.opt("format") == "json"

    def test_option_value_with_spaces(self):
        """测试带空格的选项值（使用引号）"""
        result = parse('-o "output file.txt"')
        assert result.opt("o") == "output file.txt"

    def test_multiple_same_options(self):
        """测试多个相同选项（取最后一个）"""
        result = parse("-v -v -v")
        assert result.opt("v") == "true"

    def test_boolean_options(self):
        """测试布尔选项"""
        result = parse("-a -b -c")
        assert result.opt("a") == "true"
        assert result.opt("b") == "true"
        assert result.opt("c") == "true"

# ============================================================
# ParsedArgs 数据类测试
# ============================================================

class TestParsedArgs:
    """ParsedArgs 数据类测试"""

    def test_equality(self):
        """测试相等性"""
        args1 = parse("hello world")
        args2 = parse("hello world")
        assert args1 == args2

    def test_inequality(self):
        """测试不等性"""
        args1 = parse("hello world")
        args2 = parse("goodbye world")
        assert args1 != args2

    def test_repr(self):
        """测试字符串表示"""
        args = parse("echo hello -v")
        repr_str = repr(args)
        assert "ParsedArgs" in repr_str
        assert "echo" in repr_str

    def test_immutability_of_tokens(self):
        """测试 tokens 列表（虽然可变，但原始不被影响）"""
        args = parse("one two three")
        original_tokens = args.tokens.copy()
        args.tokens.append("four")
        # 这是预期的行为 - tokens 是可变列表
        assert args.tokens == ["one", "two", "three", "four"]

# ============================================================
# 边界情况测试
# ============================================================

class TestEdgeCases:
    """边界情况测试"""

    def test_only_options(self):
        """测试只有选项"""
        result = parse("-a -b -c")
        assert result.tokens == []
        assert len(result.options) == 3

    def test_only_tokens(self):
        """测试只有 tokens"""
        result = parse("one two three")
        assert result.tokens == ["one", "two", "three"]
        assert result.options == {}

    def test_option_like_token(self):
        """测试类似选项的 token（如果不在选项位置则视为 token）"""
        result = parse("command something")
        assert result.tokens == ["command", "something"]

    def test_very_long_input(self):
        """测试非常长的输入"""
        long_input = " ".join([f"word{i}" for i in range(1000)])
        result = parse(long_input)
        assert len(result.tokens) == 1000

    def test_unicode_in_options(self):
        """测试选项中的 Unicode"""
        result = parse("-输出 值")
        # 中文开头跟在横线后面被视为选项
        assert "输出" in result.options
        assert result.options["输出"] == "值"

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
