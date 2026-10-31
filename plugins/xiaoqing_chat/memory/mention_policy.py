"""决定一条记忆该怎么用：可以直接提、只影响语气、带点不确定，还是除非问起不提。

记住一件事不等于每次都要说出来。伤心事翻出来像揭伤疤，吵过的架主动提像翻旧账，
很久以前的事说得太笃定又容易错。这里按记忆的类型、时间和内容给每条加上用法标签，
并在对方问“吃了吗”“在干嘛”这类当下状态时，过滤掉旧的状态类记忆。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any

from ..store_base import coerce_finite_float

MENTION   = "mention"
TONE      = "tone"
UNCERTAIN = "uncertain"
AVOID     = "avoid"

LABELS = {
    MENTION  : "可以自然提起",
    TONE     : "只影响语气，别复述",
    UNCERTAIN: "时间久了，提起时带点不确定",
    AVOID    : "除非对方问起，否则不主动提",
}

# 稳定资料：人物事实、画像和管理员知识，不按“当下状态”过滤。
_STABLE_TYPES = frozenset({"person_profile", "person_info", "knowledge"})
# 不同类型多久以后算“时间久了”。
_UNCERTAIN_AFTER_DAYS   = {"topic_summary": 14.0, "person_info": 120.0}
_DEFAULT_UNCERTAIN_DAYS = 30.0
_STATE_MAX_AGE_SECONDS  = 6 * 3600.0

_CONFLICT_RE = re.compile(r"吵架|吵了一架|吵起来|闹翻|拉黑|绝交|翻脸|冷战|冒犯|骂了|互骂|撕起来")
_SCAR_RE     = re.compile(
    r"去世|过世|病逝|葬礼|分手|离婚|住院|手术|确诊|抑郁|焦虑症|失业|被裁|裁员|挂科|退学|"
    r"流产|自残|轻生|想不开"
)
_STATE_QUERY_RE = re.compile(
    r"吃了吗|吃饭了吗|吃了没|吃饭没|在干嘛|在干啥|在干什么|在做什么|在忙什么|睡了吗|睡了没|醒了吗|"
    r"起床了吗|在不在|忙吗|忙不忙|下班了吗|下课了吗|放学了吗|到家了吗|到了吗|现在怎么样|现在在哪"
)


def _age_seconds(meta: dict[str, Any], now: float) -> float | None:
    updated = coerce_finite_float(meta.get("updated_at"), default=0.0, minimum=0.0)
    return max(0.0, now - updated) if updated > 0 else None


def classify(text: str, meta: dict[str, Any], *, now: float | None = None) -> str:
    """一条记忆的用法。冲突优先于伤心事，伤心事优先于时间久远。"""

    moment = time.time() if now is None else now
    kind   = str(meta.get("type") or "")
    if kind == "knowledge":
        return MENTION
    # 摘要写入时由模型结合上下文标过的，以标注为准；没有标注的旧记录再按关键词判断。
    labelled = str(meta.get("mention") or "")
    if labelled == "avoid":
        return AVOID
    if labelled == "tone":
        return TONE
    if labelled != "normal" and _CONFLICT_RE.search(text):
        return AVOID
    if labelled != "normal" and _SCAR_RE.search(text):
        return TONE
    age  = _age_seconds(meta, moment)
    days = _UNCERTAIN_AFTER_DAYS.get(kind, _DEFAULT_UNCERTAIN_DAYS)
    if kind != "person_profile" and age is not None and age > days * 86400.0:
        return UNCERTAIN
    return MENTION


def asks_current_state(text: str) -> bool:
    return _STATE_QUERY_RE.search(re.sub(r"\s+", "", str(text or ""))) is not None


def keep_for_query(meta: dict[str, Any], current_text: str, *, now: float | None = None) -> bool:
    """对方问当下状态时，旧的对话类记忆只会把过去的状态当成现在，不交给模型。"""

    if not asks_current_state(current_text):
        return True
    if str(meta.get("type") or "") in _STABLE_TYPES:
        return True
    age = _age_seconds(meta, time.time() if now is None else now)
    return age is not None and age <= _STATE_MAX_AGE_SECONDS


def format_items(
    items: Sequence[Any], *, current_text: str = "", now: float | None = None
) -> list[str]:
    """把检索结果写成带用法标签的行；被当下状态过滤掉的条目不出现。"""

    moment           = time.time() if now is None else now
    lines: list[str] = []
    for item in items:
        text = str(getattr(item, "text", "") or "").strip()
        meta = getattr(item, "meta", None)
        meta = meta if isinstance(meta, dict) else {}
        if not text or not keep_for_query(meta, current_text, now=moment):
            continue
        lines.append(f"- [{LABELS[classify(text, meta, now=moment)]}] {text}")
    return lines
