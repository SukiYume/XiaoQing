"""一次上网的编排：取材 → 过滤 → 消化（1 次模型）→ 提交到各个存储。

没有取到可看的内容、模型不可用或输出读不懂时，本次不产生任何经历和学习结果，只记失败与
预算消耗。日志只记 ID、数量、原因和错误类型，不记外部原文。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from . import digest as digest_mod
from . import threads as thread_ops
from .config import LifeConfig
from .lifecycle import (
    add_interest_evidence,
    mark_seen,
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
    Decision,
    day_key,
)
from .sources import baike, bilibili, hotlists
from .sources.base import Fetcher, SourceError, SourceItem
from .stores import MAX_RECENT, LifeStore, new_id, reserve_llm_call, roll_day

logger = logging.getLogger(__name__)

REASONING_ROUTE   = "reasoning"
COMMENT_ITEMS     = 3
ACTIVITY_KEEP_SEC = 24 * 3600
_FEELING_DELTA    = {"like": 1, "dislike": -1}

Complete = Callable[[str, list[dict[str, str]]], Awaitable[str]]


@dataclass(slots=True)
class SessionDeps:
    fetch       : Fetcher
    complete    : Complete
    rng         : random.Random
    now         : Callable[[], datetime]
    persona     : str  = ""
    reasoning_ok: bool = True


@dataclass(slots=True)
class SessionOutcome:
    status  : str = "ok"  # ok / failed / skipped
    reason  : str = ""
    items   : int = 0
    episodes: int = 0
    terms   : int = 0
    details : dict[str, int] = field(default_factory=dict)


# ───────────────────────────── 取材 ─────────────────────────────


async def _hot_items(cfg: LifeConfig, fetch: Fetcher, source: str) -> tuple[list[SourceItem], int]:
    """先取选中的热榜源，失败再依次换其余已开启的；返回条目与失败的来源数。"""

    blocked = cfg.blocked_keywords
    readers = {
        "bili_popular": lambda: bilibili.popular(fetch, blocked),
        "bili_ranking": lambda: bilibili.ranking(fetch, blocked),
        "zhihu_hot"   : lambda: hotlists.zhihu(fetch, blocked),
        "weibo_hot"   : lambda: hotlists.weibo(fetch, blocked),
        "baidu_hot"   : lambda: hotlists.baidu(fetch, blocked),
    }
    order    = [source, *[s for s in HOT_SOURCE_WEIGHTS if s != source and cfg.source_on(s)]]
    failures = 0
    for name in order:
        try:
            items = await readers[name]()
        except SourceError as exc:
            failures += 1
            logger.info("xiaoqing_life step=source name=%s failed reason=%s", name, exc.reason)
            continue
        if items:
            return items, failures
    return [], failures


async def _search_items(
    cfg: LifeConfig, fetch: Fetcher, query: str
) -> tuple[list[SourceItem], int]:
    try:
        return await bilibili.search(fetch, query, cfg.blocked_keywords), 0
    except SourceError as exc:
        logger.info("xiaoqing_life step=source name=bili_search failed reason=%s", exc.reason)
        return [], 1


async def _gather(
    cfg: LifeConfig, decision: Decision, fetch: Fetcher
) -> tuple[list[SourceItem], int]:
    if decision.choice == HOT_SCROLL:
        return await _hot_items(cfg, fetch, str(decision.params.get("source") or "bili_popular"))
    items, failures = await _search_items(cfg, fetch, str(decision.params.get("query") or ""))
    if not items and failures and cfg.source_on("bili_popular"):
        fallback, more = await _hot_items(cfg, fetch, "bili_popular")
        return fallback, failures + more
    return items, failures


async def _with_comments(
    cfg: LifeConfig, fetch: Fetcher, items: list[SourceItem]
) -> list[SourceItem]:
    """前几条 B 站条目取热评；单条失败只是没有评论。"""

    if not cfg.source_on("bili_comments") or cfg.max_comments_per_item <= 0:
        return items
    result = list(items)
    for index, item in enumerate(items[:COMMENT_ITEMS], start=1):
        if not item.source.startswith("bili_"):
            continue
        try:
            comments = await bilibili.hot_comments(
                fetch, item, index, cfg.blocked_keywords, cfg.max_comments_per_item
            )
        except SourceError as exc:
            logger.info("xiaoqing_life step=source name=bili_comments failed reason=%s", exc.reason)
            continue
        result[index - 1] = replace(item, comments=comments)
    return result


# ───────────────────────────── 提交 ─────────────────────────────


def _episode_rows(
    result: digest_mod.DigestResult,
    items: list[SourceItem],
    decision: Decision,
    moment: datetime,
) -> list[dict[str, Any]]:
    by_id                           = {item.item_id: item for item in items}
    term_keys: dict[str, list[str]] = {}
    for term in result.terms:
        term_keys.setdefault(term.item_id, []).append(term_key(term.term))
    rows = []
    for seen in result.looked_at:
        item = by_id[seen.item_id]
        rows.append(
            {
                "id"       : new_id("e"),
                "at"       : moment.timestamp(),
                "day"      : day_key(moment),
                "activity" : decision.choice,
                "source"   : item.source,
                "item_id"  : item.item_id,
                "title"    : item.title,
                "reaction" : seen.reaction,
                "feeling"  : seen.feeling,
                "share"    : seen.share,
                "topics"   : list(seen.topics),
                "term_keys": term_keys.get(item.item_id, []),
                "thread_id": decision.params.get("thread_id", ""),
            }
        )
    return rows


def _learn(
    store: LifeStore,
    result: digest_mod.DigestResult,
    episodes: list[dict[str, Any]],
    now: float,
) -> None:
    for term in result.terms:
        upsert_term(
            store.terms,
            {"term": term.term, "meaning": term.meaning, "usage": term.usage, "tone": term.tone},
            term.item_id,
            now,
        )
    for expr in result.expressions:
        upsert_expression(
            store.expressions,
            {"situation": expr.situation, "pattern": expr.pattern, "kind": expr.kind},
            expr.item_id,
            now,
        )
    for row in episodes:
        delta = _FEELING_DELTA.get(row["feeling"], 0)
        for topic in row["topics"]:
            add_interest_evidence(
                store.interests,
                topic,
                episode_id = row["id"],
                item_id    = row["item_id"],
                delta      = delta,
                now        = now,
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


def _remember_activity(state: dict[str, Any], decision: Decision, start: float, end: float) -> None:
    state["activity_log"].append({"start": start, "end": end})
    state["activity_log"][:] = [
        s for s in state["activity_log"] if float(s["end"]) >= end - ACTIVITY_KEEP_SEC
    ]
    state["recent"].append(
        {
            "choice"   : decision.choice,
            "thread_id": decision.params.get("thread_id", ""),
            "query"    : decision.params.get("query", ""),
            "at"       : end,
        }
    )
    del state["recent"][:-MAX_RECENT]


def _housekeeping(store: LifeStore, cfg: LifeConfig, now: float) -> None:
    refresh_terms(store.terms, now, cfg)
    refresh_expressions(store.expressions, now, cfg)
    refresh_interests(store.interests, now)
    seed_interests(store.interests, cfg.seed_interests, now)
    thread_ops.prune_threads(store.threads, now)
    prune_episodes(store.episodes, now, cfg, thread_ops.protected_episode_ids(store.threads))


async def _commit(
    store: LifeStore,
    cfg: LifeConfig,
    decision: Decision,
    items: list[SourceItem],
    result: digest_mod.DigestResult,
    start: float,
    deps: SessionDeps,
) -> SessionOutcome:
    moment = deps.now()
    now    = moment.timestamp()
    rows   = _episode_rows(result, items, decision, moment)

    def apply(s: LifeStore) -> None:
        s.episodes.extend(rows)
        _learn(s, result, rows, now)
        _apply_thread(s, cfg, decision, result.thread, rows, now)
        mark_seen(s.seen, [item.item_id for item in items], now)
        _housekeeping(s, cfg, now)
        _remember_activity(s.state, decision, start, now)

    await store.mutate(apply)
    return SessionOutcome(
        "ok", "digested", items=len(items), episodes=len(rows), terms=len(result.terms)
    )


# ───────────────────────────── 失败与入口 ─────────────────────────────


async def _record_failure(
    store: LifeStore, reason: str, decision: Decision, start: float, deps: SessionDeps
) -> SessionOutcome:
    """全部来源失败或模型不可用：只记失败时间，不写任何经历。"""

    end = deps.now().timestamp()

    def apply(s: LifeStore) -> None:
        s.state["last_failure_at"] = end
        s.state["last_decision"]   = {**s.state.get("last_decision", {}), "outcome": reason}
        _remember_activity(s.state, decision, start, end)

    await store.mutate(apply)
    return SessionOutcome("failed", reason)


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
    )
    try:
        text = await deps.complete(REASONING_ROUTE, messages)
    except Exception as exc:
        logger.info("xiaoqing_life step=digest model_failed error_type=%s", type(exc).__name__)
        return None, "model_failed"
    raw = digest_mod.extract_json(text or "")
    if raw is None:
        return None, "digest_unparseable"
    return (
        digest_mod.validate(
            raw, items, max_memes=cfg.max_vision_per_session, blocked=cfg.blocked_keywords
        ),
        "",
    )


async def _start(store: LifeStore, deps: SessionDeps, decision: Decision) -> tuple[float, str]:
    moment = deps.now()
    day    = day_key(moment)

    def apply(s: LifeStore) -> None:
        roll_day(s.state, day)
        s.state["sessions_today"]  = int(s.state.get("sessions_today", 0)) + 1
        s.state["last_session_at"] = moment.timestamp()
        s.state["last_decision"]   = {**decision.record(moment.timestamp())}

    await store.mutate(apply)
    return moment.timestamp(), day


async def _browse(
    store: LifeStore, cfg: LifeConfig, decision: Decision, start: float, day: str, deps: SessionDeps
) -> SessionOutcome:
    items, failures = await _gather(cfg, decision, deps.fetch)
    if not items:
        reason = "all_sources_failed" if failures else "nothing_found"
        return await _record_failure(store, reason, decision, start, deps)
    fresh = [i for i in items if i.item_id not in store.seen][: cfg.max_items_per_session]
    if not fresh:
        return await _no_news(store, cfg, decision, start, deps)
    fresh = await _with_comments(cfg, deps.fetch, fresh)
    result, reason = await _digest(store, cfg, fresh, deps, day)
    if result is None:
        if reason == "llm_exhausted":
            return SessionOutcome("skipped", reason)
        return await _record_failure(store, reason, decision, start, deps)
    return await _commit(store, cfg, decision, fresh, result, start, deps)


async def _no_news(
    store: LifeStore, cfg: LifeConfig, decision: Decision, start: float, deps: SessionDeps
) -> SessionOutcome:
    """取到的全是看过的：续做的小课题记一次空步，其余什么都不写。"""

    now = deps.now().timestamp()

    def apply(s: LifeStore) -> None:
        _apply_thread(s, cfg, decision, None, [], now)
        _remember_activity(s.state, decision, start, now)

    await store.mutate(apply)
    return SessionOutcome("ok", "all_seen")


async def _lookup_terms(
    store: LifeStore, cfg: LifeConfig, decision: Decision, start: float, deps: SessionDeps
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
        return await _record_failure(store, "all_sources_failed", decision, start, deps)
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
        _remember_activity(s.state, decision, start, now)

    await store.mutate(apply)
    return SessionOutcome("ok", "looked_up", terms=len(found))


async def run_session(
    store: LifeStore, cfg: LifeConfig, decision: Decision, deps: SessionDeps
) -> SessionOutcome:
    """一次上网。调用方负责单飞与取消；这里只管顺序和落盘。"""

    start, day = await _start(store, deps, decision)
    if decision.choice == TERM_LOOKUP:
        outcome = await _lookup_terms(store, cfg, decision, start, deps)
    elif decision.choice in (HOT_SCROLL, INTEREST, THREAD):
        outcome = await _browse(store, cfg, decision, start, day, deps)
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
