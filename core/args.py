"""
命令参数解析模块

提供灵活的命令参数解析功能。
"""

import shlex
from dataclasses import dataclass, field

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

def tokenize(text: str) -> list[str]:
    """
    分词：将输入文本分割为 token 列表。

    支持引号包裹的字符串作为单个 token。
    """
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        # 引号不匹配时退回简单分割
        return text.split()

def parse(raw: str) -> ParsedArgs:
    """
    解析命令参数。

    支持:
    - 位置参数: arg1 arg2
    - 短选项: -f value 或 -f
    - 长选项: --option=value 或 --option value 或 --flag

    返回: ParsedArgs 对象
    """
    tokens_list = tokenize(raw)
    args: list[str] = []
    options: dict[str, str] = {}
    idx = 0

    while idx < len(tokens_list):
        token = tokens_list[idx]

        if token.startswith("--") and len(token) > 2:
            # 长选项
            key, eq, value = token[2:].partition("=")
            if eq:
                options[key] = value
            elif idx + 1 < len(tokens_list) and not tokens_list[idx + 1].startswith("-"):
                options[key] = tokens_list[idx + 1]
                idx += 1
            else:
                options[key] = "true"
        elif token.startswith("-") and len(token) > 1 and not token[1:].isdigit():
            # 短选项 (排除负数如 -1)
            key = token[1:]
            if idx + 1 < len(tokens_list) and not tokens_list[idx + 1].startswith("-"):
                options[key] = tokens_list[idx + 1]
                idx += 1
            else:
                options[key] = "true"
        else:
            args.append(token)

        idx += 1

    return ParsedArgs(raw=raw, tokens=args, options=options)

def parse_kv(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """
    解析键值对（向后兼容）。

    推荐使用 parse() 函数替代。
    """
    # 复用 parse() 的逻辑
    raw = " ".join(tokens)
    result = parse(raw)
    return result.tokens, result.options

__all__ = ["ParsedArgs", "tokenize", "parse", "parse_kv"]
