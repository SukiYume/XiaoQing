"""群里最近在聊的话题词：整理第一步顺带产出，交给 xiaoqing_life 当上网的方向。

只有话题和关键词，没有原话、出处和任何 ID；只记群聊里群友自己聊的（其他插件推送的内容不算）；
标了 ``tone`` / ``avoid`` 的（吵架、伤心事、谁的私事）记下但不往外给。存在范围状态里，同一个话题
再聊到时更新时间。话题和关键词是拿去搜的，超长的关键词整个不要，不截成半个词。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .model import DAY, USAGES
from .text import squash

TOPIC_CHARS   = 12
KEYWORD_CHARS = 8
MAX_KEYWORDS  = 3
MAX_KEPT      = 20
KEEP_DAYS     = 14
FRESH_DAYS    = 3
MAX_GIVEN     = 3


def clean(item: Mapping[str, Any], at: float) -> dict[str, Any] | None:
    topic = squash(item.get("topic"))
    if not 2 <= len(topic) <= TOPIC_CHARS:
        return None
    raw      = item.get("keywords")
    keywords = [
        word
        for word in (squash(value) for value in (raw if isinstance(raw, list) else []))
        if 0 < len(word) <= KEYWORD_CHARS
    ][:MAX_KEYWORDS]
    usage = str(item.get("usage") or "normal")
    return {
        "topic"   : topic,
        "keywords": keywords,
        "usage"   : usage if usage in USAGES else "normal",
        "at"      : at,
    }


def _key(topic: str) -> str:
    return "".join(str(topic).lower().split())


def merge(existing: Any, fresh: Iterable[Mapping[str, Any]], now: float) -> list[dict[str, Any]]:
    """新的话题并进已有的：同一个话题更新时间、合并关键词，用法取更谨慎的；按时间留最近的。"""

    rows = (
        [dict(row) for row in existing if isinstance(row, dict)]
        if isinstance(existing, list)
        else []
    )
    for item in fresh:
        found = next(
            (row for row in rows if _key(row.get("topic", "")) == _key(item["topic"])), None
        )
        if found is None:
            rows.append(dict(item))
            continue
        found["at"]       = max(float(found.get("at") or 0.0), float(item["at"]))
        found["keywords"] = list(dict.fromkeys([*found.get("keywords", []), *item["keywords"]]))[
            :MAX_KEYWORDS
        ]
        if USAGES.index(item["usage"]) > USAGES.index(str(found.get("usage") or "normal")):
            found["usage"] = item["usage"]
    rows = [row for row in rows if now - float(row.get("at") or 0.0) <= KEEP_DAYS * DAY]
    rows.sort(key=lambda row: float(row.get("at") or 0.0))
    return rows[-MAX_KEPT:]


def _given(existing: Any, now: float, days: float, limit: int) -> list[dict[str, Any]]:
    rows = [
        row
        for row in (existing if isinstance(existing, list) else [])
        if isinstance(row, dict)
        and row.get("usage") == "normal"
        and 0 <= now - float(row.get("at") or 0.0) <= days * DAY
    ]
    rows.sort(key=lambda row: float(row.get("at") or 0.0), reverse=True)
    return [
        {"topic": str(row["topic"]), "keywords": list(row.get("keywords") or [])}
        for row in rows[:limit]
    ]


def fresh(existing: Any, now: float) -> list[dict[str, Any]]:
    """这两三天可以自然提起的话题词，新的在前；只有话题和关键词。"""

    return _given(existing, now, FRESH_DAYS, MAX_GIVEN)


def heard(existing: Any, now: float) -> list[dict[str, Any]]:
    """这个群还记着的全部可以自然提起的话题词：她主动开口时，用来认出哪些事是这个群聊到过的。"""

    return _given(existing, now, KEEP_DAYS, MAX_KEPT)
