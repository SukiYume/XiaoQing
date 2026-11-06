"""一次上网的编排：取材 → 过滤 → 消化（1 次模型）→ 提交到各个存储。

没有取到可看的内容、模型不可用或输出读不懂时，本次不产生任何经历和学习结果，只记失败与
预算消耗。日志只记 ID、数量、原因和错误类型，不记外部原文。
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from . import digest as digest_mod
from . import dossier, reinforce
from . import memes as memes_mod
from . import threads as thread_ops
from . import works as works_mod
from .config import LifeConfig
from .lifecycle import (
    add_interest_evidence,
    consolidate,
    interest_weights,
    mark_seen,
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
from .planner import (
    HOT_SCROLL,
    HOT_SOURCE_WEIGHTS,
    INTEREST,
    TERM_LOOKUP,
    THREAD,
    WORK_ON,
    Decision,
    day_key,
)
from .safety import normalize_for_match, overlap
from .sources import baike, bilibili, deep, hotlists, watch
from .sources.base import Fetcher, SourceError, SourceItem
from .stores import MAX_LOOKUP_QUEUE, MAX_RECENT, LifeStore, new_id, reserve_llm_call, roll_day

logger = logging.getLogger(__name__)

REASONING_ROUTE   = "reasoning"
COMMENT_ITEMS     = 3
ACTIVITY_KEEP_SEC = 24 * 3600
_FEELING_DELTA    = {"like": 1.0, "dislike": -1.0, "mixed": 0.5}

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


# ───────────────────────────── 取材 ─────────────────────────────


async def _hot_items(
    cfg: LifeConfig,
    fetch: Fetcher,
    source: str,
    outcomes: dict[str, bool] | None = None,
    unhealthy: frozenset[str]        = frozenset(),
) -> tuple[list[SourceItem], int]:
    """先取选中的热榜源，失败再依次换其余已开启的；返回条目与失败的来源数。"""

    blocked = cfg.blocked_keywords
    readers = {
        "bili_popular": lambda: bilibili.popular(fetch, blocked),
        "bili_ranking": lambda: bilibili.ranking(fetch, blocked),
        "zhihu_hot"   : lambda: hotlists.zhihu(fetch, blocked),
        "weibo_hot"   : lambda: hotlists.weibo(fetch, blocked),
        "baidu_hot"   : lambda: hotlists.baidu(fetch, blocked),
    }
    rest = [s for s in HOT_SOURCE_WEIGHTS if s != source and cfg.source_on(s)]
    order = [source, *sorted(rest, key=lambda name: name in unhealthy)]  # 近期不稳的排到最后
    failures = 0
    for name in order:
        try:
            items = await readers[name]()
        except SourceError as exc:
            failures += 1
            if outcomes is not None:
                outcomes[name] = False
            logger.info("xiaoqing_life step=source name=%s failed reason=%s", name, exc.reason)
            continue
        if outcomes is not None:
            outcomes[name] = True
        if items:
            return items, failures
    return [], failures


async def _search_items(
    cfg: LifeConfig, fetch: Fetcher, query: str, outcomes: dict[str, bool] | None = None
) -> tuple[list[SourceItem], int]:
    try:
        found = await bilibili.search(fetch, query, cfg.blocked_keywords)
    except SourceError as exc:
        if outcomes is not None:
            outcomes["bili_search"] = False
        logger.info("xiaoqing_life step=source name=bili_search failed reason=%s", exc.reason)
        return [], 1
    if outcomes is not None:
        outcomes["bili_search"] = True
    return found, 0


async def _gather(
    cfg: LifeConfig,
    decision: Decision,
    fetch: Fetcher,
    outcomes: dict[str, bool] | None = None,
    unhealthy: frozenset[str]        = frozenset(),
) -> tuple[list[SourceItem], int]:
    if decision.choice == HOT_SCROLL:
        return await _hot_items(
            cfg, fetch, str(decision.params.get("source") or "bili_popular"), outcomes, unhealthy
        )
    items, failures = await _search_items(
        cfg, fetch, str(decision.params.get("query") or ""), outcomes
    )
    if not items and failures and cfg.source_on("bili_popular"):
        fallback, more = await _hot_items(cfg, fetch, "bili_popular", outcomes, unhealthy)
        return fallback, failures + more
    return items, failures


NOVELTY_BONUS = 1.5


def novelty(item: SourceItem, seen_text: str) -> float:
    """热榜里的新鲜感：标题 / 标签和最近 3 天的经历话题都不重合就加一点分。"""

    if not seen_text:
        return 0.0
    body = f"{item.title} {' '.join(item.tags)}"
    return NOVELTY_BONUS if overlap(body, seen_text) == 0 else 0.0


def interest_hits(item: SourceItem, topics: list[str]) -> int:
    """标题 / 标签命中的兴趣数。"""

    body   = f"{item.title} {' '.join(item.tags)}"
    folded = normalize_for_match(body)
    return sum(
        1
        for topic in topics
        if overlap(body, topic) >= 1 or (len(topic) >= 2 and normalize_for_match(topic) in folded)
    )


def prescore(item: SourceItem, topics: list[str]) -> float:
    """她会不会点开：标题 / 标签与兴趣的重合，加上热度的对数。"""

    return 3.0 * min(interest_hits(item, topics), 3) + math.log10(item.heat + 1)


def pick_clicks(
    cfg: LifeConfig, items: list[SourceItem], topics: list[str], novel_text: str = ""
) -> list[int]:
    """≤3 个 B 站条目（取热评），按预打分挑，返回它们在 ``items`` 里的下标（保持原顺序）。"""

    candidates = [i for i, item in enumerate(items) if item.source.startswith("bili_")]
    candidates.sort(
        key=lambda i: prescore(items[i], topics) + novelty(items[i], novel_text), reverse=True
    )
    return sorted(candidates[:COMMENT_ITEMS])


async def _open_items(
    cfg: LifeConfig,
    fetch: Fetcher,
    items: list[SourceItem],
    topics: list[str],
    novel_text: str = "",
    *,
    subtitles: bool = False,
    need_hit: bool  = True,
) -> list[SourceItem]:
    """点开预选的条目：前 ``max_watch_per_session`` 个取详情 / 标签 / 弹幕，再取热评。

    被看了的条目里，预打分最高且命中兴趣的 ``max_deep_reads_per_session`` 条再深读（简介全文、
    UP 主置顶 / 回复，登录后还有字幕）。
    """

    result = list(items)
    clicks = pick_clicks(cfg, items, topics, novel_text)
    ranked = sorted(clicks, key=lambda i: prescore(items[i], topics), reverse=True)
    watched  = set(ranked[: cfg.max_watch_per_session])
    eligible = [
        i for i in ranked if i in watched and (not need_hit or interest_hits(items[i], topics) >= 1)
    ]
    deeper = set(eligible[: cfg.max_deep_reads_per_session])
    for index in clicks:
        item     = result[index]
        position = index + 1
        if index in watched:
            item = await watch.watch(fetch, item, position, cfg.blocked_keywords)
        page: dict[str, Any] | None = None
        if cfg.source_on("bili_comments") and cfg.max_comments_per_item > 0:
            try:
                page = await bilibili.reply_page(fetch, item, size=20 if index in deeper else 10)
                item = replace(
                    item,
                    comments=bilibili.comments_of(
                        page, position, cfg.blocked_keywords, cfg.max_comments_per_item
                    ),
                )
            except SourceError as exc:
                logger.info(
                    "xiaoqing_life step=source name=bili_comments failed reason=%s", exc.reason
                )
        if index in deeper:
            item = await deep.deep_read(
                fetch,
                item,
                position,
                cfg.blocked_keywords,
                subtitles  = subtitles,
                reply_page = page,
            )
        result[index] = item
    return result


# ───────────────────────────── 提交 ─────────────────────────────


def _episode_rows(
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
                "day"             : day_key(datetime.fromtimestamp(at, tz)),
                "gist"            : item.text[:60],
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


def _queue_for_lookup(state: dict[str, Any], term: str) -> None:
    """新的未查实词条自动进待查队列（有界、去重），让百科来判断是不是真的流行语。"""

    queue: list[str] = state.setdefault("lookup_queue", [])
    if term not in queue and len(queue) < MAX_LOOKUP_QUEUE:
        queue.append(term)


def _learn(
    store: LifeStore,
    result: digest_mod.DigestResult,
    episodes: list[dict[str, Any]],
    now: float,
) -> None:
    for term in result.terms:
        fresh = term_key(term.term) not in store.terms
        upsert_term(
            store.terms,
            {"term": term.term, "meaning": term.meaning, "usage": term.usage, "tone": term.tone},
            term.item_id,
            now,
        )
        if fresh and len(term.term) <= QUEUE_TERM_CHARS:
            _queue_for_lookup(store.state, term.term)
    for expr in result.expressions:
        upsert_expression(
            store.expressions,
            {"situation": expr.situation, "pattern": expr.pattern, "kind": expr.kind},
            expr.item_id,
            now,
        )
    for row in episodes:
        if row.get("auto"):
            continue  # 自动补的经历不产生兴趣证据
        delta   = _FEELING_DELTA.get(row["feeling"], 0.0)
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


def _apply_thread(
    store: LifeStore,
    cfg: LifeConfig,
    decision: Decision,
    proposal: digest_mod.DigestThread | None,
    episodes: list[dict[str, Any]],
    now: float,
) -> None:
    """按消化给出的 thread 动作推进小课题；续做时没有新内容也要记一次空步。"""

    episode_by_item = {e["item_id"]: e["id"] for e in episodes}
    fallback = episodes[0]["id"] if episodes else ""
    target = thread_ops.get_thread(store.threads, str(decision.params.get("thread_id") or ""))
    if proposal is not None and proposal.action == "open":
        thread_ops.open_thread(
            store.threads,
            cfg,
            title      = proposal.title,
            why        = proposal.why,
            next_query = proposal.next_query,
            now        = now,
            topic      = thread_ops.pick_topic(
                store.interests, proposal.topic, proposal.title, proposal.next_query
            ),
        )
        return
    if target is None and proposal is not None and proposal.title:
        target = thread_ops.find_similar(store.threads, proposal.title)
    if target is None or target.get("status") != "open":
        return
    episode_id = episode_by_item.get(proposal.item_id if proposal else "", fallback)
    action     = proposal.action if proposal else "none"
    note       = proposal.note if proposal else ""
    if action == "answered" and episode_id:
        thread_ops.mark_answered(target, episode_id=episode_id, note=note, now=now)
    else:
        useful = action == "progress" and bool(episode_id)
        thread_ops.record_step(
            target, cfg, episode_id=episode_id, note=note, new_content=useful, now=now
        )


def _liked_count(rows: list[dict[str, Any]]) -> float:
    """喜欢的条数；又爱又嫌（mixed）算 0.5 个喜欢。"""

    return sum(1.0 for r in rows if r["feeling"] == "like") + 0.5 * sum(
        1 for r in rows if r["feeling"] == "mixed"
    )


def _remember_activity(state: dict[str, Any], decision: Decision, start: float, end: float) -> None:
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


def _housekeeping(store: LifeStore, cfg: LifeConfig, now: float) -> None:
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


def _known_keys(terms: dict[str, dict[str, Any]]) -> set[str]:
    return {k for k, e in terms.items() if len(e.get("sources", [])) >= 2 or e.get("verified")}


def _thread_progress(threads: list[dict[str, Any]]) -> int:
    """小课题推进（步数增加）或答出（进入回看 / 完成）的数量。"""

    return sum(int(t.get("steps", 0)) for t in threads) + sum(
        1 for t in threads if t.get("status") in ("reviewing", "done")
    )


def _frontier_words(store: LifeStore, rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """小课题笔记里的关键词（下一次要搜的话），挂在对应的经历上。"""

    words: dict[str, list[str]] = {}
    for row in rows:
        thread = thread_ops.get_thread(store.threads, str(row.get("thread_id") or ""))
        if thread is not None:
            words[row["id"]] = [w for w in str(thread.get("next_query", "")).split() if len(w) >= 2]
    return words


async def _commit(
    store: LifeStore,
    cfg: LifeConfig,
    decision: Decision,
    items: list[SourceItem],
    result: digest_mod.DigestResult,
    span: tuple[float, float],
    deps: SessionDeps,
) -> SessionOutcome:
    start, end = span
    moment                  = deps.now()
    now                     = moment.timestamp()
    rows                    = _episode_rows(result, items, decision, start, end, moment.tzinfo)
    stats: dict[str, float] = {}

    def apply(s: LifeStore) -> None:
        known_before, expr_before = _known_keys(s.terms), len(s.expressions)
        progress_before = _thread_progress(s.threads)
        s.episodes.extend(rows)
        _learn(s, result, rows, now)
        _apply_thread(s, cfg, decision, result.thread, rows, now)
        liked = [r for r in rows if r["feeling"] == "like"]
        reinforce.grow_frontier(
            s.frontier, s.interests, liked, _frontier_words(s, rows), cfg.blocked_keywords, now
        )
        stats.update(
            liked           = _liked_count(rows),
            disliked        = sum(1 for r in rows if r["feeling"] == "dislike"),
            new_known       = len(_known_keys(s.terms) - known_before),
            new_expressions = max(0, len(s.expressions) - expr_before),
            thread_progress = 1 if _thread_progress(s.threads) > progress_before else 0,
        )
        mark_seen(s.seen, [item.item_id for item in items], now)
        _housekeeping(s, cfg, now)
        _remember_activity(s.state, decision, start, end)

    await store.mutate(apply)
    outcome = SessionOutcome(
        "ok", "digested", items=len(items), episodes=len(rows), terms=len(result.terms)
    )
    outcome.details.update(stats)
    outcome.source = items[0].source if items else ""
    return outcome


async def _reinforce(
    store: LifeStore,
    cfg: LifeConfig,
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
        if decision.choice == INTEREST and query and d.get("liked", 0) == 0:
            _penalize_topic(s, query, day, span[0])

    await store.mutate(apply)


def _penalize_topic(store: LifeStore, query: str, day: str, now: float) -> None:
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


# ───────────────────────────── 失败与入口 ─────────────────────────────


Span = tuple[float, float]


async def _record_failure(
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
        _remember_activity(s.state, decision, start, end)

    await store.mutate(apply)
    return SessionOutcome("failed" if failed else "skipped", reason)


async def _digest(
    store: LifeStore,
    cfg: LifeConfig,
    items: list[SourceItem],
    deps: SessionDeps,
    day: str,
) -> tuple[digest_mod.DigestResult | None, str]:
    """返回 (结果, 失败原因)；模型调用先占预算再发出。"""

    if not deps.reasoning_ok:
        return None, "model_not_configured"
    if not await reserve_llm_call(store, day, cfg.max_llm_calls_per_day):
        return None, "llm_exhausted"
    messages = digest_mod.build_messages(
        persona       = deps.persona,
        items         = items,
        known_terms   = [t["term"] for t in store.terms.values()],
        thread_titles = [t["title"] for t in thread_ops.active_threads(store.threads)],
        moment        = deps.now(),
        activity      = deps.activity_text,
    )
    try:
        text = await deps.complete(REASONING_ROUTE, messages)
    except Exception as exc:
        logger.info("xiaoqing_life step=digest model_failed error_type=%s", type(exc).__name__)
        return None, "model_failed"
    raw = digest_mod.extract_json(text or "")
    if raw is None:
        return None, "digest_unparseable"
    if deps.on_digest is not None:
        deps.on_digest(raw, items)
    return (
        digest_mod.validate(
            raw, items, max_memes=cfg.max_vision_per_session, blocked=cfg.blocked_keywords
        ),
        "",
    )


def _draw_span(cfg: LifeConfig, deps: SessionDeps, choice: str, start: float) -> Span:
    low, high = cfg.session_minutes.get(choice, (10, 10))
    return start, start + deps.rng.randint(low, high) * 60.0


def _doing(decision: Decision) -> dict[str, Any]:
    """“她这会儿在做什么”：不含任何还没刷到的标题。"""

    return {
        "choice"   : decision.choice,
        "source"   : decision.params.get("source", ""),
        "query"    : decision.params.get("query", ""),
        "thread_id": decision.params.get("thread_id", ""),
    }


async def _start(
    store: LifeStore, cfg: LifeConfig, deps: SessionDeps, decision: Decision
) -> tuple[Span, str]:
    moment = deps.now()
    day    = day_key(moment)
    span   = _draw_span(cfg, deps, decision.choice, moment.timestamp())

    def apply(s: LifeStore) -> None:
        roll_day(s.state, day)
        s.state["sessions_today"]   = int(s.state.get("sessions_today", 0)) + 1
        s.state["last_session_at"]  = span[0]
        s.state["last_session_end"] = span[1]
        s.state["current"]          = {**_doing(decision), "start": span[0], "end": span[1]}
        s.state["last_decision"]    = {**decision.record(span[0])}
        if decision.params.get("frontier"):
            reinforce.mark_tried(s.frontier, str(decision.params["frontier"]))

    await store.mutate(apply)
    return span, day


async def _record_sources(store: LifeStore, outcomes: dict[str, bool], now: float) -> None:
    """本次各来源成败写进来源可靠性（连续失败 ≥2 次就降权 6 小时）。"""

    if not outcomes:
        return

    def apply(s: LifeStore) -> None:
        for name, ok in outcomes.items():
            reinforce.record_source(s.state, name, ok, now)

    await store.mutate(apply)


def _recent_text(store: LifeStore, now: float) -> str:
    cutoff = now - 3 * 86400
    return " ".join(
        f"{e.get('title', '')} {' '.join(e.get('topics', []))} {' '.join(e.get('tags', []))}"
        for e in store.episodes
        if float(e.get("at", 0.0)) >= cutoff
    )


def _topics(store: LifeStore, cfg: LifeConfig, now: float) -> list[str]:
    return list(dict.fromkeys([*interest_weights(store.interests, now), *cfg.seed_interests]))


async def _browse(
    store: LifeStore, cfg: LifeConfig, decision: Decision, span: Span, day: str, deps: SessionDeps
) -> SessionOutcome:
    outcomes: dict[str, bool] = {}
    unhealthy                 = frozenset(
        k
        for k in (*HOT_SOURCE_WEIGHTS, "bili_search")
        if reinforce.health_factor(store.state, k, span[0]) < 1.0
    )
    items, failures = await _gather(cfg, decision, deps.fetch, outcomes, unhealthy)
    await _record_sources(store, outcomes, deps.now().timestamp())
    if deps.on_items is not None:
        deps.on_items(items)
    if not items:
        reason = "all_sources_failed" if failures else "nothing_found"
        return await _record_failure(store, reason, decision, span, deps)
    cap   = int(decision.params.get("item_cap") or cfg.max_items_per_session)
    fresh = [i for i in items if i.item_id not in store.seen][: max(1, cap)]
    if not fresh:
        return await _no_news(store, cfg, decision, span, deps)
    fresh = await _open_items(
        cfg,
        deps.fetch,
        fresh,
        _topics(store, cfg, span[0]),
        novel_text = _recent_text(store, span[0]) if decision.choice == HOT_SCROLL else "",
        subtitles  = cfg.source_on("bili_subtitles") and deps.subtitles_ok(),
    )
    result, reason = await _digest(store, cfg, fresh, deps, day)
    if result is None:
        return await _record_failure(
            store, reason, decision, span, deps, failed=reason != "llm_exhausted"
        )
    outcome                  = await _commit(store, cfg, decision, fresh, result, span, deps)
    outcome.details["memes"] = await _collect_memes(store, cfg, fresh, result, day, deps)
    await _reinforce(store, cfg, decision, outcome, span, deps)
    return outcome


async def _collect_memes(
    store: LifeStore,
    cfg: LifeConfig,
    items: list[SourceItem],
    result: digest_mod.DigestResult,
    day: str,
    deps: SessionDeps,
) -> int:
    picks = memes_mod.merge_picks(
        result.memes, memes_mod.preselect(items), cfg.max_vision_per_session
    )
    if not picks or deps.download is None or deps.vision is None:
        return 0
    download = deps.download

    async def fetch_image(url: str) -> tuple[bytes, str] | None:
        return await download(url, cfg.max_meme_bytes)

    return await memes_mod.collect(
        store,
        cfg,
        items,
        picks,
        download = fetch_image,
        vision   = deps.vision,
        day      = day,
        now      = lambda: deps.now().timestamp(),
    )


async def _no_news(
    store: LifeStore, cfg: LifeConfig, decision: Decision, span: Span, deps: SessionDeps
) -> SessionOutcome:
    """取到的全是看过的：续做的小课题记一次空步，其余什么都不写。"""

    now = deps.now().timestamp()

    def apply(s: LifeStore) -> None:
        _apply_thread(s, cfg, decision, None, [], now)
        _remember_activity(s.state, decision, span[0], span[1])

    await store.mutate(apply)
    return SessionOutcome("ok", "all_seen")


async def _lookup_terms(
    store: LifeStore, cfg: LifeConfig, decision: Decision, span: Span, deps: SessionDeps
) -> SessionOutcome:
    """查百科：查实的流行语直接 known；查不到丢弃，请求出错的留在队列里。"""

    found  : list[baike.BaikeEntry] = []
    settled: list[str]              = []
    errors                          = 0
    for term in [str(t) for t in decision.params.get("terms", [])][:3]:
        try:
            entry = await baike.lookup(deps.fetch, term, cfg.blocked_keywords)
        except SourceError as exc:
            errors += 1
            logger.info("xiaoqing_life step=source name=baike failed reason=%s", exc.reason)
            continue
        settled.append(term)
        if entry is not None:
            found.append(entry)
    if errors and not settled:
        return await _record_failure(store, "all_sources_failed", decision, span, deps)
    now = deps.now().timestamp()

    def apply(s: LifeStore) -> None:
        for entry in found:
            upsert_term(
                s.terms,
                {"term": entry.term, "meaning": entry.meaning, "usage": "", "tone": "中性"},
                f"baike:{term_key(entry.term)}",
                now,
                verified=True,
            )
        s.state["lookup_queue"] = [t for t in s.state["lookup_queue"] if t not in settled]
        _housekeeping(s, cfg, now)
        _remember_activity(s.state, decision, span[0], span[1])

    await store.mutate(apply)
    moment = datetime.fromtimestamp(span[0], deps.now().tzinfo)

    def reward_apply(s: LifeStore) -> None:
        reward = reinforce.session_reward(
            liked=0, disliked=0, new_known=len(found), new_expressions=0, thread_progress=0, memes=0
        )
        reinforce.update_preference(s.state, "activity", decision.choice, reward)
        reinforce.record_habit(s.state, moment, reward)

    await store.mutate(reward_apply)
    return SessionOutcome("ok", "looked_up", terms=len(found))


async def run_session(
    store: LifeStore, cfg: LifeConfig, decision: Decision, deps: SessionDeps
) -> SessionOutcome:
    """一次上网。调用方负责单飞与取消；这里只管顺序和落盘。"""

    span, day = await _start(store, cfg, deps, decision)
    if decision.choice == TERM_LOOKUP:
        outcome = await _lookup_terms(store, cfg, decision, span, deps)
    elif decision.choice == THREAD:
        from . import research

        outcome = await research.run(store, cfg, decision, deps, span, day)
    elif decision.choice == WORK_ON:
        from . import works

        outcome = await works.run(store, cfg, decision, deps, span, day)
    elif decision.choice in (HOT_SCROLL, INTEREST):
        outcome = await _browse(store, cfg, decision, span, day, deps)
    else:
        outcome = SessionOutcome("skipped", "unsupported_choice")
    logger.info(
        "xiaoqing_life step=session choice=%s status=%s reason=%s items=%d episodes=%d terms=%d",
        decision.choice,
        outcome.status,
        outcome.reason,
        outcome.items,
        outcome.episodes,
        outcome.terms,
    )
    return outcome
