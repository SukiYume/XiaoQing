"""校验宠物名称、用户文本、道具数量和操作冷却。"""

from collections.abc import Sequence
from datetime import datetime

from .constants import DEFAULT_SENSITIVE_WORDS
from .time import utc_now


def validate_sensitive_content(
    text: str,
    sensitive_words: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """检查文本是否包含默认或调用方补充的敏感词。"""
    normalized_text = text.casefold()
    words           = dict.fromkeys((*DEFAULT_SENSITIVE_WORDS, *(sensitive_words or ())))
    for word in words:
        if word and word.casefold() in normalized_text:
            return False, "内容包含不允许使用的词汇"
    return True, ""


def validate_pet_name(
    name: str,
    sensitive_words: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """校验宠物名字是否非空、长度合规且适合公开展示。"""
    if not name or not name.strip():
        return False, "宠物名字不能为空"

    if len(name) > 20:
        return False, "宠物名字不能超过20个字符"

    if any(character in name for character in "<>\"'\\"):
        return False, "宠物名字包含非法字符"

    is_allowed = validate_sensitive_content(name, sensitive_words)[0]
    if not is_allowed:
        return False, "宠物名字包含不允许使用的词汇"

    return True, ""


def validate_item_amount(amount: int) -> tuple[bool, str]:
    """校验单次道具操作数量。"""
    if amount <= 0:
        return False, "数量必须大于0"

    if amount > 99:
        return False, "单次购买数量不能超过99"

    return True, ""


def validate_cooling(last_time: datetime | None, cooldown_seconds: int) -> tuple[bool, int]:
    """校验冷却时间。

    使用 ``total_seconds()`` 正确处理跨天时间差；返回是否结束冷却及剩余秒数。
    """
    if last_time is None:
        return True, 0

    elapsed = (utc_now() - last_time).total_seconds()
    if elapsed < cooldown_seconds:
        return False, int(cooldown_seconds - elapsed)

    return True, 0
