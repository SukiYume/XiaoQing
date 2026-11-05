"""门控与活动选择（无模型）：规则可解释，分数与理由都写进状态，``/life status`` 可查。"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import LifeConfig
from .lifecycle import interest_weights
from .persona_bridge import Presence
from .safety import similar

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
    "bili_popular": 0.4,
    "zhihu_hot"   : 0.2,
    "weibo_hot"   : 0.2,
    "bili_ranking": 0.1,
    "baidu_hot"   : 0.1,
}
THREAD_COOLDOWN_SECONDS = 180 * 60
_RECENT_WINDOW          = 3
_SIMILAR_PENALTY        = 0.2
_EXPLORE_SHARE          = 0.3


@dataclass(frozen=True, slots=True)
class Decision:
    choice     : str
    reason     : str
    scores     : dict[str, float] = field(default_factory=dict)
    params     : dict[str, Any] = field(default_factory=dict)
    probability: float = 1.0

    def record(self, at: float) -> dict[str, Any]:
        """写进 ``state.last_decision`` 的形式；参数里的查询词不进日志，只进状态。"""

        return {
            "at"         : at,
            "choice"     : self.choice,
            "reason"     : self.reason,
            "scores"     : dict(self.scores),
            "probability": round(self.probability, 3),
        }


def day_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def _rest(reason: str, scores: dict[str, float] | None = None) -> Decision:
    return Decision(REST, reason, scores or {})


# ───────────────────────────── 门控 ─────────────────────────────


def _window_minutes(state: dict[str, Any], now: float, window_minutes: float) -> float:
    """滚动窗口内累计上网分钟数（与窗口相交的部分）。"""

    start = now - window_minutes * 60.0
    total = 0.0
    for span in state.get("activity_log", []):
        a, b = float(span.get("start", 0.0)), float(span.get("end", 0.0))
        total += max(0.0, min(b, now) - max(a, start))
    return total / 60.0


def _has_episode_today(episodes: list[dict[str, Any]], day: str) -> bool:
    return any(e.get("day") == day for e in episodes)


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
        if state.get("wound_down_day") != day and _has_episode_today(episodes, day):
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
    if manual:
        return None
    ended = max(float(state.get("last_session_end", 0.0)), float(state.get("last_session_at", 0.0)))
    if now < ended:
        return _rest("in_session")
    if now - ended < cfg.min_session_gap_minutes * 60.0:
        return _rest("min_gap")
    if now - float(state.get("last_failure_at", 0.0)) < cfg.failure_backoff_minutes * 60.0:
        return _rest("failure_backoff")
    if rng.random() >= cfg.session_probability:
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


def _recent_queries(state: dict[str, Any]) -> list[str]:
    return [str(r.get("query")) for r in state.get("recent", []) if r.get("query")]


def choose_topic(
    cfg: LifeConfig,
    state: dict[str, Any],
    interests: dict[str, dict[str, Any]],
    now: float,
    rng: random.Random,
) -> str:
    """70% 按兴趣分加权，30% 取探索种子；最近搜过的相似话题降权但不封禁。"""

    recent = _recent_queries(state)

    def discount(topic: str, weight: float) -> float:
        return weight * _SIMILAR_PENALTY if any(similar(topic, q) for q in recent) else weight

    if rng.random() >= _EXPLORE_SHARE:
        weights = interest_weights(interests, now)
        for seed in cfg.seed_interests:
            weights.setdefault(seed, 1.0)
        picked = _weighted_pick(rng, {k: discount(k, w) for k, w in weights.items()})
        if picked:
            return picked
    explore = {q: discount(q, 1.0) for q in EXPLORATION_QUERIES}
    return _weighted_pick(rng, explore) or EXPLORATION_QUERIES[0]


# ───────────────────────────── 打分 ─────────────────────────────


def _last_choices(state: dict[str, Any]) -> list[dict[str, Any]]:
    return list(state.get("recent", []))[-_RECENT_WINDOW:]


def _thread_score(thread: dict[str, Any], state: dict[str, Any], now: float) -> float:
    hours   = max(0.0, (now - float(thread.get("updated_at", now))) / 3600.0)
    repeats = sum(1 for r in _last_choices(state) if r.get("thread_id") == thread.get("id"))
    return 60.0 + min(12.0, hours) - 12.0 * repeats


def _best_thread(
    cfg: LifeConfig, state: dict[str, Any], threads: list[dict[str, Any]], now: float
) -> tuple[dict[str, Any], float] | None:
    best: tuple[dict[str, Any], float] | None = None
    for thread in threads:
        if thread.get("status") != "open" or int(thread.get("steps", 0)) >= cfg.thread_max_steps:
            continue
        if float(thread.get("cooldown_until", 0.0)) > now:
            continue
        score = _thread_score(thread, state, now)
        if best is None or score > best[1]:
            best = (thread, score)
    return best


def _enabled_hot_sources(cfg: LifeConfig) -> dict[str, float]:
    return {k: w for k, w in HOT_SOURCE_WEIGHTS.items() if cfg.source_on(k)}


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


def choose(
    cfg: LifeConfig,
    state: dict[str, Any],
    threads: list[dict[str, Any]],
    interests: dict[str, dict[str, Any]],
    moment: datetime,
    rng: random.Random,
) -> Decision:
    """在候选里按分数做 softmax 抽样；没有候选就是休息。参数只为被选中的活动生成。"""

    now                                             = moment.timestamp()
    last                                            = _last_choices(state)
    scores: dict[str, float]                        = {}
    build : dict[str, Callable[[], dict[str, Any]]] = {}

    if (found := _best_thread(cfg, state, threads, now)) and cfg.source_on("bili_search"):
        thread, score = found
        scores[THREAD] = score
        build[THREAD]  = lambda: {
            "thread_id": thread["id"],
            "query"    : str(thread.get("next_query") or thread.get("title") or ""),
        }
    hot = _enabled_hot_sources(cfg)
    if hot:
        repeated = len(last) == _RECENT_WINDOW and all(r.get("choice") == HOT_SCROLL for r in last)
        scores[HOT_SCROLL] = 40.0 - (15.0 if repeated else 0.0)
        build[HOT_SCROLL] = lambda: {"source": _weighted_pick(rng, hot)}
    if cfg.source_on("bili_search"):
        positive = any(
            e.get("delta", 0) > 0 for entry in interests.values() for e in entry.get("evidence", [])
        )
        grind = len(last) == _RECENT_WINDOW and all(
            r.get("choice") in {THREAD, HOT_SCROLL} for r in last
        )
        scores[INTEREST] = 35.0 + (8.0 if positive else 0.0) + (20.0 if grind else 0.0)
        build[INTEREST]  = lambda: {"query": choose_topic(cfg, state, interests, now, rng)}
    queue = list(state.get("lookup_queue", []))
    if queue and cfg.source_on("baike_lookup"):
        scores[TERM_LOOKUP] = 30.0 + min(15.0, 5.0 * len(queue))
        build[TERM_LOOKUP]  = lambda: {"terms": [str(t) for t in queue[:3]]}

    if not scores:
        return _rest("no_candidate")
    chosen, probability = _softmax_pick(scores, cfg.choice_temperature, rng)
    return Decision(chosen, "sampled", scores, build[chosen](), probability)


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
    running: bool = False,
    manual: bool  = False,
) -> Decision:
    blocked = gate(cfg, state, episodes, moment, presence, rng, running=running, manual=manual)
    if blocked is not None:
        return blocked
    return choose(cfg, state, threads, interests, moment, rng)
