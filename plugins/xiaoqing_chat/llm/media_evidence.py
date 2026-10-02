"""把实际消息附件状态传入生成、审查和验收边界。"""

from typing import Any


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


def history_has_image(history: Any) -> bool:
    """历史证据来自结构化媒体记录；纯文本图片标签保持为用户文字。"""
    return any(
        isinstance(part, dict) and part.get("kind") in {"image", "emoji"}
        for message in history
        for part in (getattr(message, "parts", ()) or ())
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
        "简短说明这张图的画面暂时读不到，保持对画面的未知；附和用户的视觉评价也属于确认画面。"
        "摘要只够支持部分判断时，先回答看清的内容，只对尚不清楚的细节保留疑问。"
        "摘要未读出文字时可说‘这几个字暂时没读出来’；字迹模糊、被遮挡等画面原因需要摘要明确支持。"
        "仅依据用户文字接话时明确归因于其描述，避免暗示亲眼看过；用户要求基于文字创作、"
        "复述、翻译以及概念和编程问题可以直接完成，无需追加缺图声明。"
    )
