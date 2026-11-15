"""命令层共用的参数拆分与目标 QQ 解析。"""

import re

_TARGET_QQ = re.compile(r"@?(\d+)")


def split_first_word(text: str) -> tuple[str, str]:
    """把参数拆成首个词和其余部分；参数为空时返回 ``("", "")``。"""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def parse_target_qq(text: str) -> str | None:
    """整段文本恰好是 ``@QQ号`` 或裸 QQ 号时返回号码，否则返回 ``None``。"""
    match = _TARGET_QQ.fullmatch(text.strip())
    return match.group(1) if match else None
