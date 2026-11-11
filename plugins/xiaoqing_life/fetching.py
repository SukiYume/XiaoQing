"""取材：热榜、搜索、点开条目（详情、弹幕、热评、深读）、按兴趣预选，以及来源成败的记录。

浏览（``session``）和研究（``research``）共用；只读外部数据，不写经历和学习结果。
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from typing import Any

from . import reinforce
from .config import LifeConfig
from .lifecycle import interest_weights
from .planner import HOT_SCROLL, HOT_SOURCE_WEIGHTS, Decision
from .safety import normalize_for_match, overlap
from .sources import bilibili, deep, hotlists, watch
from .sources.base import Fetcher, SourceError, SourceItem
from .stores import LifeStore

logger = logging.getLogger(__name__)


COMMENT_ITEMS = 3


async def hot_items(
    cfg: LifeConfig,
    fetch: Fetcher,
    source: str,
    outcomes: dict[str, bool] | None = None,
    unhealthy: frozenset[str]        = frozenset(),
    zone: str                        = "0",
) -> tuple[list[SourceItem], int]:
    """先取选中的热榜源，失败再依次换其余已开启的；返回条目与失败的来源数。

    ``zone`` 是翻 B站 排行榜时进的分区。
    """

    blocked = cfg.blocked_keywords
    readers = {
        "bili_popular": lambda: bilibili.popular(fetch, blocked),
        "bili_ranking": lambda: bilibili.ranking(fetch, blocked, rid=zone),
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


async def search_items(
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


async def gather(
    cfg: LifeConfig,
    decision: Decision,
    fetch: Fetcher,
    outcomes: dict[str, bool] | None = None,
    unhealthy: frozenset[str]        = frozenset(),
) -> tuple[list[SourceItem], int]:
    if decision.choice == HOT_SCROLL:
        return await hot_items(
            cfg,
            fetch,
            str(decision.params.get("source") or "bili_popular"),
            outcomes,
            unhealthy,
            zone=str(decision.params.get("zone") or "0"),
        )
    items, failures = await search_items(
        cfg, fetch, str(decision.params.get("query") or ""), outcomes
    )
    if not items and failures and cfg.source_on("bili_popular"):
        fallback, more = await hot_items(cfg, fetch, "bili_popular", outcomes, unhealthy)
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


def pick_clicks(items: list[SourceItem], topics: list[str], novel_text: str = "") -> list[int]:
    """≤3 个 B 站条目（取热评），按预打分挑，返回它们在 ``items`` 里的下标（保持原顺序）。"""

    candidates = [i for i, item in enumerate(items) if item.source.startswith("bili_")]
    candidates.sort(
        key=lambda i: prescore(items[i], topics) + novelty(items[i], novel_text), reverse=True
    )
    return sorted(candidates[:COMMENT_ITEMS])


async def open_items(
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
    clicks = pick_clicks(items, topics, novel_text)
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


async def record_sources(store: LifeStore, outcomes: dict[str, bool], now: float) -> None:
    """本次各来源成败写进来源可靠性（连续失败 ≥2 次就降权 6 小时）。"""

    if not outcomes:
        return

    def apply(s: LifeStore) -> None:
        for name, ok in outcomes.items():
            reinforce.record_source(s.state, name, ok, now)

    await store.mutate(apply)


def recent_text(store: LifeStore, now: float) -> str:
    cutoff = now - 3 * 86400
    return " ".join(
        f"{e.get('title', '')} {' '.join(e.get('topics', []))} {' '.join(e.get('tags', []))}"
        for e in store.episodes
        if float(e.get("at", 0.0)) >= cutoff
    )


def interest_topics(store: LifeStore, cfg: LifeConfig, now: float) -> list[str]:
    return list(dict.fromkeys([*interest_weights(store.interests, now), *cfg.seed_interests]))
