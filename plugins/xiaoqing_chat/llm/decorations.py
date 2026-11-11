"""发出前收拾一条回复里的装饰：emoji、波浪号、括号动作，以及当成文字打出来的 QQ 表情名。

人设卡要求这些十条里最多一条。只写在提示词里管不住：模型照着自己前几条的样子接着写，
一个 🙄 能连着挂好几条，“捂脸”会被直接打在句尾。这里按她最近实际发出的消息算：
刚用过就去掉，没用过就留着；句尾的表情名换成真正的 QQ 表情。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# 她最近这么多条回复里出现过装饰，这一条就不再带。
RECENT_WINDOW = 6
# 清单、文稿这类成品里的符号是内容，不是装饰。
_MAX_CASUAL_CHARS = 80

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿⭐⭕️‍]+")
_TILDE_RE = re.compile(r"[～~]+(?=$|[\s，,。.！!？?…）)])")
# 句尾的括号动作（“（笑）”）；“(bushi)”这类改口玩笑是话的一部分，不算。
_ACTION_RE = re.compile(
    r"\s*[（(](?:笑|笑哭|捂脸|逃|跑|溜|躺|叹气|摊手|扶额|歪头|小声|狗头)[）)]\s*$", re.M
)
_QQ_FACE_OUT_RE = re.compile(r"\s*\[想发QQ表情[:：][^\[\]]{1,24}\]")
# 她发出去的 QQ 表情和表情包在聊天记录里的样子。
_QQ_FACE_SENT_RE = re.compile(r"\[(?:QQ表情[:：][^\[\]]{1,24}|表情包[:：][^\[\]]*)\]")
_ANY_MARKER_RE   = re.compile(r"\[想发(?:表情|QQ表情|图片)[:：]")
# 表情名前面是语气词或标点时，才是把表情当文字打了出来（“吃保底了捂脸”）；
# “我只能捂脸”“今天又在摸鱼”是正常的话。
_BEFORE_FACE_WORD = r"(?<=[了啊呢吧呀嘛哦哈啦诶欸噢喔。！？!?…～~\s])"
_TAIL_PUNCT       = r"[\s。.！!～~…]*$"


# 模型语料里的垃圾词：偶尔被带到一句话的末尾（“累成这样 白小姐”“你们玩得开心俺也去”）。
# 收紧 top_p 后已经很少见，这里再兜一层；词表只收实际见过的和同一类站名，挡不住没见过的词。
_JUNK_TAIL_RE = re.compile(
    r"\s*(?:俺(?:也去|去也)|(?:亚洲|欧美|日韩|国产)\s*AV|久久(?:精品|热)|天天(?:爱|射|干)"
    r"|白小姐|助赢|六合彩|时时彩|一本道|色综合|夸克网盘)",
    re.I,
)
# 垃圾词后面最多还能跟这么多字（“俺也去不了一点”），再长就是正经在说这个词。
_JUNK_TAIL_SPAN = 6


def strip_junk_tail(text: str, context: str) -> str:
    """去掉句尾带出来的垃圾词和它后面的几个字；对话里本来就在说这个词时不动。"""

    original = str(text or "")
    for match in _JUNK_TAIL_RE.finditer(original):
        word = match.group(0).strip()
        if len(original) - match.end() > _JUNK_TAIL_SPAN or word.casefold() in context.casefold():
            continue
        head = original[: match.start()].rstrip()
        return head or original
    return original


def has_decoration(text: str) -> bool:
    """这条已经发出的回复里有没有 emoji、波浪号、QQ 表情、表情包或括号动作。"""

    body = str(text or "")
    return bool(
        _EMOJI_RE.search(body)
        or _TILDE_RE.search(body)
        or _QQ_FACE_SENT_RE.search(body)
        or _ACTION_RE.search(body)
    )


def _is_casual(text: str) -> bool:
    return len(text) <= _MAX_CASUAL_CHARS and "```" not in text and text.count("\n") <= 2


def _face_word_re(face_labels: Sequence[str]) -> re.Pattern[str] | None:
    labels = sorted((label for label in face_labels if len(label) >= 2), key=len, reverse=True)
    if not labels:
        return None
    names = "|".join(re.escape(label) for label in labels)
    return re.compile(f"{_BEFORE_FACE_WORD}(?P<face>{names}){_TAIL_PUNCT}")


def tame_decorations(text: str, recent_replies: Sequence[str], face_labels: Sequence[str]) -> str:
    """按她最近的发言收拾这条候选里的装饰，返回可以继续解析的文本。

    - 句尾当文字打出来的表情名：最近没用过装饰就换成 ``[想发QQ表情:名字]``，用过就删掉。
    - 最近 ``RECENT_WINDOW`` 条里用过装饰：这条的 emoji、句尾波浪号、括号动作和 QQ 表情标记都去掉。
    - 清单、代码、长文不动；去掉以后什么都不剩（对方就是要一个表情）时保持原样。
    """

    original = str(text or "")
    if not original.strip() or not _is_casual(original):
        return original
    used_recently = any(has_decoration(reply) for reply in list(recent_replies)[-RECENT_WINDOW:])
    result        = original
    face_word     = _face_word_re(face_labels)
    match         = face_word.search(result) if face_word is not None else None
    if match is not None:
        head = result[: match.start("face")].rstrip()
        if used_recently or _ANY_MARKER_RE.search(result):
            result = head
        else:
            result = f"{head} [想发QQ表情:{match.group('face')}]"
    if used_recently:
        result = _QQ_FACE_OUT_RE.sub("", result)
        result = _ACTION_RE.sub("", result)
        result = _EMOJI_RE.sub("", result)
        result = _TILDE_RE.sub("", result)
        result = re.sub(r"[ \t]{2,}", " ", result).strip()
    return result if result.strip() else original
