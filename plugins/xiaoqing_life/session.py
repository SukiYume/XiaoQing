"""一次上网的入口与浏览：开始（记时段和决策）→ 按活动分派（浏览 / 查词 / 研究 / 整理作品）→ 记日志。

浏览 = 取材 → 过滤看过的 → 消化（1 次模型）→ 提交。没有取到可看的内容、模型不可用或输出读不懂时，
本次不产生任何经历和学习结果，只记失败与预算消耗。浏览时的消化只能提议开新小课题，推进由研究循环做。
日志只记 ID、数量、原因和错误类型，不记外部原文。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import activity, fetching, reinforce, research, stats, work_on
from . import chat_topics as chat_topics_mod
from . import digest as digest_mod
from . import memes as memes_mod
from . import threads as thread_ops
from .activity import SessionDeps, SessionOutcome, Span
from .config import LifeConfig
from .lifecycle import mark_seen, term_key, upsert_term
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
from .sources import baike
from .sources.base import SourceError, SourceItem
from .stores import LifeStore, record_lookup_misses, reserve_llm_call, roll_day

logger = logging.getLogger(__name__)

REASONING_ROUTE = "reasoning"


# ───────────────────────────── 浏览 ─────────────────────────────


def _open_thread(
    store: LifeStore,
    cfg: LifeConfig,
    proposal: digest_mod.DigestThread | None,
    episodes: list[dict[str, Any]],
    now: float,
    day: str = "",
) -> None:
    """消化提议的小课题过了门槛就开（名额满了、有相近的都不开）。"""

    if proposal is None or not worth_opening(proposal, episodes):
        return
    opened = thread_ops.open_thread(
        store.threads,
        cfg,
        title      = proposal.title,
        why        = proposal.why,
        next_query = proposal.next_query,
        now        = now,
        topic      = thread_ops.pick_topic(
            store.interests, proposal.topic, proposal.title, proposal.next_query
        ),
        done_when = proposal.done_when,
        kind      = proposal.kind,
    )
    if opened is not None:
        stats.bump(store.state, day, {"thread.opened": 1})


def worth_opening(proposal: digest_mod.DigestThread, episodes: list[dict[str, Any]]) -> bool:
    """开小课题的门槛：有“弄明白到什么程度就算完”和问题类型，并且起因是她这次点开、喜欢（或又爱又嫌）的那条。

    生产里一条随手刷到的做饭视频就开了“锅具预热”，没有结束条件，问题越查越细，占了一整天。
    """

    if (
        not proposal.done_when
        or proposal.kind not in thread_ops.QUESTION_KINDS
        or not proposal.item_id
    ):
        return False
    return any(
        e["item_id"] == proposal.item_id and e["feeling"] in ("like", "mixed") and not e.get("auto")
        for e in episodes
    )


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
    moment = deps.now()
    now = moment.timestamp()
    rows = activity.episode_rows(result, items, decision, start, end, moment.tzinfo)
    stats: dict[str, float] = {}

    def apply(s: LifeStore) -> None:
        known_before, expr_before = activity.known_keys(s.terms), len(s.expressions)
        s.episodes.extend(rows)
        activity.learn(s, result, rows, now)
        _open_thread(s, cfg, result.thread, rows, now, day_key(moment))
        liked = [r for r in rows if r["feeling"] == "like"]
        # 小课题的搜索词（“锅具 预热 判断 温度”）不是话题，不进好奇边界。
        reinforce.grow_frontier(s.frontier, s.interests, liked, {}, cfg.blocked_keywords, now)
        stats.update(
            liked           = activity.liked_count(rows),
            disliked        = sum(1 for r in rows if r["feeling"] == "dislike"),
            new_known       = len(activity.known_keys(s.terms) - known_before),
            new_expressions = max(0, len(s.expressions) - expr_before),
        )
        mark_seen(s.seen, [item.item_id for item in items], now)
        activity.housekeeping(s, cfg, now)
        activity.remember_activity(s.state, decision, start, end)

    await store.mutate(apply)
    outcome = SessionOutcome(
        "ok", "digested", items=len(items), episodes=len(rows), terms=len(result.terms)
    )
    outcome.details.update(stats)
    outcome.source = items[0].source if items else ""
    return outcome


async def _digest(
    store: LifeStore,
    cfg: LifeConfig,
    items: list[SourceItem],
    deps: SessionDeps,
    day: str,
    chat_topic: str = "",
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
        chat_topic    = chat_topic,
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
        if decision.params.get("chat_topic"):
            chat_topics_mod.mark_tried(s.state, str(decision.params["chat_topic"]), span[0])

    await store.mutate(apply)
    return span, day


async def _browse(
    store: LifeStore, cfg: LifeConfig, decision: Decision, span: Span, day: str, deps: SessionDeps
) -> SessionOutcome:
    outcomes: dict[str, bool] = {}
    unhealthy                 = frozenset(
        k
        for k in (*HOT_SOURCE_WEIGHTS, "bili_search")
        if reinforce.health_factor(store.state, k, span[0]) < 1.0
    )
    items, failures = await fetching.gather(cfg, decision, deps.fetch, outcomes, unhealthy)
    await fetching.record_sources(store, outcomes, deps.now().timestamp())
    if deps.on_items is not None:
        deps.on_items(items)
    if not items:
        reason = "all_sources_failed" if failures else "nothing_found"
        return await activity.record_failure(store, reason, decision, span, deps)
    cap   = int(decision.params.get("item_cap") or cfg.max_items_per_session)
    fresh = [i for i in items if i.item_id not in store.seen][: max(1, cap)]
    if not fresh:
        return await _no_news(store, decision, span)
    fresh = await fetching.open_items(
        cfg,
        deps.fetch,
        fresh,
        fetching.interest_topics(store, cfg, span[0]),
        novel_text = fetching.recent_text(store, span[0]) if decision.choice == HOT_SCROLL else "",
        subtitles  = cfg.source_on("bili_subtitles") and deps.subtitles_ok(),
    )
    result, reason = await _digest(
        store, cfg, fresh, deps, day, str(decision.params.get("chat_topic") or "")
    )
    if result is None:
        return await activity.record_failure(
            store, reason, decision, span, deps, failed=reason != "llm_exhausted"
        )
    outcome                  = await _commit(store, cfg, decision, fresh, result, span, deps)
    outcome.details["memes"] = await _collect_memes(store, cfg, fresh, result, day, deps)
    await activity.reinforce_outcome(store, decision, outcome, span, deps)
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


async def _no_news(store: LifeStore, decision: Decision, span: Span) -> SessionOutcome:
    """取到的全是看过的：只记这段时间上过网，不写经历。"""

    def apply(s: LifeStore) -> None:
        activity.remember_activity(s.state, decision, span[0], span[1])

    await store.mutate(apply)
    return SessionOutcome("ok", "all_seen")


# ───────────────────────────── 查词与入口 ─────────────────────────────


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
        return await activity.record_failure(store, "all_sources_failed", decision, span, deps)
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
        hits                    = {entry.term for entry in found}
        record_lookup_misses(s.state, [t for t in settled if t not in hits], now)
        activity.housekeeping(s, cfg, now)
        activity.remember_activity(s.state, decision, span[0], span[1])

    await store.mutate(apply)
    moment = datetime.fromtimestamp(span[0], deps.now().tzinfo)

    def reward_apply(s: LifeStore) -> None:
        reward = reinforce.session_reward(
            liked=0, disliked=0, new_known=len(found), new_expressions=0, thread_progress=0, memes=0
        )
        reinforce.update_preference(s.state, "activity", decision.choice, reward)
        reinforce.record_habit(s.state, moment, reward)

    await store.mutate(reward_apply)
    outcome = SessionOutcome("ok", "looked_up", terms=len(found))
    outcome.details.update(hits=len(found), misses=len(settled) - len(found))
    return outcome


async def run_session(
    store: LifeStore, cfg: LifeConfig, decision: Decision, deps: SessionDeps
) -> SessionOutcome:
    """一次上网。调用方负责单飞与取消；这里只管顺序和落盘。"""

    span, day = await _start(store, cfg, deps, decision)
    if decision.choice == TERM_LOOKUP:
        outcome = await _lookup_terms(store, cfg, decision, span, deps)
    elif decision.choice == THREAD:
        outcome = await research.run(store, cfg, decision, deps, span, day)
    elif decision.choice == WORK_ON:
        outcome = await work_on.run(store, cfg, decision, deps, span, day)
    elif decision.choice in (HOT_SCROLL, INTEREST):
        outcome = await _browse(store, cfg, decision, span, day, deps)
    else:
        outcome = SessionOutcome("skipped", "unsupported_choice")
    await store.mutate(
        lambda s: stats.record_session(
            s.state, day, decision.choice, outcome.status, outcome.reason, outcome
        )
    )
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
