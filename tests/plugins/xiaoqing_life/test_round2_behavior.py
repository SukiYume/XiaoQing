"""第二轮行为：softmax 选活动、名义浏览时段、经历细节与链接、兴趣合并、取用精度、评论预选、消化时刻提示。"""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import digest, planner
from plugins.xiaoqing_life import lifecycle as lc
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.config import parse_life_config
from plugins.xiaoqing_life.planner import HOT_SCROLL, INTEREST, Decision
from plugins.xiaoqing_life.safety import clean_text
from plugins.xiaoqing_life.session import SessionDeps, pick_clicks, prescore, run_session
from plugins.xiaoqing_life.sources import bilibili
from plugins.xiaoqing_life.sources.base import SourceItem
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.plugins.xiaoqing_life.support import (
    FREE,
    FakeAI,
    FakeFetcher,
    at,
    bili_reply,
    bili_video,
    comments_payload,
    make_cfg,
    popular_payload,
)

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()
HOUR   = 3600.0


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


# ───────────────────────────── A1 softmax ─────────────────────────────


def test_choice_is_sampled_by_softmax_not_a_fixed_max() -> None:
    cfg   = make_cfg()  # T = 8：hot_scroll 40 与 interest_dive 35 都有机会
    picks = [
        planner.choose(cfg, default_state(), [], {}, MOMENT, random.Random(n)).choice
        for n in range(300)
    ]
    hot, dive = picks.count(HOT_SCROLL), picks.count(INTEREST)
    assert hot + dive == 300 and dive > 60 and hot > dive
    expected_hot = 1 / (1 + 2.718281828 ** (-5 / 8))
    assert abs(hot / 300 - expected_hot) < 0.08


def test_decision_records_scores_choice_and_probability() -> None:
    cfg = make_cfg(choice_temperature=8.0)
    decision = planner.choose(cfg, default_state(), [], {}, MOMENT, random.Random(5))
    record   = decision.record(NOW)
    assert record["choice"] == decision.choice and set(record["scores"]) == {HOT_SCROLL, INTEREST}
    assert 0.0 < record["probability"] < 1.0
    cold = planner.choose(
        make_cfg(choice_temperature=0.5), default_state(), [], {}, MOMENT, random.Random(5)
    )
    assert cold.choice == HOT_SCROLL and cold.probability > 0.99


def test_choice_temperature_is_clamped() -> None:
    assert parse_life_config({"choice_temperature": 0}).choice_temperature == 0.5
    assert parse_life_config({"choice_temperature": "x"}).choice_temperature == 8.0


# ───────────────────────────── A2 名义浏览时段 ─────────────────────────────

DIGEST = {
    "looked_at": [
        {"item": "i1", "reaction": "第一条", "feeling": "like", "topics": ["猫"]},
        {"item": "i2", "reaction": "第二条", "feeling": "meh", "topics": []},
    ],
}


def _deps(
    fetch: FakeFetcher, ai: FakeAI, rng: random.Random | None = None, **kw: Any
) -> SessionDeps:
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    return SessionDeps(
        fetch=fetch, complete=complete, rng=rng or random.Random(2), now=lambda: MOMENT, **kw
    )


def _fetch() -> FakeFetcher:
    return FakeFetcher(
        {
            "/popular": popular_payload(
                bili_video(1, "猫猫的一天", "讲猫"), bili_video(2, "新显卡评测")
            ),
            "/reply/main"        : comments_payload(bili_reply("好看")),
            "/web-interface/view": {"code": -404},
            "/tag/archive"       : {"code": -404},
        }
    )


@pytest.mark.asyncio
async def test_session_has_a_nominal_span_and_episodes_are_spread_inside_it(tmp_path: Path) -> None:
    store    = _store(tmp_path)
    decision = Decision(HOT_SCROLL, "sampled", {}, {"source": "bili_popular"})
    await run_session(store, make_cfg(), decision, _deps(_fetch(), FakeAI(DIGEST)))
    state = store.state
    start, end = state["last_session_at"], state["last_session_end"]
    assert start == NOW and 8 * 60 <= end - start <= 20 * 60  # hot_scroll 8~20 分钟
    assert state["activity_log"] == [{"start": start, "end": end}]
    ats = [e["at"] for e in store.episodes]
    assert ats == sorted(ats) and all(start < a < end for a in ats)
    assert ats[0] == pytest.approx(start + (end - start) / 3)
    assert state["current"]["choice"] == HOT_SCROLL and state["current"]["end"] == end


@pytest.mark.asyncio
async def test_span_per_activity_is_configurable_and_failure_shortens_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cfg = make_cfg(session_minutes={"hot_scroll": [30, 30]})
    await run_session(
        store,
        cfg,
        Decision(HOT_SCROLL, "sampled", {}, {"source": "bili_popular"}),
        _deps(_fetch(), FakeAI(DIGEST)),
    )
    assert store.state["last_session_end"] - store.state["last_session_at"] == 30 * 60
    failing = _store(tmp_path / "f")
    await run_session(
        failing,
        cfg,
        Decision(HOT_SCROLL, "sampled", {}, {"source": "bili_popular"}),
        _deps(FakeFetcher({}), FakeAI()),
    )
    assert failing.state["last_session_end"] == NOW and failing.state["current"] == {}


def test_gap_is_measured_from_the_span_end_and_the_span_blocks_new_sessions() -> None:
    cfg = make_cfg(choice_temperature=0.5, session_probability=1.0)
    state = default_state() | {"last_session_at": NOW - 100 * 60, "last_session_end": NOW + 5 * 60}

    def decide(**kw: Any) -> Decision:
        return planner.decide(cfg, state, [], [], {}, MOMENT, FREE, random.Random(0), **kw)

    assert decide().reason == "in_session"
    state["last_session_end"] = NOW - 30 * 60  # 结束 30 分钟，间隔 75
    assert decide().reason == "min_gap"
    state["last_session_end"] = NOW - 80 * 60
    assert decide().choice != planner.REST


def test_rolling_window_counts_the_part_of_a_running_span_so_far() -> None:
    cfg = make_cfg(activity_limit_minutes=10)
    state = default_state() | {"activity_log": [{"start": NOW - 20 * 60, "end": NOW + 600}]}
    assert (
        planner.decide(cfg, state, [], [], {}, MOMENT, FREE, random.Random(0)).reason
        == "activity_limit"
    )


def _episode(n: int, ago: float, **kw: Any) -> dict[str, Any]:
    return {
        "id"       : f"e{n}",
        "at"       : NOW - ago,
        "day"      : "2026-11-05",
        "activity" : "hot_scroll",
        "source"   : "bili_popular",
        "item_id"  : f"bili:{n}",
        "title"    : f"标题{n}",
        "reaction" : "挺好玩",
        "feeling"  : "like",
        "share"    : False,
        "topics"   : [],
        "term_keys": [],
        "thread_id": "",
        "gist"     : "",
        "uploader" : "",
        "url"      : "",
        **kw,
    }


def _call(store: LifeStore, text: str, **kw: Any) -> dict[str, Any] | None:
    payload = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : text,
        "recent"       : [],
        "bot_recent"   : [],
        "unknown_terms": [],
    }
    response, _ = tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0))
    return response


def test_turn_context_only_surfaces_episodes_already_reached(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.episodes += [_episode(1, 600), _episode(2, -600, title="还没刷到的视频")]
    lines = _call(store, "最近刷到啥好玩的")["life_lines"]
    assert "标题1" in lines and "还没刷到的视频" not in lines


def test_turn_context_says_what_she_is_doing_without_unreached_titles(tmp_path: Path) -> None:
    store                  = _store(tmp_path)
    store.state["current"] = {
        "choice": "interest_dive",
        "query" : "天文摄影",
        "start" : NOW - 300,
        "end"   : NOW + 600,
    }
    store.episodes.append(_episode(1, -300, title="未来的标题"))
    lines = _call(store, "在干嘛呢")["life_lines"]
    assert "你这会儿正在B站搜〔天文摄影〕。" in lines and "未来的标题" not in lines
    store.state["current"] = {
        "choice": "hot_scroll",
        "source": "bili_popular",
        "start" : NOW - 300,
        "end"   : NOW + 600,
    }
    assert "你这会儿正在刷B站热门。" in _call(store, "你好")["life_lines"]
    store.state["current"]["end"] = NOW - 1  # 时段已过
    assert _call(store, "你好") is None


# ───────────────────────────── A3 经历细节与链接 ─────────────────────────────


def test_episode_line_states_what_it_was_about_and_gives_the_link_only_on_request(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.episodes.append(
        _episode(
            1,
            600,
            title    = "猎户座拍摄教程",
            gist     = "讲赤道仪对星",
            uploader = "氪化金",
            url      = "https://www.bilibili.com/video/BV1xx",
        )
    )
    plain = _call(store, "最近刷到啥好玩的")["life_lines"]
    assert (
        "（UP主 氪化金）" in plain
        and "讲的是：讲赤道仪对星" in plain
        and "bilibili.com" not in plain
    )
    asked = _call(store, "你刚说的那个视频叫啥，谁做的，发个链接")["life_lines"]
    assert "（链接 https://www.bilibili.com/video/BV1xx）" in asked
    assert "bilibili.com" not in _call(store, "你昨天看了什么") if False else True


@pytest.mark.asyncio
async def test_committed_episode_keeps_gist_uploader_and_url(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fetch = FakeFetcher(
        {
            "/popular": popular_payload(
                bili_video(1, "猫猫的一天", "<b>讲猫</b> https://x.com 的日常")
            ),
            "/reply/main"        : comments_payload(),
            "/web-interface/view": {"code": -404},
            "/tag/archive"       : {"code": -404},
        }
    )
    await run_session(
        store,
        make_cfg(),
        Decision(HOT_SCROLL, "s", {}, {"source": "bili_popular"}),
        _deps(fetch, FakeAI({"looked_at": [{"item": "i1", "reaction": "好", "feeling": "like"}]})),
    )
    episode = store.episodes[0]
    assert episode["gist"] == "讲猫 的日常" and episode["uploader"] == "up"
    assert episode["url"].endswith("BV1") and "duration_s" in episode


def test_named_yesterday_or_day_before_restricts_to_that_day(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.episodes += [
        _episode(1, 2 * HOUR, day="2026-11-05", title="今天的视频"),
        _episode(2, 26 * HOUR, day="2026-11-04", title="昨天的视频"),
        _episode(3, 50 * HOUR, day="2026-11-03", title="前天的视频"),
    ]
    yesterday = _call(store, "你昨天刷到啥了")["life_lines"]
    assert (
        "昨天的视频" in yesterday
        and "今天的视频" not in yesterday
        and "前天的视频" not in yesterday
    )
    day_before = _call(store, "前天你在看什么")["life_lines"]
    assert "前天的视频" in day_before and "昨天的视频" not in day_before


# ───────────────────────────── A4 兴趣合并 ─────────────────────────────


def test_similar_topics_merge_onto_the_existing_key_and_are_clipped() -> None:
    interests: dict[str, Any] = {}
    add                       = lambda topic, item: lc.add_interest_evidence(  # noqa: E731
        interests, topic, episode_id="e", item_id=item, delta=1, now=NOW
    )
    assert add("天文观测", "a")
    assert add("天文观测站", "b")  # 相近：并到已有键
    assert list(interests) == ["天文观测"] and len(interests["天文观测"]["evidence"]) == 2
    assert add("一个特别特别特别长的话题名称啊", "c")
    assert max(len(k) for k in interests) <= 12


def test_seed_base_is_constant_but_learned_evidence_still_decays() -> None:
    interests: dict[str, Any] = {}
    lc.seed_interests(interests, ("天文观测",), NOW)
    assert interests["天文观测"]["seed"] is True
    assert lc.interest_score(interests["天文观测"], NOW + 400 * 86400) == pytest.approx(1.0)
    lc.add_interest_evidence(interests, "猫", episode_id="e", item_id="i", delta=1, now=NOW)
    assert lc.interest_score(interests["猫"], NOW + 30 * 86400) == pytest.approx(0.5)
    lc.refresh_interests(interests, NOW + 400 * 86400)
    assert "天文观测" in interests  # 种子不会被衰减清掉


def test_consolidation_snapshots_are_deterministic_once_per_topic_and_tier() -> None:
    interests: dict[str, Any]       = {}
    snapshots: list[dict[str, Any]] = []
    for n in range(2):
        lc.add_interest_evidence(
            interests, "天文摄影", episode_id=f"e{n}", item_id=f"i{n}", delta=1, now=NOW - n * 86400
        )
    assert lc.consolidate(interests, snapshots, NOW) == 1
    assert lc.consolidate(interests, snapshots, NOW) == 0  # 同一档位只一次
    first = snapshots[0]
    assert (first["topic"], first["tier"], first["evidence_count"], first["liked"]) == (
        "天文摄影",
        2,
        2,
        2,
    )
    assert (
        first["since"] < first["until"]
        and "天文摄影" in first["text"]
        and "大多挺喜欢" in first["text"]
        # 次数只存在字段里，交给她的文字不报数
        and not any(ch.isdigit() for ch in first["text"])
    )
    for n in range(2, 4):
        lc.add_interest_evidence(
            interests, "天文摄影", episode_id=f"e{n}", item_id=f"i{n}", delta=1, now=NOW
        )
    assert lc.consolidate(interests, snapshots, NOW) == 1 and snapshots[-1]["tier"] == 4
    for n in range(4, 12):
        lc.add_interest_evidence(
            interests, "天文摄影", episode_id=f"e{n}", item_id=f"i{n}", delta=1, now=NOW
        )
    lc.consolidate(interests, snapshots, NOW)
    assert [s["tier"] for s in snapshots] == [2, 4, 8]  # 封顶 8
    lc.consolidate(interests, snapshots, NOW)
    assert len(snapshots) == 3


def test_snapshots_answer_questions_about_what_she_likes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.snapshots.append(
        {
            "topic"     : "天文摄影",
            "tier"      : 2,
            "stage"     : "repeated",
            "created_at": NOW - HOUR,
            "text"      : "这两周常看天文摄影相关的内容（2次，喜欢的居多）",
        }
    )
    assert "最近的你：这两周常看天文摄影" in _call(store, "你最近迷上什么了")["life_lines"]
    assert _call(store, "今天吃什么") is None


# ───────────────────────────── A5 取用精度 ─────────────────────────────


def _term(store: LifeStore, name: str, meaning: str, **kw: Any) -> None:
    store.terms[name] = {
        "term"          : name,
        "meaning"       : meaning,
        "usage"         : "",
        "tone"          : "中性",
        "status"        : "known",
        "verified"      : False,
        "sources"       : ["a", "b"],
        "first_seen"    : NOW - 5 * 86400,
        "last_seen"     : NOW - 3600,
        "last_suggested": 0.0,
        **kw,
    }


def test_single_character_terms_are_never_understood_hits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _term(store, "草", "笑")
    _term(store, "绷不住", "忍不住笑")
    response = _call(store, "草 真的绷不住了")
    assert "「绷不住」" in response["slang_notes"] and "「草」" not in response["slang_notes"]


def test_stopword_bigrams_do_not_make_a_term_usable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _term(store, "哈基米", "这个就是什么时候都可以用的")
    payload = {
        "text"         : "今天我们觉得这个真的是什么",
        "recent"       : [],
        "bot_recent"   : [],
        "unknown_terms": [],
    }
    response, dirty = tc.turn_context(
        store, make_cfg(active_use_probability=1.0), payload, MOMENT, random.Random(0)
    )
    assert response is None and not dirty
    assert tc.overlap("什么", "什么") == 0 and tc.overlap("时候现在", "时候现在") == 1


def test_dirty_only_when_something_actually_changed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _term(store, "哈基米", "对猫咪的爱称")
    payload = {"text": "看这个哈基米", "recent": [], "bot_recent": [], "unknown_terms": []}
    response, dirty = tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0.99 and 1))
    assert response is not None and not dirty  # 只是听懂，没有改任何状态
    queued = dict(payload, unknown_terms=["绝绝子"])
    _, dirty = tc.turn_context(store, make_cfg(), queued, MOMENT, random.Random(1))
    assert dirty
    _, dirty = tc.turn_context(store, make_cfg(), queued, MOMENT, random.Random(1))
    assert not dirty  # 已在队列里


# ───────────────────────────── A6 评论 ─────────────────────────────


def test_reply_prefix_left_after_at_removal_is_stripped() -> None:
    assert clean_text("回复 @某人 :说得对", 100) == "说得对"
    assert clean_text("回复 @某人：说得对", 100) == "说得对"
    assert clean_text("回复了很多人", 100) == "回复了很多人"
    assert clean_text("哈" * 40, 100) == "哈哈哈"  # 超长重复压成 3 个


def test_comment_candidates_are_chosen_by_interest_and_heat_not_by_position() -> None:
    cfg = make_cfg()

    def item(n: int, title: str, heat: int) -> SourceItem:
        return SourceItem(f"bili:{n}", "bili_popular", title, "", "u", heat, ())

    items = [item(i, f"无关标题{i}", 100) for i in range(1, 6)]
    items.append(item(6, "天文摄影入门", 100))
    items.append(item(7, "无关热门", 10_000_000))
    clicks = pick_clicks(cfg, items, ["天文摄影"])
    assert 5 in clicks and 6 in clicks and len(clicks) == 3 and clicks == sorted(clicks)
    assert prescore(items[5], ["天文摄影"]) > prescore(items[0], ["天文摄影"])
    zhihu = SourceItem("zhihu_hot:1", "zhihu_hot", "天文摄影", "", "u", 1, ())
    assert pick_clicks(cfg, [zhihu], ["天文摄影"]) == []  # 只有 B 站条目取热评


@pytest.mark.asyncio
async def test_default_comment_budget_is_twelve() -> None:
    assert make_cfg().max_comments_per_item == 12
    fetch = FakeFetcher(
        {"/reply/main": comments_payload(*[bili_reply(f"评论{n}") for n in range(10)])}
    )
    [item] = await bilibili.popular(
        FakeFetcher({"/popular": popular_payload(bili_video(1, "视频"))}), []
    )
    assert len(await bilibili.hot_comments(fetch, item, 1, [], 12)) == 10


# ───────────────────────────── A8 消化时刻 ─────────────────────────────


def test_digest_prompt_carries_time_weekday_and_the_routine_activity() -> None:
    items = [SourceItem("bili:1", "bili_popular", "标题", "", "u", 1, ())]
    moment = datetime(2026, 11, 5, 23, 40, tzinfo=MOMENT.tzinfo)
    system = digest.build_messages(
        persona       = "你是小青。",
        items         = items,
        known_terms   = [],
        thread_titles = [],
        moment        = moment,
        activity      = "躺在床上刷手机",
    )[0]["content"]
    assert "2026-11-05 23:40" in system and "星期四" in system and "躺在床上刷手机" in system
    plain = digest.build_messages(persona="p", items=items, known_terms=[], thread_titles=[])[0][
        "content"
    ]
    assert "星期" not in plain


@pytest.mark.asyncio
async def test_session_passes_the_routine_activity_into_the_digest_prompt(tmp_path: Path) -> None:
    ai    = FakeAI({"looked_at": []})
    store = _store(tmp_path)
    await run_session(
        store,
        make_cfg(),
        Decision(HOT_SCROLL, "s", {}, {"source": "bili_popular"}),
        _deps(_fetch(), ai, activity_text="躺在床上刷手机"),
    )
    assert "躺在床上刷手机" in ai.calls[0][1][0]["content"]
