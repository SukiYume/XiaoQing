"""参数解析模块单元测试。"""

import pytest

from core.args import chinese_number, parse, parse_int, quote_token, tokenize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 0), ("+12", 12), ("-7", -7), ("0042", 42)],
)
def test_parse_int_accepts_strict_ascii_integer_tokens(raw: str, expected: int) -> None:
    assert parse_int(raw) == expected


@pytest.mark.parametrize("raw", ["", " 1", "1 ", "1_0", "²", "٣", "１２", "1.0"])
def test_parse_int_rejects_non_protocol_integer_tokens(raw: str) -> None:
    assert parse_int(raw) is None


def test_parse_int_applies_inclusive_range() -> None:
    assert parse_int("5", minimum=5, maximum=5) == 5
    assert parse_int("4", minimum=5) is None
    assert parse_int("6", maximum=5) is None


# ============================================================
# tokenize 测试
# ============================================================


class TestTokenize:
    """tokenize() 函数测试"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("hello world", ["hello", "world"], id="simple-words"),
            pytest.param("hello", ["hello"], id="single-word"),
            pytest.param("", [], id="empty"),
            pytest.param("hello    world", ["hello", "world"], id="multiple-spaces"),
            pytest.param("'hello world'", ["hello world"], id="single-quoted"),
            pytest.param('"hello world"', ["hello world"], id="double-quoted"),
            pytest.param('a "b c" d', ["a", "b c", "d"], id="double-quoted-between-words"),
            pytest.param("a 'b c' d", ["a", "b c", "d"], id="single-quoted-between-words"),
            pytest.param('  "hello world"  ', ["hello world"], id="spaces-around-quotes"),
            pytest.param('"a b" "c d"', ["a b", "c d"], id="multiple-quoted-segments"),
            pytest.param(
                "hello@world #tag $100", ["hello@world", "#tag", "$100"], id="special-characters"
            ),
            pytest.param("你好 世界 测试", ["你好", "世界", "测试"], id="unicode"),
        ],
    )
    def test_splits_on_whitespace_and_keeps_quoted_segments(self, text, expected):
        assert tokenize(text) == expected

    def test_escaped_quotes(self):
        """引号中的转义引号由统一 shlex 词法层处理。"""
        result = tokenize(r'"hello \"world"')
        assert result == ['hello "world']

    def test_unclosed_quote(self):
        """未闭合引号按空白回退，保留用户输入而不抛异常。"""
        assert tokenize('"hello world') == ['"hello', "world"]

    def test_unclosed_quote_can_be_strict(self):
        with pytest.raises(ValueError, match="No closing quotation"):
            tokenize('"hello world', strict=True)

    def test_parse_unclosed_quote_keeps_free_text_command_arguments(self):
        result = parse('search "fast radio burst')

        assert result.tokens == ["search", '"fast', "radio", "burst"]
        assert result.options == {}

    @pytest.mark.parametrize(
        "value",
        ["", "乳白", "fast radio burst", "Barnard's", 'a "quoted" value', r"a\b"],
    )
    def test_quote_token_round_trips_one_value(self, value):
        assert tokenize(quote_token(value), strict=True) == [value]

    def test_quote_token_rejects_non_string_values(self):
        with pytest.raises(TypeError, match="must be a string"):
            quote_token(1)


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

    def test_mixed_tokens_and_flag_value(self):
        """测试混合 token 和带等号的长选项"""
        # 使用等号来明确指定选项值为 true
        result = parse("command --verbose=true input.txt")
        assert result.tokens == ["command", "input.txt"]
        assert result.options == {"verbose": "true"}

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

    def test_combined_short_option_is_positional_text(self):
        """短选项只允许一个 ASCII 字母。"""
        result = parse("-abc")
        assert result.tokens == ["-abc"]
        assert result.options == {}

    def test_negative_numbers_are_tokens(self):
        """测试独立负数参数不会被当成选项"""
        result = parse("-1 -2.5 value")
        assert result.tokens == ["-1", "-2.5", "value"]
        assert result.options == {}

    def test_negative_number_can_be_option_value(self):
        """测试负数可以作为选项值"""
        result = parse("--offset -1 --scale -1.5")
        assert result.opt("offset") == "-1"
        assert result.opt("scale") == "-1.5"

    def test_option_terminator_keeps_all_following_tokens_positional(self):
        result = parse("-v -- -x --mode=step -1+2")

        assert result.options == {"v": "true"}
        assert result.tokens == ["-x", "--mode=step", "-1+2"]

    @pytest.mark.parametrize(
        "raw",
        ["-1+2", "-12:34:56", "-3σ", "-输出", "--3d", "--输出"],
    )
    def test_ambiguous_dash_prefixed_text_is_not_an_option(self, raw: str):
        result = parse(raw)

        assert result.tokens == [raw]
        assert result.options == {}

    def test_ascii_long_option_may_use_hyphen_underscore_and_equals(self):
        result = parse("--output-format=json --cache_key value")

        assert result.options == {"output-format": "json", "cache_key": "value"}


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


# ============================================================
# 边界情况测试
# ============================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_unicode_after_dash_is_positional(self):
        """非 ASCII 选项名不会把后一个位置参数吞成值。"""
        result = parse("-输出 值")
        assert result.tokens == ["-输出", "值"]
        assert result.options == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("零", 0),
        ("两", 2),
        ("十", 10),
        ("十二", 12),
        ("二十三", 23),
        ("八百", 800),
        ("三百五十", 350),
        ("一百零五", 105),
        ("一千零五十", 1050),
        ("十二万", 120000),
        # 末尾省掉单位的口语读法
        ("一百二", 120),
        ("一千五", 1500),
        ("三万五", 35000),
    ],
)
def test_chinese_number_reads_spoken_numerals(text: str, expected: int) -> None:
    assert chinese_number(text) == expected


@pytest.mark.parametrize("text", ["", "半", "12", "一半", "十点", "１２", "²"])
def test_chinese_number_rejects_anything_else(text: str) -> None:
    assert chinese_number(text) is None
