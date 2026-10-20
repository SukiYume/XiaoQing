"""识别群聊中低信息消息和面向所有成员的参与邀请。"""

from __future__ import annotations

import re

from .constants import GENERIC_MEDIA_LABELS, is_question

_DIRECTED_OTHER_RE = re.compile(
    r"^\s*(?:@(?!全体成员)\S{1,32}|阿[\u4e00-\u9fff]{1,2}|小[\u4e00-\u9fff]{1,2}|"
    r"老[\u4e00-\u9fff]{1,2}|[\u4e00-\u9fff]{1,3}(?:哥|姐|老师|同学))"
    r"(?:(?:\s*[，,：:、]\s*|\s+)(?:你|你们)?|(?:你|你们))"
)
# 句首直接 @ 某人（含只 @ 不说话），和点名一样是在叫那个人。
_LEADING_MENTION_RE = re.compile(r"^\s*@(?!全体成员)[^\s@，,：:]{1,32}(?:[\s，,：:]|$)")
# 句中任意位置 @ 了某人；前面紧跟邮箱用户名字符的 @ 不算。
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9._%+-])@(?!全体成员)[^\s@，,。.!！?？:：]{1,32}")
# “群里”只有接着问人时才是邀请；“现在群里只有咱俩能用”这类陈述不算。
_GROUP_INVITATION_RE = re.compile(
    r"(?:你们|大家|各位|@全体成员|有没有人|有人(?:想|愿意|能)|谁(?:来|能|想|愿意)|"
    r"群里(?:有没有|有人|谁|的?(?:各位|大家|朋友|兄弟|姐妹|老师)))"
)
# 面向全群的线索只看短消息；大段说明、报告即使带问句或“大家”，也交给规划器读完再判断。
_PARTICIPATION_CUE_MAX_CHARS = 80
_OPENING_BANTER_RE           = re.compile(r"(?:^|[，,。！？!?\s])(?:我宣布|我提议|我建议|我赌|我站\S{0,8}党)")
# “发个狗头”“来张梗图”这类没点名的发图请求，谁接都行，群友通常会顺手甩一个。
_MEDIA_REQUEST_RE = re.compile(
    r"^(?:快|再)?(?:来|发|整|甩|上)(?:个|张|一个|一张|几张|点)"
    r"[\u4e00-\u9fffA-Za-z]{0,6}(?:狗头|表情|表情包|图|梗图|emoji)"
)
# 连用感叹号的自述（喜报、崩溃、惊呼）是在等群友的反应，和开放提问一样值得接。
_EXCITED_SHARE_RE = re.compile(r"[！!]{2,}")
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


def is_group_turn_directed_to_other(text: str) -> bool:
    """识别句首明确叫其他群友的轮次，避免机器人抢答。"""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return bool(value and (_DIRECTED_OTHER_RE.search(value) or _LEADING_MENTION_RE.search(value)))


def mentions_other_member(text: str) -> bool:
    """消息里 @ 了某位群友；@全体成员 不算。"""

    return _MENTION_RE.search(str(text or "")) is not None


def is_group_repeat_chain(text: str, previous_user_texts: list[str]) -> bool:
    """群友接连发同一句短消息时形成复读队形，跟一句也是群聊里常见的接梗。"""

    def key(value: str) -> str:
        return re.sub(r"[\s，,。.!！?？~～…]+", "", str(value or ""))

    current = key(text)
    if not current or len(current) > 30 or not previous_user_texts:
        return False
    return key(previous_user_texts[-1]) == current


def classify_group_participation_cue(text: str) -> str:
    """识别高置信度、明确邀请群成员参与的消息。"""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not 4 <= len(value) <= _PARTICIPATION_CUE_MAX_CHARS:
        return ""
    if is_group_turn_directed_to_other(value) or mentions_other_member(value):
        return ""
    if _GROUP_INVITATION_RE.search(value):
        return "group_invitation"
    if _MEDIA_REQUEST_RE.search(value):
        return "media_request"
    if is_question(value):
        return "open_question"
    if _OPENING_BANTER_RE.search(value):
        return "opening_banter"
    if _EXCITED_SHARE_RE.search(value) and len(re.sub(r"[\W_]+", "", value)) >= 4:
        return "excited_share"
    return ""
