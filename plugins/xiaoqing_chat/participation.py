"""识别只有协议占位的群聊消息。"""

from __future__ import annotations

import re

from .constants import GENERIC_MEDIA_LABELS

_QQ_FACE_TOKEN_RE = re.compile(
    r"(?:\[CQ:face(?:,[^\]\r\n]*)?\]|\[QQ表情：[^\]\r\n]+\])",
    re.IGNORECASE,
)


_MEDIA_MARKER_RE = re.compile(r"\[(图片|表情包)：([^\]\n]{1,400})\]")


def _content_free_media(match: re.Match[str]) -> str:
    label = re.split(r"[；;]", match.group(2), maxsplit=1)[0].strip()
    return "" if label in GENERIC_MEDIA_LABELS else match.group(0)


def is_low_information_group_turn(text: str) -> bool:
    """识别只有协议表情码、无内容媒体和标点的群消息，避免为它启动完整规划链。"""

    value = str(text or "").strip()
    if not value:
        return False
    stripped = _MEDIA_MARKER_RE.sub(_content_free_media, value)
    if stripped == value and _QQ_FACE_TOKEN_RE.search(value) is None:
        return False
    residue = _QQ_FACE_TOKEN_RE.sub("", stripped)
    residue = re.sub(r"[\s，,。.!！?？~～…、:：;；]+", "", residue)
    return not residue
