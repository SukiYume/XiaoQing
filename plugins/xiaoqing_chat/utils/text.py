"""通用文本截断工具。"""

from __future__ import annotations


def clip_text(text: str, max_chars: int) -> str:
    """去掉首尾空白后截到最多 ``max_chars`` 个字符，超长时以省略号收尾。"""

    value = str(text or "").strip()
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"
