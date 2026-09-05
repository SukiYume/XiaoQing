"""Pendo 内部 UUID 与用户可见短标识的统一规则。"""

from __future__ import annotations

import re
import uuid

INTERNAL_ID_LENGTH = 32
PUBLIC_ID_LENGTH   = 8

_UUID_HEX_RE  = re.compile(r"[0-9a-f]{32}\Z", re.IGNORECASE)
_PUBLIC_ID_RE = re.compile(r"[0-9a-f]{8}\Z", re.IGNORECASE)


def new_internal_id() -> str:
    """生成无连字符的完整 UUID，供所有新建业务实体使用。"""

    return uuid.uuid4().hex


def is_canonical_internal_id(value: object) -> bool:
    """判断值是否为当前规范的 32 位 UUID hex。"""

    return _UUID_HEX_RE.fullmatch(str(value or "").strip()) is not None


def public_id(value: object) -> str:
    """把规范内部 UUID 转成 8 位用户可见标识。"""

    text = str(value or "").strip()
    if not text:
        return ""

    if _UUID_HEX_RE.fullmatch(text):
        return text[:PUBLIC_ID_LENGTH].lower()
    return text


def is_public_id_reference(value: object) -> bool:
    """判断输入是否可能是由 :func:`public_id` 生成的短引用。"""

    return _PUBLIC_ID_RE.fullmatch(str(value or "").strip()) is not None


def public_id_matches(stored_id: object, reference: object) -> bool:
    """按大小写不敏感方式判断短引用是否唯一指向一个存储 ID。"""

    return public_id(stored_id).casefold() == str(reference or "").strip().casefold()
