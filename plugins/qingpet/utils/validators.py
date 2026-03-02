import re
from datetime import datetime
from typing import Tuple, Optional

from .constants import DEFAULT_SENSITIVE_WORDS


def validate_pet_name(name: str, sensitive_words: list = None) -> Tuple[bool, str]:
    """校验宠物名字合法性，包括敏感词过滤"""
    if not name or not name.strip():
        return False, "宠物名字不能为空"
    
    if len(name) > 20:
        return False, "宠物名字不能超过20个字符"
    
    if re.search(r'[<>"\'\\]', name):
        return False, "宠物名字包含非法字符"
    
    # 敏感词过滤（Issue #51）
    words = sensitive_words if sensitive_words is not None else DEFAULT_SENSITIVE_WORDS
    for word in words:
        if word and word.lower() in name.lower():
            return False, "宠物名字包含不允许使用的词汇"
    
    return True, ""


def validate_item_amount(amount: int) -> Tuple[bool, str]:
    """校验道具购买数量"""
    if amount <= 0:
        return False, "数量必须大于0"
    
    if amount > 99:
        return False, "单次购买数量不能超过99"
    
    return True, ""


def validate_cooling(last_time: Optional[datetime], cooldown_seconds: int) -> Tuple[bool, int]:
    """
    校验冷却时间。使用 .total_seconds() 而非 .seconds 以正确处理超过1天的时间差。
    
    Returns:
        (is_cooled, remaining_seconds)
    """
    if last_time is None:
        return True, 0
    
    elapsed = (datetime.now() - last_time).total_seconds()
    if elapsed < cooldown_seconds:
        return False, int(cooldown_seconds - elapsed)
    
    return True, 0


def validate_sensitive_content(text: str, sensitive_words: list = None) -> Tuple[bool, str]:
    """检查文本是否包含敏感词"""
    words = sensitive_words if sensitive_words is not None else DEFAULT_SENSITIVE_WORDS
    for word in words:
        if word and word.lower() in text.lower():
            return False, f"内容包含不允许使用的词汇"
    return True, ""