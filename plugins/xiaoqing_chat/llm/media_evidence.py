"""把实际消息附件状态传入生成、审查和验收边界。"""

import re
from typing import Any

from ..constants import GENERIC_MEDIA_LABELS

# 入站媒体在对话文本里渲染成 ``[图片：摘要]`` 这类标记，第 1 组为媒体类型，第 2 组为摘要。
INBOUND_MEDIA_MARKER_RE = re.compile(r"\[(图片|表情包|QQ表情)：([^\]\n]{1,400})\]")

_VISIBLE_SPEECH_RES = tuple(
    re.compile(pattern)
    for pattern in (
        r"写着[“\"]([^”\"\]]{1,80})[”\"]",
        r"文字(?:内容)?(?:是|为)[“\"]([^”\"\]]{1,80})[”\"]",
        r"配文(?:字)?(?:是|为)?[“\"]([^”\"\]]{1,80})[”\"]",
    )
)


def marker_visible_speech(marker: str) -> str:
    """提取媒体摘要里图上可见的文字（写着/文字是/配文）；没有时返回空串。"""
    text = str(marker or "").strip()
    for pattern in _VISIBLE_SPEECH_RES:
        match = pattern.search(text)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def event_has_current_image(event: dict[str, Any]) -> bool:
    """只接受结构化入站附件与已规范化媒体段，文字中的图片称呼保持为文本。"""
    message = event.get("message")
    if isinstance(message, list) and any(
        isinstance(part, dict) and part.get("type") in {"image", "mface"} for part in message
    ):
        return True
    parts = event.get("_xc_effective_user_parts")
    return isinstance(parts, (list, tuple)) and any(
        isinstance(part, dict) and part.get("kind") in {"image", "emoji"} for part in parts
    )


def event_media_all_unreadable(event: dict[str, Any]) -> bool:
    """当前事件带有媒体，但每一项都只有通用占位标签、没有读出的画面内容。"""
    parts = event.get("_xc_effective_user_parts")
    if not isinstance(parts, (list, tuple)):
        return False
    media = [
        part for part in parts if isinstance(part, dict) and part.get("kind") in {"image", "emoji"}
    ]
    if not media:
        return False

    def readable(part: dict[str, Any]) -> bool:
        description = str(part.get("description", "") or "").strip()
        label = re.split(r"[；;]", description, maxsplit=1)[0].strip()
        return bool(description) and label not in GENERIC_MEDIA_LABELS

    return not any(readable(part) for part in media)


def history_has_image(history: Any) -> bool:
    """历史证据来自结构化媒体记录；纯文本图片标签保持为用户文字。"""
    return any(
        isinstance(part, dict) and part.get("kind") in {"image", "emoji"}
        for message in history
        for part in message.parts
    )


def image_evidence_block(attached: bool | None, *, history_available: bool = False) -> str:
    """说明可用证据来源，具体回答方式由当前问题决定。"""
    if attached is None:
        return ""
    current  = "已提供" if attached else "未提供"
    previous = "含实际图像记录，可依据对应摘要讨论" if history_available else "没有可用图像记录"
    return (
        f"图像证据：当前事件{current}图像附件；历史{previous}。"
        "附件存在只说明收到媒体，画面可用性取决于是否有成功解析的内容或摘要。"
        "具体画面事实以实际摘要或用户文字描述为依据；标注的假设与邀请推测保持其不确定性。"
        "用户提到刚才、上面、这张图时，先结合聊天顺序找到对应的历史图片摘要。"
        "当前文字消息没有重复附图时，历史中成功解析的对应摘要仍然可用，直接据此接话。"
        "用户要求评价或确认具体图片时，核对当前附件和对应历史摘要；两处均无可用画面时，"
        "像群友一样随口说一句图没加载出来或者裂了，保持对画面的未知，不用画面、读取、解析这类说法；附和用户的视觉评价也属于确认画面。"
        "摘要只够支持部分判断时，先回答看清的内容，只对尚不清楚的细节保留疑问。"
        "摘要里没有图上的文字时，口语地说那几个字没看清；字迹模糊、被遮挡等画面原因需要摘要明确支持。"
        "仅依据用户文字接话时明确归因于其描述，避免暗示亲眼看过；用户要求基于文字创作、"
        "复述、翻译以及概念和编程问题可以直接完成，无需追加缺图声明。"
    )
