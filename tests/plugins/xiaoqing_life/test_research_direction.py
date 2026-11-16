"""研究的取向与门槛：

- 同一个小课题一个“她的一天”只查一次；“材料没有说明……”这类缺口不算进展，连着两次就搁置；
- 原理类问题先去百科 / 维基，做法类才搜 B站视频；查词先用全部关键词，落空再换最具体的那个词；
- 开题要来自喜欢的条目，并写明结束条件和问题类型；
- 研究搜出来的条目不刷兴趣和好奇边界，同一次上网里刷到两条也不算“常刷到”；
- 词条要在多处出现才收，已收的词条继续累计来源；
- “她的一天”五点换日，零点后的睡前整理要带上前一晚的经历。
"""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import (
    activity,
    days,
    digest,
    planner,
    research,
    session,
    wind_down,
)
from plugins.xiaoqing_life import threads as th
from plugins.xiaoqing_life.activity import SessionDeps
from plugins.xiaoqing_life.lifecycle import consolidate, interest_stage
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.planner import THREAD, Decision
from plugins.xiaoqing_life.reinforce import frontier_ok, frontier_weights, grow_frontier
from plugins.xiaoqing_life.session import run_session
from plugins.xiaoqing_life.sources import reference, wiki
from plugins.xiaoqing_life.sources.base import SourceComment, SourceDanmaku, SourceItem
from plugins.xiaoqing_life.stores import default_state
from tests.helpers.xiaoqing_life_test_support import (
    FakeAI,
    FakeFetcher,
    at,
    comments_payload,
    make_cfg,
    new_store,
)

MOMENT = at(14, 0)  # 2026-11-05 周四
NOW    = MOMENT.timestamp()
HOUR   = 3600.0
DAY    = 86400.0


# ───────────────────────────── 夹具 ─────────────────────────────


def _thread(**kw: Any) -> dict[str, Any]:
    return {
        "id"          : "t1",
        "title"       : "锅具预热与不粘",
        "topic"       : "烹饪物理",
        "why"         : "想弄明白锅的厚薄和火力会怎样影响预热",
        "done_when"   : "知道怎么判断锅热好了",
        "kind"        : "practice",
        "next_query"  : "锅具 预热 判断",
        "status"      : "open",
        "steps"       : 0,
        "empty_streak": 0,
        "notes"       : [],
        "takeaway"    : "",
        "created_at"  : NOW - DAY,
        "updated_at"  : NOW - DAY,
        **kw,
    }


def _video(aid: int, title: str, desc: str = "简介", tags: str = "") -> dict[str, Any]:
    return {"id": aid, "bvid": f"BV{aid}", "title": title, "description": desc, "play": 10}


def _search(*videos: dict[str, Any], **extra: Any) -> FakeFetcher:
    return FakeFetcher(
        {
            "/search/type": {"code": 0, "data": {"result": list(videos)}},
            "/reply/main" : comments_payload(),
            **extra,
        }
    )


def _deps(fetch: FakeFetcher, ai: FakeAI, moment: datetime = MOMENT) -> SessionDeps:
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    return SessionDeps(fetch=fetch, complete=complete, rng=random.Random(1), now=lambda: moment)


def _continue() -> Decision:
    return Decision(THREAD, "sampled", {THREAD: 60.0}, {"thread_id": "t1", "query": "锅具 预热"})


def _reply(**kw: Any) -> dict[str, Any]:
    return {"looked_at": [], "status": "progress", "next": {"action": "stop"}, **kw}


def _look(item: str = "i1", topics: tuple[str, ...] = ("锅具",)) -> dict[str, Any]:
    return {"item": item, "reaction": "有意思", "feeling": "like", "topics": list(topics)}


# ───────────────────────────── 一天只查一次 ─────────────────────────────


def test_a_thread_is_researched_at_most_once_per_life_day() -> None:
    """同一个小课题一个“她的一天”只查一次，隔天才接着查。"""

    cfg   = make_cfg()
    state = default_state()
    today = days.day_key(MOMENT)
    assert planner._best_thread(cfg, state, [_thread(last_session_day=today)], MOMENT) is None
    scores = planner.choose(
        cfg, state, [_thread(last_session_day=today)], {}, MOMENT, random.Random(0)
    ).scores
    assert THREAD not in scores
    # 凌晨两点还是同一天；第二天上午才又能查
    late = at(2, 0, day=6)
    assert days.day_key(late) == today
    assert planner._best_thread(cfg, state, [_thread(last_session_day=today)], late) is None
    tomorrow = at(11, 0, day=6)
    assert planner._best_thread(cfg, state, [_thread(last_session_day=today)], tomorrow)


@pytest.mark.asyncio
async def test_research_marks_the_day_it_was_done(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.threads.append(_thread())
    reply = _reply(looked_at=[_look()])
    await run_session(
        store,
        make_cfg(research_max_rounds=1),
        _continue(),
        _deps(_search(_video(7, "锅热了没")), FakeAI(reply)),
    )
    assert store.threads[0]["last_session_day"] == "2026-11-05"


# ───────────────────────────── 真进展 ─────────────────────────────


GAP_NOTES = {
    "材料没有说明锅具厚薄和火力怎样改变预热时间；而且高温滑油会产生油烟": True,
    "材料没有提供不同火力下的升温数据；网友只说小火养铁锅更困难"    : True,
    "这些说法不足以作为精确温控标准"                  : True,
    "材料提到铸铁锅受热更均匀，但没给出预热到位或避免过热的判断信号。" : True,
    "这条视频展示水滴测试有时会翻车，但现有片段没有说明原因"      : True,
    "视频给了不锈钢锅的水滴判断法：水滴能在锅面滚动表示温度够"     : False,
    "厚实的铸铁锅储热多、升温慢；熟铁锅储热少，锅内温度变化更灵活。"  : False,
    "水滴滚动可能是莱顿弗罗斯特效应；不能单凭它确定锅温"        : False,
}


def test_gap_notes_are_not_findings() -> None:
    for text, gap in GAP_NOTES.items():
        assert research.is_gap(text) is gap, text


@pytest.mark.asyncio
async def test_gap_only_sessions_are_not_progress_and_two_in_a_row_park_the_thread(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    store.threads.append(_thread())
    gap = {
        "text" : "材料没有说明锅厚和火力怎样影响预热",
        "quote": "锅热了没",
        "refs" : ["i1"],
    }
    cfg = make_cfg(research_max_rounds=1)
    await run_session(
        store,
        cfg,
        _continue(),
        _deps(_search(_video(7, "锅热了没")), FakeAI(_reply(looked_at=[_look()], findings=[gap]))),
    )
    thread = store.threads[0]
    assert thread["notes"] == [] and thread["steps"] == 0 and thread["status"] == "open"
    assert thread["actions"][-1]["useful"] is False
    # 第二天又是只有“没说明”：搁置，不再被选中
    nextday = at(11, 0, day=6)
    await run_session(
        store,
        cfg,
        _continue(),
        _deps(
            _search(_video(8, "锅热了没第二期")),
            FakeAI(_reply(looked_at=[_look()], findings=[{**gap, "quote": "锅热了没第二期"}])),
            nextday,
        ),
    )
    assert thread["status"] == "parked" and thread["park_reason"] == th.STALL_WHY
    assert planner._best_thread(cfg, default_state(), store.threads, at(12, 0, day=7)) is None


# ───────────────────────────── 去对的地方查 ─────────────────────────────


def test_fact_questions_start_in_the_encyclopedia_and_practice_on_bilibili() -> None:
    cfg, state = make_cfg(), default_state()
    fact = _thread(kind="fact", title="水珠为什么在热锅上跑", next_query="莱顿弗罗斯特效应")
    assert research.first_action(cfg, state, fact, NOW) == research.Next(
        "lookup", "莱顿弗罗斯特效应"
    )
    assert research.first_action(cfg, state, _thread(), NOW).action == "search"  # practice
    # 事实类不会因为研究偏好里一次查词落空就改去搜 B站
    state["preferences"] = {"research_action": {"search": 1.0, "lookup": 0.0}}
    assert research.first_action(cfg, state, fact, NOW).action == "lookup"


def test_a_way_of_looking_that_kept_failing_is_swapped() -> None:
    cfg     = make_cfg()
    failing = [
        {"action": "search", "arg": "锅具 预热", "useful": False, "round": 1, "at": NOW},
        {"action": "search", "arg": "水滴测试", "useful": False, "round": 2, "at": NOW},
    ]
    thread = _thread(actions=failing, next_action={"action": "search", "arg": "水滴测试 锅厚"})
    assert research.stale(thread, "search") and not research.stale(thread, "lookup")
    assert research.first_action(cfg, default_state(), thread, NOW).action == "lookup"


def test_the_research_prompt_carries_the_end_condition_and_past_action_results() -> None:
    thread = _thread(
        actions=[{"action": "search", "arg": "锅具 预热", "useful": False, "round": 1, "at": NOW}]
    )
    messages = research.build_messages(
        persona="你是小青。", thread=thread, items=[], methods=[], round_no=1, rounds_left=2
    )
    user = messages[1]["content"]
    assert "知道怎么判断锅热好了" in user and '"useful": false' in user


@pytest.mark.asyncio
async def test_lookup_searches_wiki_with_all_keywords_and_baike_with_the_first(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    store.threads.append(_thread(kind="fact", next_query="莱顿弗罗斯特效应 水滴"))
    wiki_pages = {
        "query": {
            "pages": {
                "1": {
                    "pageid" : 1,
                    "index"  : 1,
                    "title"  : "莱顿弗罗斯特效应",
                    "extract": "莱顿弗罗斯特效应是指液体在远高于其沸点的表面上形成蒸气层。",
                },
                "2": {"pageid": 2, "index": 2, "title": "某某县", "extract": "某某县位于山区。"},
            }
        }
    }
    fetch   = FakeFetcher({"Baike": {}, "wikipedia": wiki_pages})
    finding = {
        "text" : "水滴是被蒸气层托起来的",
        "quote": "液体在远高于其沸点的表面上形成蒸气层",
        "refs" : ["p1.1"],
    }
    ai = FakeAI(_reply(findings=[finding], status="answered"))
    await run_session(store, make_cfg(research_max_rounds=1), _continue(), _deps(fetch, ai))
    baike_call = next(params for url, params in fetch.calls if "Baike" in url)
    wiki_call  = next(params for url, params in fetch.calls if "wikipedia" in url)
    assert baike_call["bk_key"] == "莱顿弗罗斯特效应"
    assert wiki_call["gsrsearch"] == "莱顿弗罗斯特效应 水滴"
    assert '"title": "某某县"' not in ai.calls[0][1][1]["content"]  # 不相干的页面不读
    assert store.threads[0]["status"] == "reviewing"
    # 一轮就答出来也算一次真收获：小课题的话题记一条兴趣证据
    assert [e["item"] for e in store.interests["烹饪物理"]["evidence"]] == ["thread:t1:2026-11-05"]


def test_wiki_relevance_keeps_matching_pages_only() -> None:
    assert wiki.relevant({"title": "莱顿弗罗斯特效应", "extract": ""}, "莱顿弗罗斯特效应")
    assert wiki.relevant({"title": "炒锅", "extract": "炒锅预热后再放油"}, "锅具 预热")
    assert not wiki.relevant({"title": "某某县", "extract": "某某县位于山区"}, "锅具预热安全")


# ───────────────────────────── 开题门槛 ─────────────────────────────


def _proposal(**kw: Any) -> digest.DigestThread:
    base = {
        "title"     : "猫为什么爱纸箱",
        "why"       : "好奇",
        "next_query": "猫 纸箱",
        "item_id"   : "bili:1",
        "topic"     : "猫",
        "done_when" : "知道猫爱钻纸箱的原因",
        "kind"      : "fact",
    }
    return digest.DigestThread(**{**base, **kw})


def _row(feeling: str) -> dict[str, Any]:
    return {"id": "e1", "item_id": "bili:1", "feeling": feeling, "auto": False}


def test_threads_open_only_from_liked_items_with_an_end_condition() -> None:
    assert session.worth_opening(_proposal(), [_row("like")])
    assert session.worth_opening(_proposal(), [_row("mixed")])
    assert not session.worth_opening(_proposal(), [_row("meh")])
    assert not session.worth_opening(_proposal(done_when=""), [_row("like")])
    assert not session.worth_opening(_proposal(item_id=""), [_row("like")])
    assert not session.worth_opening(_proposal(item_id="bili:2"), [_row("like")])
    assert not session.worth_opening(_proposal(kind=""), [_row("like")])  # 要写问题类型


def test_opened_threads_keep_the_end_condition_and_kind(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    session._open_thread(store, make_cfg(), _proposal(), [_row("like")], NOW)
    thread = store.threads[0]
    assert (thread["done_when"], thread["kind"]) == ("知道猫爱钻纸箱的原因", "fact")
    session._open_thread(
        store, make_cfg(), _proposal(title="锅具预热与不粘", done_when=""), [_row("like")], NOW
    )
    assert len(store.threads) == 1


# ───────────────────────────── 兴趣不被研究刷高 ─────────────────────────────


@pytest.mark.asyncio
async def test_research_results_do_not_become_interests_or_frontier(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.threads.append(_thread())
    reply = _reply(
        looked_at = [_look("i1", ("锅具", "厨房安全")), _look("i2", ("水滴测试",))],
        findings  = [{"text": "水滴会在热锅上跑", "quote": "水滴测试", "refs": ["i2"]}],
    )
    fetch = _search(_video(7, "锅具大比拼"), _video(8, "水滴测试"))
    await run_session(
        store, make_cfg(research_max_rounds=1), _continue(), _deps(fetch, FakeAI(reply))
    )
    assert not {"锅具", "厨房安全", "水滴测试"} & set(store.interests)
    evidence = store.interests["烹饪物理"]["evidence"]
    assert [e["item"] for e in evidence] == ["thread:t1:2026-11-05"]
    assert not {"锅具", "预热", "判断"} & {f["topic"] for f in store.frontier}


def test_two_liked_items_in_one_session_are_not_a_habit_yet() -> None:
    entry = {
        "base"    : 0.0,
        "evidence": [
            {"item": "a", "at": NOW, "delta": 1.0},
            {"item": "b", "at": NOW + 60, "delta": 1.0},
        ],
    }
    interests = {"锅具": entry}
    assert interest_stage(entry, NOW + 120) == "tentative"
    snapshots: list[dict[str, Any]] = []
    assert consolidate(interests, snapshots, NOW + 120) == 0
    entry["evidence"].append({"item": "c", "at": NOW + 2 * DAY, "delta": 1.0})
    assert consolidate(interests, snapshots, NOW + 2 * DAY) == 1
    assert snapshots[0]["text"].startswith("最近几天")  # 只跨了两天，不说“这两周”


def test_campaign_tags_and_search_words_stay_out_of_the_frontier() -> None:
    for junk in (
        "九月打卡挑战",
        "夏日美食大作战",
        "618哔买清单",
        "B站年度知识分享官评选",
        "判断",
        "温度",
    ):
        assert not frontier_ok(junk), junk
    assert frontier_ok("莱顿弗罗斯特") and frontier_ok("特斯拉线圈")
    frontier: list[dict[str, Any]] = []
    row                            = {
        "id"    : "e1",
        "tags"  : ["九月打卡挑战", "不锈钢", "夏日vlog挑战", "灭火", "着火", "火灾", "烟点"],
        "topics": ["莱顿弗罗斯特效应"],
    }
    grow_frontier(frontier, {}, [row], {}, (), NOW)
    assert [f["topic"] for f in frontier] == ["莱顿弗罗斯特效应", "不锈钢", "灭火", "着火"]
    # 旧数据里已经进来的活动标签：读的时候也不给
    old = [{"topic": "九月打卡挑战", "added_at": NOW, "tried": False}]
    assert frontier_weights(old, NOW) == {}


# ───────────────────────────── 词条要“大家都在说” ─────────────────────────────


def _item(comments: tuple[str, ...], danmaku: tuple[tuple[str, int], ...] = ()) -> SourceItem:
    return SourceItem(
        item_id  = "bili:1",
        source   = "bili_popular",
        title    = "铁锅物理不粘",
        text     = "简介",
        url      = "",
        heat     = 1,
        tags     = (),
        comments = tuple(SourceComment(f"c1.{n}", t, 1) for n, t in enumerate(comments, 1)),
        danmaku  = tuple(SourceDanmaku(f"d1.{n}", t, c) for n, (t, c) in enumerate(danmaku, 1)),
    )


def test_term_voices_count_distinct_places_and_danmaku_repeats() -> None:
    once  = [_item(("5090Ti才是真的",))]
    twice = [_item(("绷不住了", "我也绷不住"))]
    dense = [_item((), (("绷不住", 5),))]
    for items, expected in ((once, 1), (twice, 2), (dense, 2)):
        texts = digest.ref_texts(items)
        term  = "5090Ti" if items is once else "绷不住"
        assert digest.voices(term, texts, items) == expected


def test_one_off_new_terms_are_skipped_but_known_ones_keep_collecting_sources(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    single = digest.DigestTerm("5090Ti", "谐音梗", "", "玩笑", "bili:1", voices=1)
    shared = digest.DigestTerm("绷不住", "忍不住笑", "", "玩笑", "bili:1", voices=2)
    activity.learn(store, digest.DigestResult(terms=[single, shared]), [], NOW)
    assert list(store.terms) == ["绷不住"]
    again = digest.DigestTerm("绷不住", "忍不住笑", "", "玩笑", "bili:2", voices=1)
    activity.learn(store, digest.DigestResult(terms=[again]), [], NOW + 1)
    assert store.terms["绷不住"]["sources"] == ["bili:1", "bili:2"]


# ───────────────────────────── 她的一天 ─────────────────────────────


def test_life_day_rolls_over_at_five_in_the_morning() -> None:
    assert days.day_key(at(0, 30, day=6)) == "2026-11-05"
    assert days.day_key(at(4, 59, day=6)) == "2026-11-05"
    assert days.day_key(at(5, 0, day=6)) == "2026-11-06"


def _episode(eid: str, moment: datetime) -> dict[str, Any]:
    return {
        "id"      : eid,
        "at"      : moment.timestamp(),
        "day"     : moment.strftime("%Y-%m-%d"),  # 旧数据按日历记的
        "title"   : f"视频{eid}",
        "reaction": "有意思",
        "feeling" : "like",
        "gist"    : "",
        "topics"  : [],
    }


@pytest.mark.asyncio
async def test_wind_down_after_midnight_covers_the_whole_evening(tmp_path: Path) -> None:
    """零点后的睡前整理按“她的一天”取材：前一晚的经历也进日记。"""

    store = new_store(tmp_path)
    store.episodes += [_episode("e1", at(21, 0)), _episode("e2", at(0, 15, day=6))]
    late = at(0, 30, day=6)
    near = Presence("free", "", 60)
    # 零点后没上网的晚上也有睡前整理
    evening_only = [_episode("e1", at(21, 0))]
    gated        = planner.decide(
        make_cfg(), default_state(), evening_only, [], {}, late, near, random.Random(0)
    )
    assert gated.choice == planner.WIND_DOWN
    reply = {
        "diary": {"date": "2026-11-05", "text": "今晚刷了两个视频。", "episode_ids": ["e1", "e2"]}
    }
    ai = FakeAI(reply)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    result = await wind_down.run_wind_down(
        store, make_cfg(), late, complete, persona="", moment_line="", reasoning_ok=True
    )
    assert result.diary and store.state["done_on"]["wind_down"] == "2026-11-05"
    assert '"e1"' in ai.calls[0][1][1]["content"] and '"e2"' in ai.calls[0][1][1]["content"]
    assert store.journal[-1]["episode_ids"] == ["e1", "e2"]


def test_sunday_night_after_midnight_still_writes_the_weekly(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.journal.append({"kind": "diary", "at": at(23, 0, day=7).timestamp(), "day": "2026-11-07"})
    assert wind_down.weekly_wanted(store, at(0, 40, day=9))  # 周一凌晨：还是周日
    assert not wind_down.weekly_wanted(store, at(23, 0, day=9))


# ───────────────────────────── 查词的关键词处理 ─────────────────────────────


def test_lookup_keywords_are_cut_at_word_boundaries() -> None:
    """查词的关键词按词截断，不会把一个词切成半截。"""

    got = research._next({"action": "lookup", "arg": "不锈钢锅 水滴 预热 莱顿弗罗斯特效应"}, []).arg
    assert got == "不锈钢锅 水滴 预热"
    assert (
        reference.keywords("莱顿弗罗斯特效应和沸腾的关系很长很长", 16)
        == "莱顿弗罗斯特效应和沸腾的关系很长"
    )
    assert reference.key_word("不锈钢锅 水滴 莱顿弗罗斯特效应") == "莱顿弗罗斯特效应"
    assert reference.key_word("锅具 预热 判断") == "锅具"


@pytest.mark.asyncio
async def test_lookup_reads_the_most_specific_word_when_the_full_query_finds_nothing(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    store.threads.append(_thread(kind="fact", next_query="水滴 莱顿弗罗斯特效应"))
    page = {
        "pageid" : 1,
        "index"  : 1,
        "title"  : "莱顿弗罗斯特效应",
        "extract": "液体在远高于其沸点的表面上形成蒸气层，液滴因此悬浮。",
    }

    def wiki_route(params: dict[str, str]) -> dict[str, Any]:
        found = params["gsrsearch"] == "莱顿弗罗斯特效应"
        return {"query": {"pages": {"1": page}}} if found else {"query": {"pages": {}}}

    fetch = FakeFetcher({"Baike": {}, "wikipedia": wiki_route})
    ai = FakeAI(_reply(status="none"))
    await run_session(store, make_cfg(research_max_rounds=1), _continue(), _deps(fetch, ai))
    baike_key = next(p["bk_key"] for u, p in fetch.calls if "Baike" in u)
    searched  = [p["gsrsearch"] for u, p in fetch.calls if "wikipedia" in u]
    assert baike_key == "莱顿弗罗斯特效应"
    assert searched == ["水滴 莱顿弗罗斯特效应", "莱顿弗罗斯特效应"]
    assert "莱顿弗罗斯特效应" in ai.calls[0][1][1]["content"]
