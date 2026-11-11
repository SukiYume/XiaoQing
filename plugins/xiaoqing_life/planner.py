"""门控与活动选择（无模型）：规则可解释，分数与理由都写进状态，``/life status`` 可查。"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import chat_topics, reinforce
from .config import LifeConfig
from .days import day_key, episode_day
from .lifecycle import interest_weights
from .persona_bridge import Presence
from .safety import similar
from .stores import done_today
from .works import WORK_ON, best_candidate

REST        = "rest"
WIND_DOWN   = "wind_down"
THREAD      = "thread_continue"
HOT_SCROLL  = "hot_scroll"
INTEREST    = "interest_dive"
TERM_LOOKUP = "term_lookup"

EXPLORATION_QUERIES = (
    "天文摄影",
    "物理实验",
    "动画制作幕后",
    "独立游戏开发",
    "历史文物",
    "乐器演奏",
    "城市建筑",
    "开源软件",
    "动物行为",
    "地方美食",
)
# 热榜源的抽取权重（只在已开启的里面抽）。
HOT_SOURCE_WEIGHTS = {
    "bili_popular": 0.3,
    "bili_ranking": 0.3,
    "zhihu_hot"   : 0.15,
    "weibo_hot"   : 0.15,
    "baidu_hot"   : 0.1,
}
# 翻排行榜时进哪个分区（B站 rid）：动画、游戏各占两份，知识、音乐、鬼畜、科技各一份。
# 全站热门什么都有，刷出来的常常和她没关系；分区榜才是她平时会逛的地方。按小时轮换，不占随机数。
RANKING_ZONES    = ("1", "4", "36", "1", "3", "4", "119", "188")
_RECENT_WINDOW   = 3
_SIMILAR_PENALTY = 0.2
_EXPLORE_SHARE   = 0.3


@dataclass(frozen=True, slots=True)
class Decision:
    choice     : str
    reason     : str
    scores     : dict[str, float] = field(default_factory=dict)
    params     : dict[str, Any] = field(default_factory=dict)
    probability: float = 1.0
    # 自我强化因素（偏好、来源可靠性、习惯、探索欲），写进 last_decision 和 /life status。
    factors: dict[str, Any] = field(default_factory=dict)

    def record(self, at: float) -> dict[str, Any]:
        """写进 ``state.last_decision`` 的形式；参数里的查询词不进日志，只进状态。"""

        return {
            "at"         : at,
            "choice"     : self.choice,
            "reason"     : self.reason,
            "scores"     : dict(self.scores),
            "probability": round(self.probability, 3),
            "factors"    : dict(self.factors),
        }


def _rest(reason: str, scores: dict[str, float] | None = None) -> Decision:
    return Decision(REST, reason, scores or {})


# ───────────────────────────── 门控 ─────────────────────────────


def _window_minutes(state: dict[str, Any], now: float, window_minutes: float) -> float:
    """滚动窗口内累计上网分钟数（与窗口相交的部分）。

    时段先按并集合并：手动触发的一次浏览和还没走完的名义时段重叠时，重叠部分只算一次。
    """

    start = now - window_minutes * 60.0
    spans = sorted(
        (max(float(span.get("start", 0.0)), start), min(float(span.get("end", 0.0)), now))
        for span in state.get("activity_log", [])
    )
    total  = 0.0
    cursor = start
    for a, b in spans:
        a = max(a, cursor)
        if b > a:
            total += b - a
            cursor = b
    return total / 60.0


def _has_episode_today(episodes: list[dict[str, Any]], moment: datetime) -> bool:
    day = day_key(moment)
    return any(episode_day(e, moment.tzinfo) == day for e in episodes)


def _affinity_hit(cfg: LifeConfig, presence: Presence | None, kind: str) -> bool:
    """作息活动文字是否含该类关键词。"""

    activity = presence.activity if presence is not None else ""
    return bool(activity) and any(word in activity for word in cfg.activity_affinity.get(kind, ()))


def gate(
    cfg: LifeConfig,
    state: dict[str, Any],
    episodes: list[dict[str, Any]],
    moment: datetime,
    presence: Presence,
    rng: random.Random,
    *,
    running: bool,
    manual: bool,
) -> Decision | None:
    """依次检查，任何一项不满足就结束；全部通过返回 None，由 ``choose`` 继续。"""

    now = moment.timestamp()
    day = day_key(moment)
    if not cfg.enabled:
        return _rest("disabled")
    if running:
        return _rest("session_running")
    if presence.availability == "asleep":
        return _rest("asleep")
    if presence.availability == "busy" and not cfg.browse_when_busy:
        return _rest("busy")
    until    = presence.minutes_until_sleep
    same_day = state.get("day") == day
    llm_used = state.get("llm_calls_today", 0) if same_day else 0
    if until is not None and until <= cfg.wind_down_minutes:
        if not done_today(state, "wind_down", day) and _has_episode_today(episodes, moment):
            if cfg.max_llm_calls_per_day <= llm_used:
                return _rest("llm_exhausted")
            return Decision(WIND_DOWN, "before_sleep")
        return _rest("winding_down")
    if cfg.max_sessions_per_day <= (state.get("sessions_today", 0) if same_day else 0):
        return _rest("sessions_exhausted")
    if cfg.max_llm_calls_per_day <= llm_used:
        return _rest("llm_exhausted")
    if _window_minutes(state, now, cfg.activity_window_minutes) >= cfg.activity_limit_minutes:
        return _rest("activity_limit")
    if _affinity_hit(cfg, presence, "no_browse"):
        return _rest("activity_busy")
    if manual:
        return None
    ended = max(float(state.get("last_session_end", 0.0)), float(state.get("last_session_at", 0.0)))
    if now < ended:
        return _rest("in_session")
    if now - ended < cfg.min_session_gap_minutes * 60.0:
        return _rest("min_gap")
    if now - float(state.get("last_failure_at", 0.0)) < cfg.failure_backoff_minutes * 60.0:
        return _rest("failure_backoff")
    if cfg.source_on("bili_search") and chat_topics.spark(state, now):
        return None  # 刚挑出来想去看看的群聊话题：这次不掷骰子
    habit = reinforce.habit_multiplier(state, moment)
    if rng.random() >= min(1.0, cfg.session_probability * habit):
        return _rest("dice")
    return None


# ───────────────────────────── 话题 ─────────────────────────────


def _weighted_pick(rng: random.Random, weights: dict[str, float]) -> str | None:
    total = sum(weights.values())
    if total <= 0:
        return None
    point = rng.random() * total
    for key, weight in weights.items():
        point -= weight
        if point <= 0:
            return key
    return next(reversed(weights))


ANIME_QUERIES             = ("新番", "动画推荐", "番剧", "动漫混剪", "轻小说")
ANIME_BONUS               = 10.0
THREAD_PARENT_BONUS       = 4.0
EXPLORE_BONUS             = 10.0
EXPLORE_PATIENCE          = 5
FRONTIER_SHARE            = 0.5
UNRELIABLE_SEARCH_PENALTY = 15.0
ANIME_TOPIC_SHARE         = 0.7
MEAL_HOT_BONUS            = 10.0
MORNING_WINDOW_MIN        = 120
MORNING_THREAD_BONUS      = 6.0
PARKED_RESUME_PENALTY     = 8.0
CHAT_TOPIC_BONUS          = 4.0  # dafeiyu：有朋友聊过的话题可以看时，探索 +4
CHAT_TOPIC_SHARE          = 0.5


def _anime_topic(rng: random.Random) -> str:
    return ANIME_QUERIES[int(rng.random() * len(ANIME_QUERIES)) % len(ANIME_QUERIES)]


def _recent_queries(state: dict[str, Any]) -> list[str]:
    return [str(r.get("query")) for r in state.get("recent", []) if r.get("query")]


def choose_topic(
    cfg: LifeConfig,
    state: dict[str, Any],
    interests: dict[str, dict[str, Any]],
    now: float,
    rng: random.Random,
    frontier: list[dict[str, Any]] | None = None,
    force_explore: bool                   = False,
) -> tuple[str, bool, str]:
    """返回 (话题, 是否探索, 取自 frontier 的话题)。

    70% 按兴趣分加权，30% 探索；探索时 50% 从好奇边界（喜欢的经历带来的新话题，近期的更可能）抽，
    50% 从探索种子抽。最近搜过的相似话题降权但不封禁。``force_explore`` 是“探索欲”：最近一直没探索时。
    """

    recent = _recent_queries(state)

    def discount(topic: str, weight: float) -> float:
        return weight * _SIMILAR_PENALTY if any(similar(topic, q) for q in recent) else weight

    if not force_explore and rng.random() >= _EXPLORE_SHARE:
        weights = interest_weights(interests, now)
        for seed in cfg.seed_interests:
            weights.setdefault(seed, 1.0)
        picked = _weighted_pick(rng, {k: discount(k, w) for k, w in weights.items()})
        if picked:
            return picked, False, ""
    pool = reinforce.frontier_weights(frontier or [], now)
    if pool and rng.random() < FRONTIER_SHARE:
        topic = _weighted_pick(rng, {k: discount(k, w) for k, w in pool.items()})
        if topic:
            return topic, True, topic
    explore = {q: discount(q, 1.0) for q in EXPLORATION_QUERIES}
    return _weighted_pick(rng, explore) or EXPLORATION_QUERIES[0], True, ""


# ───────────────────────────── 打分 ─────────────────────────────


def _last_choices(state: dict[str, Any]) -> list[dict[str, Any]]:
    return list(state.get("recent", []))[-_RECENT_WINDOW:]


def _thread_score(thread: dict[str, Any], state: dict[str, Any], now: float) -> float:
    hours        = max(0.0, (now - float(thread.get("updated_at", now))) / 3600.0)
    repeats      = sum(1 for r in _last_choices(state) if r.get("thread_id") == thread.get("id"))
    parent_bonus = THREAD_PARENT_BONUS if thread.get("parent_id") else 0.0
    return 60.0 + min(12.0, hours) - 12.0 * repeats + parent_bonus


def _best_thread(
    cfg: LifeConfig, state: dict[str, Any], threads: list[dict[str, Any]], moment: datetime
) -> tuple[dict[str, Any], float] | None:
    """可续做的小课题里分最高的。同一个小课题一天最多查一次（隔天接着查），免得一天都耗在一个问题上。"""

    now                                       = moment.timestamp()
    today                                     = day_key(moment)
    best: tuple[dict[str, Any], float] | None = None
    for thread in threads:
        if thread.get("last_session_day") == today:
            continue
        status = thread.get("status")
        if status == "parked":
            # 搁置的小课题过了 ``parked_until`` 才作为续做候选，分数 −8。
            if float(thread.get("parked_until", 0.0)) > now:
                continue
        elif status != "open" or int(thread.get("steps", 0)) >= cfg.thread_max_steps:
            continue
        score = _thread_score(thread, state, now) - (
            PARKED_RESUME_PENALTY if status == "parked" else 0.0
        )
        if best is None or score > best[1]:
            best = (thread, score)
    return best


def _enabled_hot_sources(
    cfg: LifeConfig, state: dict[str, Any] | None = None, now: float = 0.0
) -> dict[str, float]:
    """已开启的热榜源及权重；有记录时乘上“最近喜不喜欢”和“最近稳不稳”两个因子。"""

    weights = {k: w for k, w in HOT_SOURCE_WEIGHTS.items() if cfg.source_on(k)}
    if state is None:
        return weights
    return {
        k: w * reinforce.source_pref_factor(state, k) * reinforce.health_factor(state, k, now)
        for k, w in weights.items()
    }


def _softmax_pick(
    scores: dict[str, float], temperature: float, rng: random.Random
) -> tuple[str, float]:
    """``p ∝ exp((s - max) / T)``：分高的更常被选中，但不是每次都选它。"""

    top     = max(scores.values())
    weights = {k: math.exp((v - top) / temperature) for k, v in scores.items()}
    total   = sum(weights.values())
    point   = rng.random() * total
    chosen  = next(reversed(weights))
    for key, weight in weights.items():
        point -= weight
        if point <= 0:
            chosen = key
            break
    return chosen, weights[chosen] / total


def _explore_overdue(state: dict[str, Any]) -> bool:
    """最近 5 次上网都没有探索过：下一次 interest_dive 进入探索模式（避免越过越窄）。"""

    last = list(state.get("recent", []))[-EXPLORE_PATIENCE:]
    return len(last) == EXPLORE_PATIENCE and not any(r.get("explored") for r in last)


def _interest_params(
    cfg: LifeConfig,
    state: dict[str, Any],
    interests: dict[str, dict[str, Any]],
    now: float,
    rng: random.Random,
    frontier: list[dict[str, Any]] | None,
    *,
    anime: bool,
    overdue: bool,
) -> dict[str, Any]:
    if anime and rng.random() < ANIME_TOPIC_SHARE:
        return {"query": _anime_topic(rng), "explored": False}
    picks = chat_topics.fresh(state, now)
    if picks and rng.random() < CHAT_TOPIC_SHARE:
        # 朋友们在聊的：搜她睡前自己写下的搜索词；话题本身不是她的兴趣证据。
        return {"query": picks[0]["query"], "explored": True, "chat_topic": picks[0]["topic"]}
    topic, explored, from_frontier = choose_topic(
        cfg, state, interests, now, rng, frontier, force_explore=overdue
    )
    params: dict[str, Any] = {"query": topic, "explored": explored}
    if from_frontier:
        params["frontier"] = from_frontier
    return params


def _apply_reinforcement(
    scores: dict[str, float], factors: dict[str, Any], state: dict[str, Any], now: float
) -> None:
    """活动偏好（EMA）加成；搜索接口近期不稳时，依赖它的活动整体扣分。写入因素供状态页查看。"""

    pref: dict[str, float] = {}
    for name in scores:
        adjust = reinforce.pref_adjust(state, "activity", name)
        if adjust:
            scores[name] += adjust
            pref[name] = round(adjust, 2)
    if pref:
        factors["pref"] = pref
    if reinforce.health_factor(state, "bili_search", now) < 1.0:
        # 研究可以改用百科 / 维基，所以搜索不稳只给按兴趣搜扣分。
        if INTEREST in scores:
            scores[INTEREST] -= UNRELIABLE_SEARCH_PENALTY
        factors["unreliable"] = ["bili_search"]
    hot_down = [k for k in HOT_SOURCE_WEIGHTS if reinforce.health_factor(state, k, now) < 1.0]
    if hot_down:
        factors["unreliable_hot"] = hot_down


def choose(
    cfg: LifeConfig,
    state: dict[str, Any],
    threads: list[dict[str, Any]],
    interests: dict[str, dict[str, Any]],
    moment: datetime,
    rng: random.Random,
    presence: Presence | None             = None,
    frontier: list[dict[str, Any]] | None = None,
    works: list[dict[str, Any]] | None    = None,
    episodes: list[dict[str, Any]] | None = None,
) -> Decision:
    """在候选里按分数做 softmax 抽样；没有候选就是休息。参数只为被选中的活动生成。"""

    now                                              = moment.timestamp()
    last                                             = _last_choices(state)
    scores : dict[str, float]                        = {}
    build  : dict[str, Callable[[], dict[str, Any]]] = {}
    factors: dict[str, Any]                          = {}

    research_ok = (
        cfg.source_on("bili_search") or cfg.source_on("baike_lookup") or cfg.source_on("wiki_read")
    )
    if research_ok and (found := _best_thread(cfg, state, threads, moment)):
        thread, score = found
        since_wake     = presence.minutes_since_wake if presence is not None else None
        morning        = since_wake is not None and since_wake <= MORNING_WINDOW_MIN
        scores[THREAD] = score + (MORNING_THREAD_BONUS if morning else 0.0)
        build[THREAD]  = lambda: {
            "thread_id": thread["id"],
            "query"    : str(thread.get("next_query") or thread.get("title") or ""),
        }
    hot = _enabled_hot_sources(cfg, state, now)
    if hot:
        repeated = len(last) == _RECENT_WINDOW and all(r.get("choice") == HOT_SCROLL for r in last)
        meal = _affinity_hit(cfg, presence, "meal")
        scores[HOT_SCROLL] = 40.0 - (15.0 if repeated else 0.0) + (MEAL_HOT_BONUS if meal else 0.0)
        build[HOT_SCROLL] = lambda: {
            "source": _weighted_pick(rng, hot),
            "zone"  : RANKING_ZONES[int(now // 3600) % len(RANKING_ZONES)],
            **({"item_cap": max(1, cfg.max_items_per_session // 2)} if meal else {}),
        }
    if cfg.source_on("bili_search"):
        positive = any(
            e.get("delta", 0) > 0 for entry in interests.values() for e in entry.get("evidence", [])
        )
        grind = len(last) == _RECENT_WINDOW and all(
            r.get("choice") in {THREAD, HOT_SCROLL} for r in last
        )
        anime            = _affinity_hit(cfg, presence, "anime")
        scores[INTEREST] = (
            35.0
            + (8.0 if positive else 0.0)
            + (20.0 if grind else 0.0)
            + (ANIME_BONUS if anime else 0.0)
        )
        overdue = _explore_overdue(state)
        scores[INTEREST] += EXPLORE_BONUS if overdue else 0.0
        if overdue:
            factors["explore"] = EXPLORE_BONUS
        if chat_topics.fresh(state, now):
            scores[INTEREST] += CHAT_TOPIC_BONUS
            factors["chat_topic"] = CHAT_TOPIC_BONUS
        build[INTEREST] = lambda: _interest_params(
            cfg, state, interests, now, rng, frontier, anime=anime, overdue=overdue
        )
    if works and (found_work := best_candidate(cfg, works, state, episodes or [], threads, now)):
        work, work_score = found_work
        scores[WORK_ON] = work_score
        build[WORK_ON]  = lambda: {"work_id": work["id"], "query": str(work.get("title", ""))}
    queue = list(state.get("lookup_queue", []))
    if queue and cfg.source_on("baike_lookup"):
        scores[TERM_LOOKUP] = 30.0 + min(15.0, 5.0 * len(queue))
        build[TERM_LOOKUP]  = lambda: {"terms": [str(t) for t in queue[:3]]}

    if not scores:
        return _rest("no_candidate")
    _apply_reinforcement(scores, factors, state, now)
    habit = reinforce.habit_multiplier(state, moment)
    if habit != 1.0:
        factors["habit"] = round(habit, 3)
    chosen, probability = _softmax_pick(scores, cfg.choice_temperature, rng)
    return Decision(chosen, "sampled", scores, build[chosen](), probability, factors)


def decide(
    cfg: LifeConfig,
    state: dict[str, Any],
    episodes: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    interests: dict[str, dict[str, Any]],
    moment: datetime,
    presence: Presence,
    rng: random.Random,
    *,
    running: bool                         = False,
    manual: bool                          = False,
    frontier: list[dict[str, Any]] | None = None,
    works: list[dict[str, Any]] | None    = None,
) -> Decision:
    blocked = gate(cfg, state, episodes, moment, presence, rng, running=running, manual=manual)
    if blocked is not None:
        return blocked
    return choose(cfg, state, threads, interests, moment, rng, presence, frontier, works, episodes)
