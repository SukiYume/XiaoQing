"""第四轮：自我强化的日常决策、自由探索、自我成长与注入文字。"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import commands, planner, reinforce, wind_down
from plugins.xiaoqing_life import lifecycle as lc
from plugins.xiaoqing_life import threads as th
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.session import SessionDeps, novelty, pick_clicks, run_session
from plugins.xiaoqing_life.sources.base import SourceItem
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.plugins.xiaoqing_life.support import (
    FREE,
    FakeAI,
    FakeFetcher,
    at,
    comments_payload,
    make_cfg,
)

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()
HOUR   = 3600.0
DAY    = 86400.0


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


# ───────────────────────────── 收获与偏好 ─────────────────────────────


def test_session_reward_formula_and_clamp() -> None:
    reward = reinforce.session_reward(
        liked=2, disliked=1, new_known=2, new_expressions=1, thread_progress=1, memes=1
    )
    assert reward == 3.0  # 2 − 0.5 + 1 + 0.3 + 1 + 0.5 = 4.3，截断到 3
    assert (
        reinforce.session_reward(
            liked=0, disliked=4, new_known=0, new_expressions=0, thread_progress=0, memes=0
        )
        == -1.0
    )
    assert (
        reinforce.session_reward(
            liked=1, disliked=0, new_known=0, new_expressions=0, thread_progress=0, memes=0
        )
        == 1.0
    )


def test_ema_preferences_adjust_scores_within_bounds_and_sources_scale_weights() -> None:
    state = default_state()
    assert reinforce.update_preference(state, "activity", "interest_dive", 2.0) == 2.0
    assert reinforce.update_preference(state, "activity", "hot_scroll", -1.0) == -1.0
    assert reinforce.update_preference(state, "activity", "interest_dive", 0.0) == pytest.approx(
        1.4
    )  # 2 + 0.3×(0−2)
    up   = reinforce.pref_adjust(state, "activity", "interest_dive")
    down = reinforce.pref_adjust(state, "activity", "hot_scroll")
    assert up == pytest.approx(8 * (1.4 - 0.2)) or up == 8.0
    assert up == 8.0 and down == -8.0  # 两端都截断在 ±8
    assert reinforce.pref_adjust(state, "activity", "term_lookup") == 0.0
    reinforce.update_preference(state, "source", "zhihu_hot", -1.0)
    reinforce.update_preference(state, "source", "bili_popular", 1.0)
    assert reinforce.source_pref_factor(state, "zhihu_hot") == pytest.approx(2.718281828**-0.5)
    assert reinforce.source_pref_factor(state, "bili_popular") == pytest.approx(2.718281828**0.5)
    assert reinforce.source_pref_factor(state, "weibo_hot") == 1.0


def test_planner_applies_preferences_and_records_the_factors() -> None:
    state = default_state()
    reinforce.update_preference(state, "activity", "interest_dive", 1.5)
    reinforce.update_preference(state, "activity", "hot_scroll", -0.5)
    cfg = make_cfg(choice_temperature=0.5)
    base  = planner.choose(cfg, default_state(), [], {}, MOMENT, random.Random(1))
    tuned = planner.choose(cfg, state, [], {}, MOMENT, random.Random(1))
    assert tuned.scores["interest_dive"] > base.scores["interest_dive"]
    assert tuned.scores["hot_scroll"] < base.scores["hot_scroll"]
    assert set(tuned.factors["pref"]) == {"interest_dive", "hot_scroll"}
    assert tuned.record(NOW)["factors"]["pref"]["interest_dive"] > 0
    assert tuned.choice == "interest_dive"


# ───────────────────────────── 来源可靠性 ─────────────────────────────


def test_two_consecutive_failures_cut_a_source_for_six_hours_then_it_recovers() -> None:
    state = default_state()
    reinforce.record_source(state, "zhihu_hot", False, NOW)
    assert reinforce.health_factor(state, "zhihu_hot", NOW) == 1.0
    reinforce.record_source(state, "zhihu_hot", True, NOW + 10)  # 成功后重新数
    reinforce.record_source(state, "zhihu_hot", False, NOW + 20)
    assert reinforce.health_factor(state, "zhihu_hot", NOW + 20) == 1.0
    reinforce.record_source(state, "zhihu_hot", False, NOW + 30)
    assert reinforce.health_factor(state, "zhihu_hot", NOW + 31) == 0.2
    assert reinforce.health_factor(state, "zhihu_hot", NOW + 30 + 6 * HOUR - 1) == 0.2
    assert reinforce.health_factor(state, "zhihu_hot", NOW + 30 + 6 * HOUR + 1) == 1.0
    reinforce.record_source(state, "zhihu_hot", False, NOW + 7 * HOUR)  # 过期后从头数
    assert reinforce.health_factor(state, "zhihu_hot", NOW + 7 * HOUR) == 1.0


def test_unreliable_sources_lose_weight_and_a_failing_search_cools_the_activities_that_need_it() -> (
    None
):
    state = default_state()
    for i in range(2):
        reinforce.record_source(state, "zhihu_hot", False, NOW + i)
        reinforce.record_source(state, "bili_search", False, NOW + i)
    cfg = make_cfg(choice_temperature=0.5)
    weights = planner._enabled_hot_sources(cfg, state, NOW + 5)
    assert weights["zhihu_hot"] == pytest.approx(0.2 * 0.2) and weights["bili_popular"] == 0.4
    out = planner.choose(cfg, state, [], {}, MOMENT.replace(), random.Random(2))
    assert out.scores["interest_dive"] == 35 - 15 or "unreliable" in out.factors
    assert out.factors.get("unreliable") == ["bili_search"] or out.factors.get("unreliable_hot")
    fresh = planner.choose(cfg, default_state(), [], {}, MOMENT, random.Random(2))
    assert fresh.scores["interest_dive"] == 35.0 and "unreliable" not in fresh.factors


# ───────────────────────────── 话题强化 ─────────────────────────────

DIVE_NONE = {
    "looked_at": [{"item": "i1", "reaction": "没意思", "feeling": "meh", "topics": ["别的"]}]
}


def _search_fetch() -> FakeFetcher:
    return FakeFetcher(
        {
            "/search/type": {
                "code": 0,
                "data": {
                    "result": [
                        {"id": 5, "bvid": "BV5", "title": "某个视频", "description": "d", "play": 5}
                    ]
                },
            },
            "/reply/main"        : comments_payload(),
            "/web-interface/view": {"code": -404},
            "/tag/archive"       : {"code": -404},
        }
    )


async def _dive(store: LifeStore, query: str, reply: dict[str, Any], moment=MOMENT) -> None:
    ai = FakeAI(reply)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    deps = SessionDeps(
        fetch=_search_fetch(), complete=complete, rng=random.Random(1), now=lambda: moment
    )
    await run_session(
        store, make_cfg(), planner.Decision("interest_dive", "s", {}, {"query": query}), deps
    )


@pytest.mark.asyncio
async def test_a_dive_without_likes_leaves_one_mild_negative_note_per_topic_per_day(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    lc.seed_interests(store.interests, ("天文摄影",), NOW)
    await _dive(store, "天文摄影", DIVE_NONE)
    entry = store.interests["天文摄影"]
    assert (
        [e["delta"] for e in entry["evidence"]] == [-0.5] and entry["base"] == 1.0 and entry["seed"]
    )
    assert lc.interest_score(entry, NOW) == pytest.approx(0.5)
    store.seen.clear()
    await _dive(store, "天文摄影", DIVE_NONE)
    assert len(entry["evidence"]) == 1  # 同一话题同一天只记一次
    store.seen.clear()
    await _dive(store, "天文摄影", DIVE_NONE, moment=MOMENT + timedelta_days(1))
    assert len(entry["evidence"]) == 2
    assert "天文摄影" in store.interests  # 种子不会被清掉
    store.seen.clear()
    liked = {
        "looked_at": [{"item": "i1", "reaction": "好看", "feeling": "like", "topics": ["别的"]}]
    }
    await _dive(store, "别的话题", liked)
    assert "别的话题" not in store.state["topic_penalty"]


def timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=days)


# ───────────────────────────── 习惯 ─────────────────────────────


def test_hour_of_week_habit_multiplier_description_and_the_dice() -> None:
    state   = default_state()
    evening = at(19, 0)
    for day, reward in ((2, 2.0), (3, 2.5), (4, 3.0)):
        reinforce.record_habit(state, at(19, 0, day=day), reward)
    assert (
        reinforce.habit_multiplier(state, evening) == 1.0
        or reinforce.habit_multiplier(state, evening) >= 1.0
    )
    for day in (2, 3, 4):
        reinforce.record_habit(state, at(9, 0, day=day), -1.0)  # 工作日上午收获差
    assert (
        reinforce.habit_bucket(evening) == "wd19"
        and reinforce.habit_bucket(at(10, 0, day=7)) == "we10"
    )
    high = reinforce.habit_multiplier(state, evening)
    low  = reinforce.habit_multiplier(state, at(9, 0))
    assert 1.0 < high <= 1.3 and 0.7 <= low < 1.0
    assert reinforce.habit_multiplier(state, at(3, 0)) == 1.0  # 没有样本
    reinforce.record_habit(state, at(15, 0, day=2), 0.0)
    assert reinforce.habit_multiplier(state, at(15, 0, day=2)) == 1.0  # 样本不足 3 个
    assert "工作日19点" in reinforce.habit_text(state)
    assert reinforce.habit_text(default_state()) == ""
    # 掷骰：概率 0.5 × 习惯乘数
    cfg = make_cfg(session_probability=0.5)

    class Roll(random.Random):
        def __init__(self, value: float) -> None:
            super().__init__(0)
            self.value = value

        def random(self) -> float:
            return self.value

    def decided(moment, roll: float) -> str:
        return planner.decide(cfg, state, [], [], {}, moment, FREE, Roll(roll)).reason

    assert decided(evening, 0.55) != "dice"  # 0.5×1.xx 之内
    assert decided(at(9, 0), 0.45) == "dice"  # 0.5×0.7x < 0.45
    assert planner.choose(cfg, state, [], {}, evening, random.Random(1)).factors["habit"] == round(
        high, 3
    )


# ───────────────────────────── 说法的社交反馈 ─────────────────────────────


def _term(store: LifeStore, name: str, **kw: Any) -> dict[str, Any]:
    entry = {
        "term"          : name,
        "meaning"       : "含义",
        "usage"         : "场合",
        "tone"          : "玩笑",
        "status"        : "known",
        "verified"      : False,
        "sources"       : ["a", "b"],
        "first_seen"    : NOW - 5 * DAY,
        "last_seen"     : NOW - HOUR,
        "last_suggested": NOW - 600.0,
        **kw,
    }
    store.terms[name] = entry
    return entry


def _feedback(store: LifeStore, recent: list[str], bot: list[str], now: float = NOW) -> bool:
    return reinforce.apply_feedback(store.terms, store.expressions, recent, bot, now)


def test_social_feedback_counts_ok_and_confused_once_per_reply(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ok_term, bad_term = _term(store, "哈基米"), _term(store, "南北绿豆")
    recent = ["看这猫", "哈基米来了", "哈哈哈哈确实", "南北绿豆懂吧", "啥意思", "?"]
    bot    = ["哈基米来了", "南北绿豆懂吧"]
    assert _feedback(store, recent, bot) is True
    assert (ok_term["use_ok"], ok_term.get("use_confused", 0)) == (1, 0)
    assert (bad_term["use_confused"], bad_term.get("use_ok", 0)) == (1, 0)
    assert _feedback(store, recent, bot) is False  # 同一条回复同一个词只评一次
    assert ok_term["use_ok"] == 1 and bad_term["use_confused"] == 1
    # 对方也跟着用这个词，同样算 ok
    other = _term(store, "绷不住")
    assert (
        _feedback(store, ["绷不住了", "我也绷不住"], ["绷不住了"]) is True and other["use_ok"] == 1
    )


def test_social_feedback_ignores_old_suggestions_unanswered_lines_and_her_own_followups(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    old = _term(store, "老梗", last_suggested=NOW - 3 * HOUR)
    assert _feedback(store, ["老梗哈哈", "哈哈哈"], ["老梗哈哈"]) is False and "use_ok" not in old
    fresh = _term(store, "新梗")
    assert _feedback(store, ["新梗来了"], ["新梗来了"]) is False  # 她这句后面还没有任何人说话
    assert _feedback(store, ["新梗来了", "新梗再来", "好笑"], ["新梗来了", "新梗再来"]) is True
    assert fresh["use_ok"] == 1  # 只算第一句之后、下一句她自己的话之前的反应
    assert _feedback(store, ["无关闲聊", "哈哈"], ["无关闲聊"]) is False


def test_expression_feedback_uses_the_pattern_overlap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    expr  = {
        "id"            : "x1",
        "situation"     : "夸可爱",
        "pattern"       : "猫猫基础猫猫不基础",
        "kind"          : "phrase",
        "sources"       : ["a", "b"],
        "first_seen"    : NOW,
        "last_seen"     : NOW,
        "last_suggested": NOW - 300.0,
    }
    store.expressions.append(expr)
    line = "这个猫猫基础，猫猫不基础的说法太绝了"
    assert _feedback(store, [line, "笑死"], [line]) is True and expr["use_ok"] == 1


def test_habit_halves_the_cooldown_and_is_preferred_while_confused_terms_are_suppressed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    cfg = make_cfg(active_use_probability=1.0, suggest_cooldown_hours=6.0)
    habit = _term(
        store,
        "口头禅",
        meaning        = "看到猫图拍得好可爱时感叹",
        usage          = "夸猫",
        use_ok         = 2,
        last_suggested = NOW - 4 * HOUR,
    )
    plain = _term(
        store, "普通词", meaning="看到猫图拍得好可爱时感叹", usage="夸猫", last_suggested=0.0
    )
    assert reinforce.is_habit(habit) and tc._cooled(habit, NOW, cfg)  # 6h 的一半是 3h，已过
    assert not tc._cooled(_term(store, "对照", last_suggested=NOW - 4 * HOUR), NOW, cfg)
    req    = {"text": "你家猫图拍得好可爱，是什么猫", "recent": [], "bot_recent": []}
    picked = tc.pick_usable_term(store, req, NOW, cfg, random.Random(0))
    assert picked is not None and picked[1] is habit  # 习惯优先
    habit["last_suggested"] = NOW
    picked                  = tc.pick_usable_term(store, req, NOW, cfg, random.Random(0))
    assert picked is not None and picked[1] is plain
    # 困惑 ≥2 且没有 ok：14 天内不推荐，但仍能听懂
    plain["use_confused"], plain["use_ok"] = 1, 0
    reinforce._record_use(plain, "confused", NOW)
    assert reinforce.is_suppressed(plain, NOW + 13 * DAY) and not reinforce.is_suppressed(
        plain, NOW + 15 * DAY
    )
    assert tc.pick_usable_term(store, req, NOW, cfg, random.Random(0)) is None
    payload    = {"text": "普通词是啥", "recent": [], "bot_recent": [], "unknown_terms": []}
    understood = tc.understood_terms(store, tc.clean_payload(payload))
    assert any(key == "普通词" for key, _ in understood)
    mixed = _term(store, "又好又坏", use_ok=1)
    reinforce._record_use(mixed, "confused", NOW)
    reinforce._record_use(mixed, "confused", NOW)
    assert not reinforce.is_suppressed(mixed, NOW)  # 有 ok 就不压


def test_turn_context_applies_feedback_from_the_ordered_recent_lines_and_reports_a_change(
    tmp_path: Path,
) -> None:
    store   = _store(tmp_path)
    entry   = _term(store, "哈基米")
    payload = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : "继续",
        "recent"       : ["哈基米来了", "哈哈哈", "继续"],
        "bot_recent"   : ["哈基米来了"],
        "unknown_terms": [],
    }
    _response, dirty = tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0))
    assert dirty and entry["use_ok"] == 1
    _response, dirty = tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0))
    assert not dirty


def test_the_chat_request_keeps_assistant_and_user_lines_in_order() -> None:
    from types import SimpleNamespace

    from plugins.xiaoqing_chat.life.online import build_request

    history = [
        SimpleNamespace(role="user", content="看这猫"),
        SimpleNamespace(role="assistant", content="哈基米来了"),
        SimpleNamespace(role="user", content="哈哈哈确实"),
        SimpleNamespace(role="assistant", content="嘿嘿"),
    ]
    request = build_request(text="继续", history=history, is_private=False, unknown_words=[])
    assert request["recent"] == ["看这猫", "哈基米来了", "哈哈哈确实", "嘿嘿"]
    assert request["bot_recent"] == ["哈基米来了", "嘿嘿"]
    store              = LifeStore(Path("."))
    store.terms["哈基米"] = {"term": "哈基米", "sources": ["a", "b"], "last_suggested": NOW - 60.0}
    assert reinforce.apply_feedback(store.terms, [], request["recent"], request["bot_recent"], NOW)
    assert store.terms["哈基米"]["use_ok"] == 1


# ───────────────────────────── 自由探索 ─────────────────────────────


def _liked_row(n: int, **kw: Any) -> dict[str, Any]:
    return {
        "id"     : f"e{n}",
        "tags"   : ["赤道仪", "星空", "x"],
        "topics" : ["天文摄影"],
        "feeling": "like",
        **kw,
    }


def test_frontier_grows_from_liked_episodes_with_filters_and_a_cap() -> None:
    frontier: list[dict[str, Any]] = []
    interests                      = {
        "天文摄影": {"stage": "consistent", "evidence": [], "base": 0, "updated_at": NOW},
        "地方美食": {"stage": "tentative", "evidence": [], "base": 0, "updated_at": NOW},
    }
    row = _liked_row(1, tags=["赤道仪", "星空", "加微信群", "天文摄影观测", "x", "地方美食"])
    added = reinforce.grow_frontier(
        frontier, interests, [row], {"e1": ["猎户座", "拍摄"]}, ("加微信",), NOW
    )
    topics = [f["topic"] for f in frontier]
    assert added == len(topics) and {"赤道仪", "星空", "猎户座", "拍摄"} <= set(topics)
    assert "加微信群" not in topics  # 屏蔽词
    assert "天文摄影" not in topics and "天文摄影观测" not in topics  # 已经是强兴趣（含相近）
    assert "x" not in topics and "地方美食" not in topics  # 太短 / 已在兴趣里
    assert reinforce.grow_frontier(frontier, interests, [row], {}, (), NOW) == 0  # 去重
    many = [_liked_row(n, tags=[f"话题{n:02d}号"], topics=[]) for n in range(80)]
    reinforce.grow_frontier(frontier, interests, many, {}, (), NOW + 1)
    assert len(frontier) <= 50
    assert frontier[0]["from_episode"] and set(frontier[0]) == {
        "topic",
        "from_episode",
        "added_at",
        "tried",
    }


def test_exploration_draws_half_from_the_frontier_and_marks_it_tried(tmp_path: Path) -> None:
    frontier = [
        {"topic": "赤道仪", "from_episode": "e1", "added_at": NOW - HOUR, "tried": False},
        {"topic": "旧话题", "from_episode": "e2", "added_at": NOW - 30 * DAY, "tried": True},
    ]
    cfg = make_cfg(seed_interests=["猫"])
    picks = [
        planner.choose_topic(
            cfg, default_state(), {}, NOW, random.Random(n), frontier, force_explore=True
        )
        for n in range(200)
    ]
    from_frontier = [p for p in picks if p[2]]
    assert all(p[1] for p in picks) and {p[2] for p in from_frontier} == {
        "赤道仪"
    }  # 试过的不会再抽
    assert 0.35 < len(from_frontier) / len(picks) < 0.65
    assert any(p[0] in planner.EXPLORATION_QUERIES for p in picks)
    normal = [
        planner.choose_topic(cfg, default_state(), {}, NOW, random.Random(n), frontier)
        for n in range(200)
    ]
    assert 0.1 < sum(p[1] for p in normal) / 200 < 0.45  # 默认 30% 探索
    assert planner.choose_topic(cfg, default_state(), {}, NOW, random.Random(1), frontier)[0]


@pytest.mark.asyncio
async def test_a_session_started_from_the_frontier_marks_it_tried_and_remembers_it_was_exploration(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.frontier.append(
        {"topic": "赤道仪", "from_episode": "e1", "added_at": NOW, "tried": False}
    )
    ai = FakeAI(
        {"looked_at": [{"item": "i1", "reaction": "不错", "feeling": "like", "topics": ["赤道仪"]}]}
    )

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    deps = SessionDeps(
        fetch=_search_fetch(), complete=complete, rng=random.Random(1), now=lambda: MOMENT
    )
    decision = planner.Decision(
        "interest_dive", "s", {}, {"query": "赤道仪", "explored": True, "frontier": "赤道仪"}
    )
    await run_session(store, make_cfg(), decision, deps)
    assert store.frontier[0]["tried"] is True and store.state["recent"][-1]["explored"] is True
    assert store.interests["赤道仪"]["evidence"][0]["delta"] == 1  # 喜欢就按正常证据长成兴趣


def test_five_sessions_without_exploring_turn_on_exploration_mode() -> None:
    state           = default_state()
    state["recent"] = [{"choice": "interest_dive", "explored": False}] * 5
    cfg = make_cfg(choice_temperature=0.5)
    out = planner.choose(cfg, state, [], {}, MOMENT, random.Random(3))
    assert out.scores["interest_dive"] == 35.0 + 10.0 and out.factors["explore"] == 10.0
    topics = {
        planner.choose(cfg, state, [], {}, MOMENT, random.Random(n)).params.get("query")
        for n in range(30)
    }
    assert topics & set(planner.EXPLORATION_QUERIES)  # 探索模式里的话题来自探索种子
    state["recent"][-1]["explored"] = True
    assert (
        planner.choose(cfg, state, [], {}, MOMENT, random.Random(3)).scores["interest_dive"] == 35.0
    )
    state["recent"] = state["recent"][-4:]
    assert "explore" not in planner.choose(cfg, state, [], {}, MOMENT, random.Random(3)).factors


def test_hot_scroll_preselection_gives_a_novelty_bonus_to_unfamiliar_items() -> None:
    familiar = SourceItem("bili:1", "bili_popular", "天文摄影入门", "", "u", 100, ("赤道仪",))
    fresh    = SourceItem("bili:2", "bili_popular", "古建筑修复纪录片", "", "u", 100, ("建筑",))
    recent   = "天文摄影入门教程 赤道仪 星空"
    assert novelty(familiar, recent) == 0.0 and novelty(fresh, recent) > 0.0
    assert novelty(fresh, "") == 0.0
    picks = pick_clicks(
        [familiar, fresh, SourceItem("bili:3", "bili_popular", "别的", "", "u", 100, ())],
        [],
        novel_text=recent,
    )
    assert picks[:1] != [] and 1 in picks


# ───────────────────────────── 自我成长 ─────────────────────────────


def _thread(
    tid: str, status: str, notes: list[str], depth: int = 0, parent: str = ""
) -> dict[str, Any]:
    return {
        "id"            : tid,
        "title"         : f"课题{tid}",
        "why"           : "",
        "next_query"    : f"查{tid}",
        "status"        : status,
        "steps"         : 1,
        "empty_streak"  : 0,
        "cooldown_until": 0.0,
        "notes"         : [{"episode_id": n, "note": "要点"} for n in notes],
        "takeaway"      : "",
        "created_at"    : NOW - DAY,
        "updated_at"    : NOW - 100,
        "parent_id"     : parent,
        "depth"         : depth,
    }


def _episode(n: int, ago: float = 600.0) -> dict[str, Any]:
    return {
        "id"      : f"e{n}",
        "at"      : NOW - ago,
        "day"     : "2026-11-05",
        "title"   : f"视频{n}",
        "reaction": "有意思",
        "feeling" : "like",
        "gist"    : "",
        "topics"  : [],
    }


async def _wind(store: LifeStore, reply: dict[str, Any], **cfg: Any):
    ai = FakeAI(reply)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    result = await wind_down.run_wind_down(
        store,
        make_cfg(**cfg),
        MOMENT.replace(hour=23),
        complete,
        persona      = "p",
        moment_line  = "",
        reasoning_ok = True,
    )
    return result, ai


@pytest.mark.asyncio
async def test_done_threads_become_learnings_and_open_one_deeper_followup_per_day(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.episodes.append(_episode(1))
    store.threads.append(_thread("t1", "reviewing", ["e1"]))
    reply = {
        "diary"  : {"date": "2026-11-05", "text": "今天查了点东西。", "episode_ids": ["e1"]},
        "threads": [
            {"id": "t1", "action": "done", "takeaway": "三脚架稳比贵重要", "episode_ids": ["e1"]}
        ],
        "followup": {
            "parent_id" : "t1",
            "title"     : "赤道仪怎么调平",
            "why"       : "接着想",
            "next_query": "赤道仪 调平",
        },
    }
    result, ai = await _wind(store, reply)
    assert result.done == 1 and result.followup
    [learning] = store.learnings
    assert (
        learning["topic"],
        learning["text"],
        learning["thread_id"],
        learning["episode_ids"],
    ) == ("课题t1", "三脚架稳比贵重要", "t1", ["e1"])
    child = next(t for t in store.threads if t["id"] != "t1")
    assert (child["parent_id"], child["depth"], child["status"]) == ("t1", 1, "open")
    assert store.state["followup_day"] == "2026-11-05"
    assert "followup" in ai.calls[0][1][0]["content"]


@pytest.mark.asyncio
async def test_followups_are_validated_one_per_day_and_at_most_three_levels_deep(
    tmp_path: Path,
) -> None:
    def reply(tid: str, parent: str | None = None, title: str = "后续问题") -> dict[str, Any]:
        out: dict[str, Any] = {
            "diary"  : {"date": "2026-11-05", "text": "今天。", "episode_ids": ["e1"]},
            "threads": [
                {"id": tid, "action": "done", "takeaway": "查明白了", "episode_ids": ["e1"]}
            ],
        }
        if parent:
            out["followup"] = {"parent_id": parent, "title": title, "why": "", "next_query": "q"}
        return out

    store = _store(tmp_path / "a")
    store.episodes.append(_episode(1))
    store.threads.append(_thread("t1", "reviewing", ["e1"], depth=2))
    assert (await _wind(store, reply("t1", "t1")))[0].followup  # 第 3 层
    assert next(t for t in store.threads if t["id"] != "t1")["depth"] == 3
    deep = _store(tmp_path / "b")
    deep.episodes.append(_episode(1))
    deep.threads.append(_thread("t1", "reviewing", ["e1"], depth=3))
    assert (
        not (await _wind(deep, reply("t1", "t1")))[0].followup and len(deep.threads) == 1
    )  # 会是第 4 层
    for case, parent in (("unknown", "zzz"), ("blocked", "t1")):
        s = _store(tmp_path / case)
        s.episodes.append(_episode(1))
        s.threads.append(_thread("t1", "reviewing", ["e1"]))
        title = "含屏蔽词的问题" if case == "blocked" else "后续问题"
        assert not (await _wind(s, reply("t1", parent, title), blocked_keywords=["屏蔽词"]))[
            0
        ].followup
    once = _store(tmp_path / "once")
    once.episodes.append(_episode(1))
    once.state["followup_day"] = "2026-11-05"
    once.threads.append(_thread("t1", "reviewing", ["e1"]))
    assert not (await _wind(once, reply("t1", "t1")))[0].followup
    undone = _store(tmp_path / "undone")  # 没 done 的小课题不能提后续
    undone.episodes.append(_episode(1))
    undone.threads.append(_thread("t1", "reviewing", ["e1"]))
    bad                              = reply("t1", "t1")
    bad["threads"][0]["episode_ids"] = ["e-other"]
    assert not (await _wind(undone, bad))[0].followup and not undone.learnings


def test_learnings_are_kept_180_days_and_capped_at_100() -> None:
    rows = [
        {"id": f"l{n}", "topic": "t", "text": "x", "created_at": NOW - n * DAY}
        for n in range(0, 200)
    ]
    wind_down.prune_learnings(rows, make_cfg(), NOW)
    assert all(NOW - r["created_at"] <= 180 * DAY for r in rows) and len(rows) == 100
    assert rows[-1]["id"] == "l0" and rows[0]["id"] == "l99"
    short = [{"id": "a", "created_at": NOW - 40 * DAY}, {"id": "b", "created_at": NOW}]
    wind_down.prune_learnings(short, make_cfg(learning_keep_days=30), NOW)
    assert [r["id"] for r in short] == ["b"]


def _learn(store: LifeStore) -> None:
    store.learnings += [
        {
            "id"         : "l1",
            "topic"      : "天文摄影",
            "text"       : "三脚架稳比贵重要",
            "thread_id"  : "t1",
            "episode_ids": ["e1"],
            "created_at" : NOW - 2 * DAY,
            "last_used"  : 0.0,
        },
        {
            "id"         : "l2",
            "topic"      : "天文摄影",
            "text"       : "赤道仪要先调平",
            "thread_id"  : "t2",
            "episode_ids": ["e2"],
            "created_at" : NOW - DAY,
            "last_used"  : 0.0,
        },
        {
            "id"         : "l3",
            "topic"      : "独立游戏",
            "text"       : "手感比画面重要",
            "thread_id"  : "t3",
            "episode_ids": ["e3"],
            "created_at" : NOW - 3 * DAY,
            "last_used"  : 0.0,
        },
    ]


def _call(store: LifeStore, text: str, recent: list[str] | None = None):
    payload = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : text,
        "recent"       : recent or [],
        "bot_recent"   : [],
        "unknown_terms": [],
    }
    return tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0))


def test_turn_context_gives_one_learning_on_topic_and_two_plus_a_growth_line_when_asked(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _learn(store)
    response, dirty = _call(store, "我想买个三脚架拍星空，脚架稳不稳很重要吗")
    lines = response["life_lines"]
    assert "你之前自己查明白的（可以用自己的话讲，记录外的细节说大概）：" in lines
    assert "- 三脚架稳比贵重要" in lines and "赤道仪要先调平" not in lines and dirty
    assert store.learnings[0]["last_used"] == NOW
    assert (
        _call(_store(tmp_path / "e"), "我想买个三脚架拍星空")[0] is None
    )  # 没有学到的东西就没有这一块
    asked, _ = _call(store, "最近学到啥了没")
    text = asked["life_lines"]
    assert text.count("\n- ") == 3  # 2 条学到的 + 1 句成长概括
    assert "赤道仪要先调平" in text and "三脚架稳比贵重要" in text and "手感比画面重要" not in text
    assert "这个月在天文摄影上查明白了一两件事，别的方面也零零碎碎查了点" in text
    assert _call(store, "今天吃什么好呢")[0] is None


def test_growth_line_is_deterministic() -> None:
    rows = [{"topic": "天文摄影", "created_at": NOW - DAY}] * 3 + [
        {"topic": "游戏", "created_at": NOW - 40 * DAY}
    ]
    assert tc.growth_line(rows, NOW) == "这个月在天文摄影上查明白了好几件事"
    assert tc.growth_line([{"topic": "游戏", "created_at": NOW - 40 * DAY}], NOW) == ""


def test_followup_threads_get_a_planner_bonus_and_plans_can_mention_the_next_question(
    tmp_path: Path,
) -> None:
    base = _thread("a", "open", [])
    child = _thread("b", "open", [], depth=1, parent="a")
    child["updated_at"] = base["updated_at"]
    cfg = make_cfg(choice_temperature=0.5)
    scores = {
        t["id"]: planner.choose(cfg, default_state(), [t], {}, MOMENT, random.Random(1)).scores[
            "thread_continue"
        ]
        for t in (base, child)
    }
    assert scores["b"] == scores["a"] + 4
    store = _store(tmp_path)
    store.threads.append(child)
    plan = _call(store, "等会干嘛")[0]["life_lines"]
    assert "想接着查：查b（接着上次查明白的事往下问）" in plan
    th.open_thread(
        store.threads,
        make_cfg(max_open_threads=5),
        title      = "第三个",
        why        = "",
        next_query = "",
        now        = NOW,
        parent_id  = "b",
        depth      = 2,
    )
    assert store.threads[-1]["parent_id"] == "b" and store.threads[-1]["depth"] == 2


@pytest.mark.asyncio
async def test_weekly_input_includes_the_weeks_learnings(tmp_path: Path) -> None:
    sunday = at(23, 0, day=8)
    store = _store(tmp_path)
    store.episodes.append({**_episode(5), "at": sunday.timestamp() - 600, "day": "2026-11-08"})
    store.journal.append(
        {
            "kind"       : "diary",
            "at"         : sunday.timestamp() - DAY,
            "day"        : "2026-11-07",
            "text"       : "周六",
            "episode_ids": ["e2"],
        }
    )
    store.learnings += [
        {
            "id"        : "l1",
            "topic"     : "天文",
            "text"      : "本周查明白的",
            "created_at": sunday.timestamp() - 2 * DAY,
        },
        {
            "id"        : "l2",
            "topic"     : "旧",
            "text"      : "上个月的",
            "created_at": sunday.timestamp() - 20 * DAY,
        },
    ]
    ai = FakeAI({"diary": {"date": "2026-11-08", "text": "周日。", "episode_ids": ["e5"]}})

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    await wind_down.run_wind_down(
        store, make_cfg(), sunday, complete, persona="p", moment_line="", reasoning_ok=True
    )
    record = json.loads(ai.calls[0][1][1]["content"])["untrusted_record"]
    assert [row["text"] for row in record["learnings"]] == ["本周查明白的"]


# ───────────────────────────── 注入文字 ─────────────────────────────


def test_injected_text_is_orientation_plus_one_hard_boundary() -> None:
    assert tc._LIFE_HEADER.startswith(
        "你最近上网看到的（你自己的经历，也只是你生活里上网的这一部分）。"
    )
    assert "事实照记录：谁做的、你看到了多少（多半是扫了简介、弹幕和评论区）" in tc._LIFE_HEADER
    assert "感想可以用自己的话重新说，记录外的细节就是记不清：" in tc._LIFE_HEADER
    assert tc._SLANG_HEADER == "你在网上学到的说法（帮你听懂对方；对得上时也可以自己用）："
    assert tc._GUIDE == "这些是依据，不是要讲的清单。打算和做完分开说；兴趣变了要前后都有记录才算。"
    assert (
        tc._PART_NOTE
        == "对方问的是一整天（或这阵子）：上网只是其中一部分，其他的事看人物事实里的生活记录。"
    )
    assert tc._PLAN_HEADER == "接下来的安排（只是打算，还没发生）："
    assert tc._LEARN_HEADER == "你之前自己查明白的（可以用自己的话讲，记录外的细节说大概）："
    everything = "".join(
        [
            tc._LIFE_HEADER,
            tc._SLANG_HEADER,
            tc._GUIDE,
            tc._PART_NOTE,
            tc._PLAN_HEADER,
            tc._LEARN_HEADER,
        ]
    )
    for stacked in (
        "不用报清单",
        "一件最想聊",
        "别说成看完",
        "不用反复声明",
        "讲的是”那句",
        "简短答",
        "一次带出",
    ):
        assert stacked not in everything


def test_status_shows_preferences_unreliable_sources_habits_and_frontier(tmp_path: Path) -> None:
    store = _store(tmp_path)
    reinforce.update_preference(store.state, "activity", "interest_dive", 1.0)
    reinforce.update_preference(store.state, "activity", "hot_scroll", -1.0)
    reinforce.update_preference(store.state, "source", "zhihu_hot", -1.0)
    reinforce.update_preference(store.state, "source", "bili_popular", 1.0)
    for _ in range(2):
        reinforce.record_source(store.state, "weibo_hot", False, NOW)
    for day in (2, 3, 4):
        reinforce.record_habit(store.state, at(19, 0, day=day), 2.0)
        reinforce.record_habit(store.state, at(9, 0, day=day), 0.0)
    store.frontier.append(
        {"topic": "赤道仪", "from_episode": "e1", "added_at": NOW, "tried": False}
    )
    store.learnings.append({"id": "l", "topic": "t", "text": "x", "created_at": NOW})
    store.state["last_decision"] = {
        "choice" : "hot_scroll",
        "reason" : "sampled",
        "scores" : {},
        "at"     : NOW,
        "factors": {"pref": {"hot_scroll": -8.0}},
    }
    text = commands.status_text(store, make_cfg(), Presence("free", "", None), MOMENT)
    assert "最近的偏好" in text and "按兴趣搜 +8.0" in text and "刷热榜 -8.0" in text
    assert "热榜来源偏好" in text and "zhihu_hot ×" in text
    assert "近期不稳的来源" in text and "weibo_hot" in text
    assert "习惯：工作日19点前后刷得最有收获" in text
    assert "好奇边界 1 个待试，自己查明白的事 1 件" in text and "自我强化因素" in text
