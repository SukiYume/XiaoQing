"""一次活动的公共部分：依赖与结果、写经历、学词条 / 表达 / 兴趣、收拾、记活动时段、记失败、写回强化。

浏览（``session``）、研究（``research``）和整理作品（``work_on``）共用，彼此不再互相导入。
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import digest as digest_mod
from . import dossier, reinforce
from . import threads as thread_ops
from . import works as works_mod
from .config import LifeConfig
from .days import day_key
from .lifecycle import (
    add_interest_evidence,
    consolidate,
    is_known,
    merge_topic,
    prune_episodes,
    refresh_expressions,
    refresh_interests,
    refresh_terms,
    seed_interests,
    term_key,
    upsert_expression,
    upsert_term,
)
from .planner import HOT_SCROLL, HOT_SOURCE_WEIGHTS, INTEREST, Decision
from .sources import deep
from .sources.base import Fetcher, SourceItem
from .stores import MAX_LOOKUP_QUEUE, MAX_RECENT, LifeStore, lookup_missed, new_id

ACTIVITY_KEEP_SEC = 24 * 3600


FEELING_DELTA = {"like": 1.0, "dislike": -1.0, "mixed": 0.5}


Complete = Callable[[str, list[dict[str, str]]], Awaitable[str]]


@dataclass(slots=True)
class SessionDeps:
    fetch       : Fetcher
    complete    : Complete
    rng         : random.Random
    now         : Callable[[], datetime]
    persona     : str  = ""
    reasoning_ok: bool = True
    # 她此刻的作息活动文字（"上课" "睡觉"），进入消化提示让感想贴合当下状态。
    activity_text: str = ""
    # 梗图用：下载并校验图片、视觉判定；缺省时不收梗图。
    download: Callable[[str, int], Awaitable[tuple[bytes, str] | None]] | None = None
    vision  : Callable[[list[dict[str, Any]]], Awaitable[str]] | None          = None
    # 调试 / 验证脚本的钩子：拿到取材条目、模型原始 JSON。
    on_items : Callable[[list[SourceItem]], None] | None                 = None
    on_digest: Callable[[dict[str, Any], list[SourceItem]], None] | None = None
    # B站 登录凭证此刻是否可用（未登录或已失效为 False）；字幕只在可用且 ``bili_subtitles`` 开启时读。
    subtitles_ok: Callable[[], bool] = lambda: False


@dataclass(slots=True)
class SessionOutcome:
    status  : str = "ok"  # ok / failed / skipped
    reason  : str = ""
    items   : int = 0
    episodes: int = 0
    terms   : int = 0
    details : dict[str, float] = field(default_factory=dict)
    # 这次实际用到的来源（热榜源名），用来更新来源偏好。
    source: str = ""


def episode_rows(
    result: digest_mod.DigestResult,
    items: list[SourceItem],
    decision: Decision,
    start: float,
    end: float,
    tz: Any,
    auto_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """经历的时间戳按顺序均匀撒在名义浏览时段 [start, end] 内：她是一条一条刷过去的。

    ``auto_ids`` 是研究要点引用、但没出现在 ``looked_at`` 里的条目：自动补一条（meh、感想为空、``auto``）。
    """

    by_id                           = {item.item_id: item for item in items}
    term_keys: dict[str, list[str]] = {}
    for term in result.terms:
        term_keys.setdefault(term.item_id, []).append(term_key(term.term))
    rows  = []
    seens = list(result.looked_at)
    seens += [digest_mod.LookedAt(item_id, "", "", "meh", False, ()) for item_id in auto_ids]
    total = len(seens)
    for order, seen in enumerate(seens, start=1):
        item = by_id[seen.item_id]
        at   = start + (end - start) * order / (total + 1)
        rows.append(
            {
                "id"              : new_id("e"),
                "at"              : at,
                "gist"            : item.text[:60] + ("…" if len(item.text) > 60 else ""),
                "tags"            : list(item.tags),
                "uploader"        : item.uploader,
                "url"             : item.url,
                "duration_s"      : item.duration_s,
                "dense_moment"    : item.dense_moment,
                "activity"        : decision.choice,
                "source"          : item.source,
                "item_id"         : item.item_id,
                "title"           : item.title,
                "reaction"        : seen.reaction,
                "feeling"         : seen.feeling,
                "share"           : seen.share,
                "topics"          : list(seen.topics),
                "term_keys"       : term_keys.get(item.item_id, []),
                "thread_id"       : decision.params.get("thread_id", ""),
                "chat_topic"      : str(decision.params.get("chat_topic") or ""),
                "depth"           : deep.depth_of(item),
                "subtitle_partial": item.subtitle_partial,
                "aspects": (
                    {"like": seen.liked_part, "dislike": seen.disliked_part}
                    if seen.feeling == "mixed"
                    else {}
                ),
                "auto": seen.ref == "",
            }
        )
    return rows


QUEUE_TERM_CHARS = 8


MIN_TERM_VOICES = 2


def queue_for_lookup(state: dict[str, Any], term: str, now: float) -> None:
    """新的未查实词条自动进待查队列（有界、去重），让百科来判断是不是真的流行语；最近查过没查到的不排。"""

    queue: list[str] = state.setdefault("lookup_queue", [])
    if term not in queue and len(queue) < MAX_LOOKUP_QUEUE and not lookup_missed(state, term, now):
        queue.append(term)


def learn(
    store: LifeStore,
    result: digest_mod.DigestResult,
    episodes: list[dict[str, Any]],
    now: float,
) -> None:
    for term in result.terms:
        fresh = term_key(term.term) not in store.terms
        if fresh and term.voices < MIN_TERM_VOICES:
            # 新说法只在一条评论里出现过：多半是一次性的玩笑（“5090Ti”）或专业词，不收；
            # 已经在收着的说法再碰到一次，照常记一个来源。
            continue
        upsert_term(
            store.terms,
            {
                "term"   : term.term,
                "meaning": term.meaning,
                "usage"  : term.usage,
                "tone"   : term.tone,
                "voices" : term.voices,
            },
            term.item_id,
            now,
        )
        if fresh and len(term.term) <= QUEUE_TERM_CHARS:
            queue_for_lookup(store.state, term.term, now)
    for expr in result.expressions:
        upsert_expression(
            store.expressions,
            {
                "situation": expr.situation,
                "pattern"  : expr.pattern,
                "kind"     : expr.kind,
                "example"  : expr.example,
            },
            expr.item_id,
            now,
        )
    for row in episodes:
        if row.get("auto"):
            continue  # 自动补的经历不产生兴趣证据
        delta   = FEELING_DELTA.get(row["feeling"], 0.0)
        aspects = row.get("aspects") or {}
        for topic in row["topics"]:
            add_interest_evidence(
                store.interests,
                topic,
                episode_id     = row["id"],
                item_id        = row["item_id"],
                delta          = delta,
                now            = now,
                aspect_like    = str(aspects.get("like", "")),
                aspect_dislike = str(aspects.get("dislike", "")),
            )


def liked_count(rows: list[dict[str, Any]]) -> float:
    """喜欢的条数；又爱又嫌（mixed）算 0.5 个喜欢。"""

    return sum(1.0 for r in rows if r["feeling"] == "like") + 0.5 * sum(
        1 for r in rows if r["feeling"] == "mixed"
    )


def remember_activity(state: dict[str, Any], decision: Decision, start: float, end: float) -> None:
    """``start``/``end`` 是名义浏览时段；间隔、滚动时长和“正在做什么”都按它算。"""

    state["activity_log"].append({"start": start, "end": end})
    state["activity_log"][:] = [
        s for s in state["activity_log"] if float(s["end"]) >= end - ACTIVITY_KEEP_SEC
    ]
    state["recent"].append(
        {
            "choice"   : decision.choice,
            "thread_id": decision.params.get("thread_id", ""),
            "query"    : decision.params.get("query", ""),
            "explored" : bool(decision.params.get("explored")),
            "at"       : end,
        }
    )
    del state["recent"][:-MAX_RECENT]


def housekeeping(store: LifeStore, cfg: LifeConfig, now: float) -> None:
    refresh_terms(store.terms, now, cfg)
    refresh_expressions(store.expressions, now, cfg)
    refresh_interests(store.interests, now)
    seed_interests(store.interests, cfg.seed_interests, now)
    consolidate(store.interests, store.snapshots, now)
    del store.snapshots[:-100]
    dossier.sync(store, cfg, now)  # 先于下面的清理：档案存自己的快照副本
    works_mod.housekeep(store.works, now)
    thread_ops.prune_threads(store.threads, now)
    prune_episodes(store.episodes, now, cfg, thread_ops.protected_episode_ids(store.threads))


def known_keys(terms: dict[str, dict[str, Any]]) -> set[str]:
    return {k for k, e in terms.items() if is_known(e)}


async def reinforce_outcome(
    store: LifeStore,
    decision: Decision,
    outcome: SessionOutcome,
    span: Span,
    deps: SessionDeps,
) -> None:
    """一次成功消化的上网把收获写回偏好、来源偏好、习惯，并在没有喜欢时给话题一点负面证据。"""

    if outcome.status != "ok" or outcome.reason != "digested":
        return
    d      = outcome.details
    reward = reinforce.session_reward(
        liked           = d.get("liked", 0),
        disliked        = d.get("disliked", 0),
        new_known       = d.get("new_known", 0),
        new_expressions = d.get("new_expressions", 0),
        thread_progress = d.get("thread_progress", 0),
        memes           = d.get("memes", 0),
    )
    moment = datetime.fromtimestamp(span[0], deps.now().tzinfo)
    day    = day_key(moment)

    def apply(s: LifeStore) -> None:
        reinforce.update_preference(s.state, "activity", decision.choice, reward)
        if decision.choice == HOT_SCROLL and outcome.source in HOT_SOURCE_WEIGHTS:
            reinforce.update_preference(s.state, "source", outcome.source, reward)
        reinforce.record_habit(s.state, moment, reward)
        s.state["last_reward"] = round(reward, 3)
        query                  = str(decision.params.get("query") or "")
        # 朋友们在聊的话题不是她自己的兴趣：没看到喜欢的也不记负面证据。
        chat_topic = decision.params.get("chat_topic")
        if decision.choice == INTEREST and query and not chat_topic and d.get("liked", 0) == 0:
            penalize_topic(s, query, day, span[0])

    await store.mutate(apply)


def penalize_topic(store: LifeStore, query: str, day: str, now: float) -> None:
    """一次 interest_dive 一条喜欢的都没有：给所搜话题记一条 −0.5 的证据（同一话题每天最多一次）。"""

    topic                     = merge_topic(store.interests, query)
    penalties: dict[str, str] = store.state.setdefault("topic_penalty", {})
    if not topic or penalties.get(topic) == day:
        return
    penalties[topic] = day
    add_interest_evidence(
        store.interests,
        topic,
        episode_id = "",
        item_id    = f"dive:{day}:{topic}",
        delta      = reinforce.TOPIC_PENALTY,
        now        = now,
    )


Span = tuple[float, float]


async def record_failure(
    store: LifeStore,
    reason: str,
    decision: Decision,
    span: Span,
    deps: SessionDeps,
    *,
    failed: bool = True,
) -> SessionOutcome:
    """全部来源失败或模型不可用：只记失败时间，不写任何经历；这次不算浏览了那么久。"""

    start = span[0]
    end   = max(start, deps.now().timestamp())

    def apply(s: LifeStore) -> None:
        if failed:
            s.state["last_failure_at"] = end
        s.state["last_session_end"] = end
        s.state["current"]          = {}
        s.state["last_decision"]    = {**s.state.get("last_decision", {}), "outcome": reason}
        remember_activity(s.state, decision, start, end)

    await store.mutate(apply)
    return SessionOutcome("failed" if failed else "skipped", reason)
