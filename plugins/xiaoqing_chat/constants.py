"""集中定义聊天、记忆、规划和媒体链共享的稳定边界。"""

from __future__ import annotations

# ── 公共疑问句识别 ──
# 由 frequency_control、heartflow、goal_state 和 reply_checker 共用。

_QUESTION_ENDINGS = ("吗", "嘛", "啊", "呢", "吧", "诶")
_QUESTION_KEYWORDS = frozenset(
    {"啥", "谁", "咋", "为啥", "为什么", "什么", "哪", "哪里", "哪个", "多少", "几", "吗", "嘛"}
)
_COLLOQUIAL_MEI_MAX_CHARS = 24
_COLLOQUIAL_MEI_ASPECT_SUFFIXES = ("了", "过", "完", "好", "到", "上", "下", "成", "在", "着")
_COLLOQUIAL_MEI_ACTION_MARKERS = frozenset(
    {
        "吃",
        "喝",
        "睡",
        "醒",
        "起床",
        "到",
        "来",
        "回",
        "去",
        "看",
        "听",
        "写",
        "做",
        "弄",
        "搞",
        "开",
        "关",
        "要",
        "能",
        "会",
        "有",
        "行",
        "可以",
    }
)
_COLLOQUIAL_MEI_ONE_CHAR_PREDICATES = frozenset(
    {"好", "到", "行", "醒", "睡", "吃", "喝", "来", "去", "有"}
)
_COLLOQUIAL_MEI_STANDALONE_NEGATIONS = frozenset(
    {
        "没",
        "真没",
        "还没",
        "也没",
        "都没",
        "并没",
        "我没",
        "你没",
        "他没",
        "她没",
        "它没",
        "这没",
        "那没",
    }
)


def _is_colloquial_mei_question(t: str) -> bool:
    """Detect colloquial questions such as "起床了没" without treating "没" as a question."""
    if not t.endswith("没") or len(t) > _COLLOQUIAL_MEI_MAX_CHARS:
        return False
    if t in _COLLOQUIAL_MEI_STANDALONE_NEGATIONS:
        return False

    body = t[:-1].strip()
    if not body:
        return False
    if len(body) == 1:
        return body in _COLLOQUIAL_MEI_ONE_CHAR_PREDICATES
    if body.endswith(_COLLOQUIAL_MEI_ASPECT_SUFFIXES):
        return True
    return any(marker in body for marker in _COLLOQUIAL_MEI_ACTION_MARKERS)


def is_question(text: str) -> bool:
    """根据问号、句末语气词和疑问关键词判断文本是否为问题。"""
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t or "？" in t:
        return True
    if t.endswith(_QUESTION_ENDINGS):
        return True
    if _is_colloquial_mei_question(t):
        return True
    return any(kw in t for kw in _QUESTION_KEYWORDS)


# 把 local_id（如 m123）还原为正文时，最多向前扫描的消息数。
FIND_BY_LOCAL_ID_LIMIT = 200

# 前台记忆检索任务放弃前的超时秒数。
MEMORY_RETRIEVAL_TIMEOUT = 4.5

# 每次生成回复时最多查询的未知词或行话数量。
UNKNOWN_WORDS_MAX = 6

# 注入提示词的表达习惯默认上限。
EXPRESSION_MAX_INJ_DEFAULT = 10

# 同一会话两次表达学习之间的最短秒数。
EXPRESSION_LEARN_MIN_INTERVAL = 90.0

# 触发一次表达学习所需的最少新消息数。
EXPRESSION_LEARN_MIN_MESSAGES = 10
