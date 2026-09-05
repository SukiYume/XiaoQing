"""
命令参数解析模块

提供灵活的命令参数解析功能。
"""

import re
import shlex
from dataclasses import dataclass, field

_SHORT_OPTION_PATTERN = re.compile(r"-[A-Za-z]\Z")
_LONG_OPTION_PATTERN  = re.compile(r"--[A-Za-z][A-Za-z0-9_-]*(?:=.*)?\Z")
_INTEGER_PATTERN      = re.compile(r"[+-]?[0-9]+\Z")
FLAG_VALUE            = "true"


def _is_option_token(token: str) -> bool:
    """只接受无歧义的 ASCII 短选项或长选项形态。"""

    return bool(_SHORT_OPTION_PATTERN.fullmatch(token) or _LONG_OPTION_PATTERN.fullmatch(token))


@dataclass
class ParsedArgs:
    """解析后的命令参数"""

    raw: str
    tokens: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)

    def get(self, index: int, default: str = "") -> str:
        """获取指定位置的参数"""
        if 0 <= index < len(self.tokens):
            return self.tokens[index]
        return default

    def opt(self, key: str, default: str = "") -> str:
        """获取指定选项的值"""
        return self.options.get(key, default)

    def has(self, key: str) -> bool:
        """检查是否存在指定选项"""
        return key in self.options

    def rest(self, start: int = 0) -> str:
        """获取从指定位置开始的所有参数拼接"""
        return " ".join(self.tokens[start:])

    @property
    def first(self) -> str:
        """第一个参数"""
        return self.get(0)

    @property
    def second(self) -> str:
        """第二个参数"""
        return self.get(1)

    def __len__(self) -> int:
        return len(self.tokens)

    def __bool__(self) -> bool:
        return bool(self.raw.strip())


def tokenize(text: str, *, strict: bool = False) -> list[str]:
    """
    分词：将输入文本分割为 token 列表。

    支持引号包裹的字符串作为单个 token。默认将未闭合引号按空白分割；
    需要严格命令语法的调用方可传入 ``strict=True`` 保留错误。
    """
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        if strict:
            raise
        # A bare quote is common in free text (for example an inch mark) and
        # should not turn command parsing into an exception path.
        return text.split()


def quote_token(value: str) -> str:
    """把一个值编码为可由 :func:`tokenize` 无损还原的单个可读 token。"""

    if not isinstance(value, str):
        raise TypeError("token value must be a string")
    if value and not any(character.isspace() or character in "'\"\\" for character in value):
        return value
    return shlex.quote(value)


def parse_int(
    text: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    """Parse one strict ASCII integer token, returning ``None`` when invalid.

    The lexical check keeps command protocols deterministic: Unicode digits,
    whitespace and Python-only underscore separators are not accepted.  The
    conversion still treats ``ValueError`` as ordinary invalid input, including
    Python's protection against excessively long integer strings.
    """

    if _INTEGER_PATTERN.fullmatch(text) is None:
        return None
    try:
        value = int(text, 10)
    except ValueError:
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def parse(raw: str) -> ParsedArgs:
    """
    解析命令参数。

    支持:
    - 位置参数: arg1 arg2
    - 短选项: -f value 或 -f
      仅单个 ASCII 字母可作为短选项；-abc、-1+2、-3σ 均是位置参数
    - 长选项: --option=value 或 --option value 或 --flag
    - `--` 终止选项解析；其后的 token 全部是位置参数

    返回: ParsedArgs 对象
    """
    args, options = _parse_tokens(tokenize(raw))
    return ParsedArgs(raw=raw, tokens=args, options=options)


def _parse_tokens(tokens_list: list[str]) -> tuple[list[str], dict[str, str]]:
    """Parse caller-owned token boundaries without joining and tokenizing again."""
    args: list[str]         = []
    options: dict[str, str] = {}
    idx                     = 0
    options_enabled         = True

    while idx < len(tokens_list):
        token = tokens_list[idx]

        if options_enabled and token == "--":
            options_enabled = False
        elif options_enabled and _LONG_OPTION_PATTERN.fullmatch(token):
            # 长选项
            key, eq, value = token[2:].partition("=")
            if eq:
                options[key] = value
            elif (
                idx + 1 < len(tokens_list)
                and tokens_list[idx + 1] != "--"
                and not _is_option_token(tokens_list[idx + 1])
            ):
                options[key] = tokens_list[idx + 1]
                idx += 1
            else:
                options[key] = FLAG_VALUE
        elif options_enabled and _SHORT_OPTION_PATTERN.fullmatch(token):
            # 单字母短选项
            key = token[1:]
            if (
                idx + 1 < len(tokens_list)
                and tokens_list[idx + 1] != "--"
                and not _is_option_token(tokens_list[idx + 1])
            ):
                options[key] = tokens_list[idx + 1]
                idx += 1
            else:
                options[key] = FLAG_VALUE
        else:
            args.append(token)

        idx += 1

    return args, options


__all__ = ["FLAG_VALUE", "ParsedArgs", "parse", "parse_int", "quote_token", "tokenize"]
