"""门控与活动选择：睡眠/忙/空闲、睡前、预算、间隔、退避、概率、冷却、无候选即休息。"""

from __future__ import annotations

import random
from typing import Any

import pytest

from plugins.xiaoqing_life import planner
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.stores import default_state
from tests.plugins.xiaoqing_life.support import FREE, at, make_cfg

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()


class Always(random.Random):
    """掷骰固定：``value`` 越小越容易通过概率门。"""

    def __init__(self, value: float = 0.0) -> None:
        super().__init__(1)
        self._value = value
        self._calls = 0

    def random(self) -> float:
        # 第一次是概率门（受 value 控制），之后是抽样：取中位数，分数占优的活动必被选中。
        self._calls += 1
        return self._value if self._calls == 1 else 0.5


def _decide(
    cfg=None, *, state=None, episodes=(), threads=(), interests=None, presence=FREE, rng=None, **kw
):
    return planner.decide(
        cfg or make_cfg(choice_temperature=0.5),
        state if state is not None else default_state(),
        list(episodes),
        list(threads),
        interests if interests is not None else {},
        MOMENT,
        presence,
        rng or Always(0.0),
        **kw,
    )


def test_disabled_asleep_busy_and_running_do_nothing() -> None:
    assert _decide(make_cfg(enabled=False)).reason == "disabled"
    assert _decide(running=True).reason == "session_running"
    assert _decide(presence=Presence("asleep", "睡觉", 0)).reason == "asleep"
    busy = Presence("busy", "收拾宿舍", 120)
    assert _decide(presence=busy).reason == "busy"
    assert _decide(make_cfg(browse_when_busy=True), presence=busy).choice != planner.REST


def test_wind_down_is_chosen_only_with_todays_episodes_and_not_yet_done() -> None:
    near  = Presence("free", "", 30)
    today = [{"id": "e1", "at": NOW - 600}]
    assert _decide(presence=near, episodes=today).choice == planner.WIND_DOWN
    done = default_state() | {"done_on": {"wind_down": "2026-11-05"}}
    assert _decide(presence=near, episodes=today, state=done).reason == "winding_down"
    assert (
        _decide(presence=near, episodes=[{"id": "x", "at": NOW - 86400}]).reason == "winding_down"
    )
    far = Presence("free", "", 61)
    assert _decide(presence=far, episodes=today).choice != planner.WIND_DOWN


def test_daily_budgets_and_rolling_activity_window() -> None:
    state = default_state() | {"day": "2026-11-05", "sessions_today": 5}
    assert _decide(state=state).reason == "sessions_exhausted"
    state = default_state() | {"day": "2026-11-05", "llm_calls_today": 30}
    assert _decide(state=state).reason == "llm_exhausted"
    # 昨天的计数不算今天
    stale = default_state() | {"day": "2026-11-04", "sessions_today": 5, "llm_calls_today": 30}
    assert _decide(state=stale).choice != planner.REST

    busy_log = default_state() | {"activity_log": [{"start": NOW - 3000, "end": NOW - 300}]}
    assert _decide(state=busy_log).reason == "activity_limit"  # 45 分钟
    old_log = default_state() | {"activity_log": [{"start": NOW - 20000, "end": NOW - 12000}]}
    assert _decide(state=old_log).choice != planner.REST  # 窗口外


def test_overlapping_activity_spans_count_once() -> None:
    """手动浏览和还没走完的名义时段重叠：生产里 13+37 分钟的两段实际只占 40 分钟。"""

    start   = NOW - 50 * 60
    overlap = default_state() | {
        "activity_log": [
            {"start": start, "end": start + 13 * 60},
            {"start": start + 3 * 60, "end": start + 40 * 60},
        ]
    }
    assert round(planner._window_minutes(overlap, NOW, 180)) == 40
    assert _decide(state=overlap).reason != "activity_limit"
    nested = default_state() | {
        "activity_log": [
            {"start": NOW - 3000, "end": NOW - 300},
            {"start": NOW - 2000, "end": NOW - 1000},
        ]
    }
    assert round(planner._window_minutes(nested, NOW, 180)) == 45


def test_gap_and_failure_backoff_are_skipped_by_manual_runs_only() -> None:
    recent = default_state() | {"last_session_at": NOW - 60 * 60}
    assert _decide(state=recent).reason == "min_gap"
    assert _decide(state=recent, manual=True).choice != planner.REST
    failed = default_state() | {"last_failure_at": NOW - 30 * 60}
    assert _decide(state=failed).reason == "failure_backoff"
    assert _decide(state=failed, manual=True).choice != planner.REST
    # 手动也不跳过预算
    full = recent | {"day": "2026-11-05", "sessions_today": 5}
    assert _decide(state=full, manual=True).reason == "sessions_exhausted"


def test_probability_roll() -> None:
    assert _decide(rng=Always(0.99)).reason == "dice"
    assert _decide(rng=Always(0.1)).choice != planner.REST
    assert _decide(make_cfg(session_probability=0.0), rng=Always(0.0)).reason == "dice"
    assert _decide(rng=Always(0.99), manual=True).choice != planner.REST


def _thread(**kw: Any) -> dict[str, Any]:
    return {
        "id"        : "t1",
        "title"     : "猎户座",
        "next_query": "猎户座 拍摄",
        "status"    : "open",
        "steps"     : 0,
        "updated_at": NOW,
        **kw,
    }


def test_thread_continue_wins_and_scores_are_recorded() -> None:
    decision = _decide(threads=[_thread(updated_at=NOW - 5 * 3600)])
    assert decision.choice == planner.THREAD
    assert decision.scores[planner.THREAD] == pytest.approx(65.0)
    assert decision.params == {"thread_id": "t1", "query": "猎户座 拍摄"}
    record = decision.record(NOW)
    assert record["choice"] == planner.THREAD and record["reason"] == "sampled"


def test_thread_penalties_cooldown_and_step_limit() -> None:
    recent = [{"choice": planner.THREAD, "thread_id": "t1", "query": "q"}] * 3
    state  = default_state() | {"recent": recent}
    decision = _decide(threads=[_thread(updated_at=NOW - 12 * 3600)], state=state)
    assert decision.scores[planner.THREAD] == pytest.approx(60 + 12 - 36)
    assert decision.scores[planner.HOT_SCROLL] == 40.0 > decision.scores[planner.THREAD]
    assert decision.choice == planner.INTEREST  # 连续续做课题后插入按兴趣搜 (35+20)
    assert planner.THREAD not in _decide(threads=[_thread(last_session_day="2026-11-05")]).scores
    assert planner.THREAD not in _decide(threads=[_thread(steps=3)]).scores
    assert planner.THREAD not in _decide(threads=[_thread(status="reviewing")]).scores


def test_hot_scroll_penalty_and_interest_dive_after_a_grind() -> None:
    hots  = [{"choice": planner.HOT_SCROLL}] * 3
    state = default_state() | {"recent": hots}
    decision = _decide(state=state)
    assert decision.scores[planner.HOT_SCROLL] == 25.0
    assert decision.scores[planner.INTEREST] == 55.0 and decision.choice == planner.INTEREST
    two = default_state() | {"recent": hots[:2]}
    assert _decide(state=two).scores[planner.HOT_SCROLL] == 40.0


def test_interest_dive_gets_bonus_for_positive_evidence() -> None:
    plain = _decide().scores[planner.INTEREST]
    liked = {
        "猫": {"base": 0.0, "evidence": [{"delta": 1, "at": NOW, "item": "a"}], "updated_at": NOW}
    }
    assert _decide(interests=liked).scores[planner.INTEREST] == plain + 8


def test_term_lookup_scores_by_queue_length_and_needs_baike() -> None:
    state = default_state() | {"lookup_queue": ["甲", "乙", "丙", "丁"]}
    decision = _decide(state=state)
    assert decision.scores[planner.TERM_LOOKUP] == 45.0
    assert decision.choice == planner.TERM_LOOKUP and decision.params["terms"] == ["甲", "乙", "丙"]
    off = make_cfg(sources={"baike_lookup": False})
    assert planner.TERM_LOOKUP not in _decide(off, state=state).scores


def test_no_candidates_means_rest_without_records() -> None:
    off = dict.fromkeys(
        (
            "bili_popular",
            "bili_ranking",
            "bili_search",
            "zhihu_hot",
            "weibo_hot",
            "baidu_hot",
            "baike_lookup",
        ),
        False,
    )
    decision = _decide(make_cfg(sources=off))
    assert decision.choice == planner.REST and decision.reason == "no_candidate"


def test_hot_source_is_drawn_from_enabled_ones_by_weight() -> None:
    cfg = make_cfg(
        choice_temperature = 0.5,
        sources            = {"bili_popular": False, "zhihu_hot": False, "bili_ranking": False},
    )
    sources = {
        planner.choose(cfg, default_state(), [], {}, MOMENT, random.Random(n)).params.get("source")
        for n in range(40)
    }
    assert sources == {"weibo_hot"}
    default_sources = {
        planner.choose(make_cfg(), default_state(), [], {}, MOMENT, random.Random(n)).params.get(
            "source"
        )
        for n in range(80)
    }
    assert {"bili_popular", "zhihu_hot", "weibo_hot"} <= default_sources
    assert "baidu_hot" not in default_sources


def test_topic_choice_mixes_interests_and_exploration_and_demotes_repeats() -> None:
    cfg = make_cfg(seed_interests=["猫", "狗"])
    topics = [
        planner.choose_topic(cfg, default_state(), {}, NOW, random.Random(n))[0] for n in range(200)
    ]
    assert "猫" in topics and any(t in planner.EXPLORATION_QUERIES for t in topics)
    explore_share = sum(t in planner.EXPLORATION_QUERIES for t in topics) / len(topics)
    assert 0.15 < explore_share < 0.45

    state   = default_state() | {"recent": [{"choice": planner.INTEREST, "query": "猫"}]}
    demoted = [planner.choose_topic(cfg, state, {}, NOW, random.Random(n))[0] for n in range(200)]
    assert demoted.count("猫") < topics.count("猫")  # 降权但没有封禁
    assert "猫" in demoted


def test_similarity_uses_bigrams_and_difflib() -> None:
    assert planner.similar("猎户座大星云", "猎户座大星云怎么拍")
    assert planner.similar("独立游戏开发", "独立游戏开发幕后")
    assert not planner.similar("天文摄影", "地方美食")
    assert not planner.similar("", "x")
