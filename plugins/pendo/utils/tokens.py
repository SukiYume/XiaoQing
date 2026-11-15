"""命令参数分词：各个子命令共用的一份。"""

from __future__ import annotations

import re
import shlex
from typing import Final

_WORD_APOSTROPHE_RE      : Final = re.compile(r"(?<=\w)'(?=\w)")
_WORD_APOSTROPHE_SENTINEL: Final = "\ufdd0"


def shell_tokens(text: str, what: str) -> list[str]:
    """按 shell 风格切词：成对的引号括住带空格的一段，词里的撇号（``don't``）是普通字符。

    ``what`` 是这段输入在报错里的叫法（“搜索参数”“待办参数”）；引号没闭合时抛 ``ValueError``。
    """

    if _WORD_APOSTROPHE_SENTINEL in text:
        raise ValueError(f"{what}包含不支持的字符")
    # shlex 会把词里的自然撇号当作引号；先换成哨兵，分词后再原样恢复。
    protected = _WORD_APOSTROPHE_RE.sub(_WORD_APOSTROPHE_SENTINEL, text)
    try:
        tokens = shlex.split(protected, comments=False, posix=True)
    except ValueError as exc:
        raise ValueError(f"{what}中的引号未闭合") from exc
    return [token.replace(_WORD_APOSTROPHE_SENTINEL, "'") for token in tokens]
