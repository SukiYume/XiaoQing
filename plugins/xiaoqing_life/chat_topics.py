"""朋友们在聊什么（学 dafeiyu 的 chat_topics：朋友聊过的是她上网的方向，不是她自己的兴趣证据）。

1. **听到**：xiaoqing_chat 每轮回复带来本群最近的话题词（它自己的话题摘要里，只取话题和关键词，不是
   原话，没有任何 ID；只来自群聊，只取可以自然提起的）。这里只记话题词、听到几次和时间。
2. **挑**：睡前整理时她从这两三天听到的里挑 0–2 个自己也想去网上看看的公开话题（游戏、番剧、梗、新闻、
   作品……），写下到 B站搜什么；谁的私事、工作、身体、感情不挑。搜索词只来自这一步，原来的话题词不会
   直接发给任何网站。
3. **去看**：按兴趣上网时可以从挑出来的话题里抽一个去搜（``planner``），每个只看一次；刚挑出来、还没
   看过的话题让下一次上网不用再掷骰子（像 dafeiyu 的好奇火花）。看到的经历照常按她自己的感想记兴趣，
   话题本身不因为朋友在聊就变成她的兴趣，也不因为没看到喜欢的就记负面证据。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .days import DAY
from .safety import clean_text, is_blocked, normalize_for_match, similar

TOPIC_CHARS         = 12
KEYWORD_CHARS       = 8
MAX_KEYWORDS        = 3
MAX_TOPICS_PER_TURN = 3
MAX_HEARD           = 40
HEARD_KEEP_DAYS     = 14
REVIEW_DAYS         = 3
MAX_REVIEW          = 8
SKIP_DAYS           = 7
MAX_PICKS           = 2
PICK_FRESH_DAYS     = 7
PICK_KEEP_DAYS      = 14
QUERY_CHARS         = 20
WHY_CHARS           = 30


def clean_topics(value: Any) -> list[dict[str, Any]]:
    """聊天发来的 ``topics``：``[{"topic": str, "keywords": [str]}]``，类型不对的丢掉。"""

    if not isinstance(value, list):
        return []
    found: list[dict[str, Any]] = []
    for row in value[:MAX_TOPICS_PER_TURN]:
        if not isinstance(row, dict):
            continue
        topic    = clean_text(row.get("topic"), TOPIC_CHARS)
        words    = row.get("keywords")
        keywords = [
            k
            for k in (
                clean_text(w, KEYWORD_CHARS) for w in (words if isinstance(words, list) else [])
            )
            if k
        ][:MAX_KEYWORDS]
        if len(topic) >= 2:
            found.append({"topic": topic, "keywords": keywords})
    return found


def _match(heard: list[dict[str, Any]], topic: str) -> dict[str, Any] | None:
    key = normalize_for_match(topic)
    return next(
        (
            h
            for h in heard
            if normalize_for_match(str(h.get("topic", ""))) == key
            or similar(str(h["topic"]), topic)
        ),
        None,
    )


def record_heard(
    state: dict[str, Any], topics: Iterable[dict[str, Any]], now: float, blocked: tuple[str, ...]
) -> bool:
    """记下这一轮听到的话题词；返回是否改了状态。"""

    heard: list[dict[str, Any]] = state.setdefault("heard_topics", [])
    changed                     = False
    for row in topics:
        topic = str(row.get("topic") or "")
        if not topic or is_blocked(" ".join([topic, *row.get("keywords", [])]), blocked):
            continue
        entry = _match(heard, topic)
        if entry is None:
            heard.append(
                {
                    "topic"      : topic,
                    "keywords"   : list(row.get("keywords", [])),
                    "first_at"   : now,
                    "last_at"    : now,
                    "turns"      : 1,
                    "status"     : "new",
                    "reviewed_at": 0.0,
                }
            )
        else:
            entry["last_at"]  = now
            entry["turns"]    = int(entry.get("turns", 0)) + 1
            merged            = [*entry.get("keywords", []), *row.get("keywords", [])]
            entry["keywords"] = list(dict.fromkeys(merged))[:MAX_KEYWORDS]
        changed = True
    if changed:
        prune(state, now)
    return changed


def prune(state: dict[str, Any], now: float) -> None:
    heard = [
        h
        for h in state.get("heard_topics", [])
        if now - float(h.get("last_at", 0.0)) <= HEARD_KEEP_DAYS * DAY
    ]
    heard.sort(key=lambda h: float(h.get("last_at", 0.0)))
    state["heard_topics"] = heard[-MAX_HEARD:]
    state["chat_topics"]  = [
        p
        for p in state.get("chat_topics", [])
        if now - float(p.get("picked_at", 0.0)) <= PICK_KEEP_DAYS * DAY
    ]


def for_review(state: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """睡前给她看的：这两三天听到、还没挑过的（挑剩下的过一周又听到才再给）。听到次数多的在前。"""

    rows = []
    for h in state.get("heard_topics", []):
        if now - float(h.get("last_at", 0.0)) > REVIEW_DAYS * DAY:
            continue
        status = h.get("status")
        if status == "picked":
            continue
        reviewed = float(h.get("reviewed_at", 0.0))
        if status == "skipped" and (
            now - reviewed < SKIP_DAYS * DAY or float(h.get("last_at", 0.0)) <= reviewed
        ):
            continue
        rows.append(h)
    rows.sort(key=lambda h: (-int(h.get("turns", 0)), -float(h.get("last_at", 0.0))))
    return rows[:MAX_REVIEW]


def review_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "topic"   : h["topic"],
            "keywords": list(h.get("keywords", [])),
            "times"   : int(h.get("turns", 0)),
        }
        for h in rows
    ]


def apply_review(
    state: dict[str, Any],
    offered: list[dict[str, Any]],
    raw: Any,
    now: float,
    blocked: tuple[str, ...],
) -> int:
    """睡前整理的 ``friends_topics``：只认给她看过的话题；给过的都标成挑了 / 没挑。返回挑出的个数。"""

    picks: list[dict[str, Any]] = state.setdefault("chat_topics", [])
    by_key                      = {normalize_for_match(str(h["topic"])): h for h in offered}
    chosen: set[str]            = set()
    for row in raw if isinstance(raw, list) else []:
        if len(chosen) >= MAX_PICKS or not isinstance(row, dict):
            break
        key   = normalize_for_match(clean_text(row.get("topic"), TOPIC_CHARS))
        query = clean_text(row.get("query"), QUERY_CHARS)
        why   = clean_text(row.get("why"), WHY_CHARS)
        if key not in by_key or key in chosen or not query or is_blocked(f"{query} {why}", blocked):
            continue
        chosen.add(key)
        topic    = str(by_key[key]["topic"])
        picks[:] = [p for p in picks if p.get("topic") != topic]
        picks.append(
            {"topic": topic, "query": query, "why": why, "picked_at": now, "tried_at": 0.0}
        )
    for key, entry in by_key.items():
        entry["status"]      = "picked" if key in chosen else "skipped"
        entry["reviewed_at"] = now
    return len(chosen)


def fresh(state: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """挑出来、还没去看过的话题，新挑的在前。"""

    rows = [
        p
        for p in state.get("chat_topics", [])
        if not p.get("tried_at") and now - float(p.get("picked_at", 0.0)) <= PICK_FRESH_DAYS * DAY
    ]
    return sorted(rows, key=lambda p: -float(p.get("picked_at", 0.0)))


def spark(state: dict[str, Any], now: float) -> bool:
    """有在上一次上网之后才挑出来、还没看过的话题：这次不用掷骰子。"""

    since = max(float(state.get("last_session_end", 0.0)), float(state.get("last_session_at", 0.0)))
    return any(float(p.get("picked_at", 0.0)) > since for p in fresh(state, now))


def mark_tried(state: dict[str, Any], topic: str, now: float) -> None:
    for pick in state.get("chat_topics", []):
        if pick.get("topic") == topic:
            pick["tried_at"] = now
