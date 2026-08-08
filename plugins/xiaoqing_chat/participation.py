"""识别群聊中低信息消息和面向所有成员的参与邀请。"""

from __future__ import annotations

import re

from .constants import is_question

_DIRECTED_OTHER_RE = re.compile(
    r"^\s*(?:@\S{1,32}|阿[\u4e00-\u9fff]{1,2}|小[\u4e00-\u9fff]{1,2}|"
    r"老[\u4e00-\u9fff]{1,2}|[\u4e00-\u9fff]{1,3}(?:哥|姐|老师|同学))"
    r"(?:(?:\s*[，,：:、]\s*|\s+)(?:你|你们)?|(?:你|你们))"
)
_GROUP_INVITATION_RE = re.compile(
    r"(?:你们|大家|各位|群里|有没有人|有人(?:想|愿意|能)|谁(?:来|能|想|愿意))"
)
_OPENING_BANTER_RE = re.compile(r"(?:^|[，,。！？!?\s])(?:我宣布|我提议|我建议|我赌|我站\S{0,8}党)")
_QQ_FACE_TOKEN_RE = re.compile(
    r"(?:\[CQ:face(?:,[^\]\r\n]*)?\]|\[QQ表情：[^\]\r\n]+\])",
    re.IGNORECASE,
)


def is_low_information_group_turn(text: str) -> bool:
    """识别只有协议表情码和标点的群消息，避免为它启动完整规划链。"""

    value = str(text or "").strip()
    if not value or _QQ_FACE_TOKEN_RE.search(value) is None:
        return False
    residue = _QQ_FACE_TOKEN_RE.sub("", value)
    residue = re.sub(r"[\s，,。.!！?？~～…、:：;；]+", "", residue)
    return not residue


def is_group_turn_directed_to_other(text: str) -> bool:
    """识别句首明确叫其他群友的轮次，避免机器人抢答。"""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return bool(value and _DIRECTED_OTHER_RE.search(value))


def classify_group_participation_cue(text: str) -> str:
    """识别高置信度、明确邀请群成员参与的消息。"""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) < 4 or is_group_turn_directed_to_other(value):
        return ""
    if _GROUP_INVITATION_RE.search(value):
        return "group_invitation"
    if is_question(value):
        return "open_question"
    if _OPENING_BANTER_RE.search(value):
        return "opening_banter"
    return ""
