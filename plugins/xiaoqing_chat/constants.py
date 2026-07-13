from __future__ import annotations

# ── Shared question detection ──
# Used across frequency_control, heartflow, goal_state, reply_checker.

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
_COLLOQUIAL_MEI_ONE_CHAR_PREDICATES = frozenset({"好", "到", "行", "醒", "睡", "吃", "喝", "来", "去", "有"})
_COLLOQUIAL_MEI_STANDALONE_NEGATIONS = frozenset(
    {"没", "真没", "还没", "也没", "都没", "并没", "我没", "你没", "他没", "她没", "它没", "这没", "那没"}
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
    """Check if text is a question (contains question marks, ends with question particles, or
    contains question keywords).
    """
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


# Maximum character length considered as "short text" for display/truncation purposes.
DEFAULT_SHORT_TEXT_LIMIT = 120

# Maximum character length for text logged in step-level debug output.
LOG_TEXT_LIMIT = 140

# How many recent local_ids to track per chat for auto-increment allocation.
LOCAL_ID_HISTORY_LIMIT = 50

# How many messages to scan backwards when resolving a local_id (e.g. "m123") to its content.
FIND_BY_LOCAL_ID_LIMIT = 200

# Timeout (seconds) for the foreground memory retrieval task before giving up.
MEMORY_RETRIEVAL_TIMEOUT = 4.5

# Maximum number of unknown/jargon words to look up per reply generation.
UNKNOWN_WORDS_MAX = 6

# Default maximum number of expression habits injected into the prompt.
EXPRESSION_MAX_INJ_DEFAULT = 10

# Maximum number of reply regeneration attempts when reply-checker rejects the output.
REGENERATION_MAX_ATTEMPTS = 3

# Minimum interval (seconds) between expression learning runs for the same chat.
EXPRESSION_LEARN_MIN_INTERVAL = 90.0

# Minimum number of new messages before triggering an expression learning cycle.
EXPRESSION_LEARN_MIN_MESSAGES = 10
