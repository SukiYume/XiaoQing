"""私聊收尾沉默：对方只回一句晚安、拜拜、谢谢，而角色已经收过尾时，不再接话。

私聊消息每条必回，互道晚安会变成死循环。这里只认最窄的一种情况，判断失败时一律照常回复：

* 本轮消息去掉标点、表情和语气词后，只剩收尾用语（晚安、拜拜、谢谢、好的、早安、爱你……）；
  带问句、请求、图片或任何新内容都照常回复。
* 角色最近一条发言在时间窗内，本身就是收尾（晚安、拜拜、不客气……），且之后对方
  没说过别的内容。
* 只有“好的”“嗯嗯”这类应声、“抱抱”这类亲昵或“早呀”这类招呼不算角色收尾，对方的第一句晚安仍会得到回应。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

# 强收尾：角色说了这些就算已经把对话收住。
_GOODNIGHT = (
    "晚安",
    "安安",
    "好梦",
    "做个好梦",
    "梦里见",
    "睡个好觉",
    "好好睡",
    "好好休息",
    "早点休息",
    "早点睡",
    "早睡",
    "快去睡",
    "去睡吧",
    "睡吧",
    "睡觉了",
    "睡觉啦",
    "去睡了",
    "去睡啦",
    "睡了",
    "睡啦",
    "先睡了",
    "先睡啦",
    "我睡了",
    "我先睡了",
    "困了",
    "先休息了",
    "休息啦",
    "休息了",
    "goodnight",
    "night",
    "gn",
    "sweetdreams",
)
_FAREWELL = (
    "拜拜",
    "拜",
    "白白",
    "再见",
    "回见",
    "回聊",
    "待会聊",
    "晚点聊",
    "一会聊",
    "等会聊",
    "下回聊",
    "去吧",
    "快去吧",
    "去忙吧",
    "去吃饭吧",
    "吃饭去吧",
    "去吃饭",
    "快去吃饭",
    "明天见",
    "下次见",
    "改天聊",
    "回头聊",
    "下次聊",
    "先这样",
    "不聊了",
    "先撤了",
    "撤了",
    "溜了",
    "下线了",
    "先下了",
    "去忙了",
    "先忙了",
    "你忙吧",
    "去吃饭了",
    "出门了",
    "去洗澡了",
    "路上小心",
    "bye",
    "byebye",
    "seeyou",
)
_THANKS = (
    "谢谢",
    "多谢",
    "感谢",
    "谢啦",
    "谢了",
    "辛苦了",
    "辛苦啦",
    "不客气",
    "没事儿",
    "thanks",
    "thx",
)
_MORNING   = ("早安", "早上好", "早", "早呀", "早啊", "morning")
_AFFECTION = ("爱你", "么么哒", "么么", "亲亲", "抱抱", "mua")
# 弱收尾：应声。只能出现在对方的消息里，不证明角色已经收尾。
_ACK = (
    "好的",
    "好滴",
    "好嘞",
    "好哒",
    "好",
    "行",
    "行吧",
    "可以",
    "收到",
    "知道了",
    "明白",
    "了解",
    "嗯",
    "嗯嗯",
    "哦",
    "噢",
    "ok",
    "okay",
    "没事",
    "没关系",
    "那好",
    "那就好",
)
_STRONG = {
    **dict.fromkeys(_GOODNIGHT, "goodnight"),
    **dict.fromkeys(_FAREWELL, "farewell"),
    **dict.fromkeys(_THANKS, "thanks"),
    **dict.fromkeys(_MORNING, "morning"),
    **dict.fromkeys(_AFFECTION, "affection"),
}
_KINDS: dict[str, str] = {**dict.fromkeys(_ACK, "ack"), **_STRONG}
# 角色说这些不代表收尾：应声只是接话，“抱抱”常常是在安慰，早安是打招呼。
_NOT_BOT_CLOSING = frozenset({"ack", "affection", "morning"})
# 打招呼开新话题，哪怕拆开来像“你”加“好”。
_GREETING_RE = re.compile(r"你好|你们好|大家好|哈喽|hello")
_TOKENS = tuple(sorted(_KINDS, key=len, reverse=True))
# 语气词和称呼：让“那我也睡啦宝”仍算收尾，但永远排在收尾用语之后尝试。
_FILLERS = tuple(
    sorted(
        {
            "啦",
            "呀",
            "啊",
            "呢",
            "吧",
            "哈",
            "嘛",
            "咯",
            "喽",
            "哒",
            "嘞",
            "哟",
            "了",
            "的",
            "我",
            "你",
            "也",
            "都",
            "就",
            "先",
            "那",
            "那我",
            "再",
            "快",
            "赶紧",
            "乖",
            "宝",
            "宝贝",
            "亲",
            "大家",
            "今天",
            "今晚",
            "明天",
            "晚上",
            "早上",
            "现在",
            "一下",
        }
        - set(_KINDS),
        key     = len,
        reverse = True,
    )
)
_BRACKET_RE  = re.compile(r"\[([^\]\r\n]*)\]")
_QUESTION_RE = re.compile(
    r"[?？]|吗|么|几点|要不要|好不好|行不行|能不能|是不是|有没有|记得|提醒|帮我|叫我"
)
_MAX_USER_CHARS = 40
_MAX_BOT_CHARS  = 80


def _compact(text: str) -> str | None:
    """去掉标点、空白、表情和 QQ 表情标记；含图片等其他媒体标记时返回 None。"""

    raw = str(text or "")
    for label in _BRACKET_RE.findall(raw):
        if not label.startswith(("QQ表情", "表情")):
            return None
    raw  = _BRACKET_RE.sub("", raw)
    kept = [
        char for char in raw if not char.isspace() and unicodedata.category(char)[0] not in "PSCZ"
    ]
    return "".join(kept).casefold()


def closing_kinds(text: str) -> frozenset[str]:
    """整句只由收尾用语和语气词组成时返回其类别；否则返回空集。"""

    raw = str(text or "")
    if _QUESTION_RE.search(raw):
        return frozenset()
    compact = _compact(raw)
    if not compact or len(compact) > _MAX_USER_CHARS or _GREETING_RE.search(compact):
        return frozenset()
    index, kinds = 0, set[str]()
    while index < len(compact):
        token = next((item for item in _TOKENS if compact.startswith(item, index)), None)
        if token is not None:
            kinds.add(_KINDS[token])
            index += len(token)
            continue
        filler = next((item for item in _FILLERS if compact.startswith(item, index)), None)
        if filler is None:
            return frozenset()
        index += len(filler)
    return frozenset(kinds)


def bot_closed(text: str) -> bool:
    """角色这条发言是否把对话收住：没有发问，且整句或其中一个分句是强收尾。

    “晚安，周五交报告别忘啦”这类先道别、再补一句叮嘱的也算收尾。
    """

    raw = str(text or "").strip()
    if not raw or len(raw) > _MAX_BOT_CHARS or _QUESTION_RE.search(raw):
        return False
    clauses    = [part for part in re.split(r"[，,。！!～~…\s]+", raw) if part.strip()]
    candidates = (raw, *clauses)
    return any(closing_kinds(candidate) - _NOT_BOT_CLOSING for candidate in candidates)


def should_stay_silent(
    current_text: str,
    history: Sequence[Any],
    *,
    now: float,
    window_seconds: float,
    current_message_id: Any = None,
) -> bool:
    """本轮是否属于“对方只是回个收尾，而角色已经收过尾”。

    ``history`` 是按时间排列的 ``StoredMessage``；可以包含本轮消息，会按消息 ID 或末尾
    同文的用户消息排除。
    """

    if not closing_kinds(current_text):
        return False
    messages = list(history)
    if messages:
        last = messages[-1]
        if last.role == "user" and (
            (current_message_id is not None and last.message_id == current_message_id)
            or str(last.content or "").strip() == str(current_text or "").strip()
        ):
            messages.pop()
    for message in reversed(messages):
        if message.role == "assistant":
            if now - float(message.ts or 0.0) > window_seconds:
                return False
            return bot_closed(str(message.content or ""))
        if message.role == "user" and not closing_kinds(str(message.content or "")):
            # 角色收尾之后对方说过别的内容，那一句还没被接住。
            return False
    return False
