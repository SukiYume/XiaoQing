"""查词与梗图、节律与意图、主动分享：词条自动查实队列与暴力词、弹幕密集点、梗图预选、探针、节律亲和、打算意图、分享候选。"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import digest, memes, planner
from plugins.xiaoqing_life import main as life_main
from plugins.xiaoqing_life import persona_bridge as pb
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.activity import SessionDeps
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.safety import is_violent_term
from plugins.xiaoqing_life.session import run_session
from plugins.xiaoqing_life.sources import watch
from plugins.xiaoqing_life.sources.base import SourceComment, SourceItem
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.helpers.xiaoqing_life_test_support import (
    FREE,
    FakeAI,
    FakeFetcher,
    at,
    bili_reply,
    bili_video,
    comments_payload,
    fixed_clock,
    make_cfg,
    make_context,
    new_store,
    popular_payload,
    write_persona_card,
)

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()
HOUR   = 3600.0


# ───────────────────────────── A1 词条 ─────────────────────────────


def test_violent_terms_are_checked_against_the_term_text_only() -> None:
    assert all(is_violent_term(w) for w in ("灭门", "屠杀", "砍死", "弄死", "去死", "自残"))
    assert not is_violent_term("绷不住")
    item = SourceItem(
        "bili:1",
        "bili_popular",
        "标题",
        "",
        "u",
        1,
        (),
        (SourceComment("c1.1", "灭门了灭门了，太好笑", 1), SourceComment("c1.2", "绷不住了", 1)),
    )
    raw = {
        "terms": [
            {"term": "灭门", "meaning": "整套没了", "quote": "灭门了", "refs": ["c1.1"]},
            {"term": "绷不住", "meaning": "忍不住笑", "quote": "绷不住了", "refs": ["c1.2"]},
        ]
    }
    result = digest.validate(raw, [item], max_memes=0)
    assert [t.term for t in result.terms] == ["绷不住"]  # 评论本身没被整条丢弃


@pytest.mark.asyncio
async def test_new_short_candidate_terms_are_queued_for_baike_once_and_bounded(
    tmp_path: Path,
) -> None:
    store             = new_store(tmp_path)
    store.terms["旧词"] = {
        "term"          : "旧词",
        "meaning"       : "m",
        "usage"         : "",
        "tone"          : "中性",
        "status"        : "known",
        "verified"      : True,
        "sources"       : ["a", "b"],
        "first_seen"    : NOW,
        "last_seen"     : NOW,
        "last_suggested": 0.0,
    }
    comments = tuple(
        SourceComment(f"c1.{n}", t, 1)
        for n, t in enumerate(
            ["绷不住了", "旧词又来了", "这是一个超过八个字的长词条哦哦哦", "哈基米"], 1
        )
    )
    fetch = FakeFetcher(
        {
            "/popular": popular_payload(bili_video(1, "标题")),
            "/reply/main": comments_payload(*[bili_reply(c.text, sub=(c.text,)) for c in comments]),
            "/web-interface/view": {"code": -404},
            "/tag/archive": {"code": -404},
        }
    )
    reply = {
        "looked_at": [{"item": "i1", "reaction": "好", "feeling": "like"}],
        "terms"    : [
            {"term": "绷不住", "meaning": "忍不住笑", "quote": "绷不住了", "refs": ["c1.1"]},
            {"term": "旧词", "meaning": "m", "quote": "旧词又来了", "refs": ["c1.2"]},
            {
                "term"   : "这是一个超过八个字的长词条",
                "meaning": "m",
                "quote"  : "这是一个超过八个字的长词条哦哦哦",
                "refs"   : ["c1.3"],
            },
            {"term": "哈基米", "meaning": "爱称", "quote": "哈基米", "refs": ["c1.4"]},
        ],
    }
    store.state["lookup_queue"] = ["哈基米"]

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await FakeAI(reply).complete(route, messages)).content

    deps = SessionDeps(fetch=fetch, complete=complete, rng=random.Random(1), now=lambda: MOMENT)
    await run_session(
        store, make_cfg(), planner.Decision("hot_scroll", "s", {}, {"source": "bili_popular"}), deps
    )
    assert store.state["lookup_queue"] == [
        "哈基米",
        "绷不住",
    ]  # 旧词已知、长词条不进队，哈基米不重复
    assert "旧词" not in store.state["lookup_queue"]


# ───────────────────────────── A2 弹幕密集点 ─────────────────────────────


def test_dense_moment_ignores_opening_and_closing_noise_and_never_says_zero() -> None:
    rows = (
        [(5.0, "来了")] * 40 + [(10.0, "前排")] * 30 + [(95.0, "高能")] * 5 + [(610.0, "终于")] * 20
    )
    _, dense = watch.process_danmaku(rows, 1, [])
    assert dense == "1:30 前后弹幕最密"
    only_opening = watch.process_danmaku([(3.0, "来了")] * 50, 1, [])[1]
    assert only_opening == "" and "0:00" not in only_opening
    closing_only = watch.process_danmaku([(5.0, "来了")] * 9 + [(100.0, "好")] * 6, 1, [])[1]
    assert closing_only == "1:30 前后弹幕最密"
    assert "0:00" not in watch._dense_moment(Counter({0: 99, 1: 3, 2: 4}))


# ───────────────────────────── A3 梗图预选 ─────────────────────────────


def _item(*comments: SourceComment) -> SourceItem:
    return SourceItem("bili:1", "bili_popular", "视频", "", "u", 1, (), tuple(comments))


def test_preselect_needs_median_likes_short_text_and_a_picture() -> None:
    item = _item(
        SourceComment("c1.1", "笑死", 50, ("https://i0.hdslb.com/a.jpg",)),
        SourceComment("c1.2", "笑死", 40, ("https://i0.hdslb.com/b.gif",)),
        SourceComment(
            "c1.3",
            "这是一段超过二十个字的很长很长的评论内容文字哦哦哦",
            90,
            ("https://i0.hdslb.com/c.png",),
        ),
        SourceComment("c1.4", "", 5, ("https://i0.hdslb.com/d.png",)),  # 低于中位数
        SourceComment("c1.5", "纯图", 60, ("https://i0.hdslb.com/e.webp?x=1",)),
        SourceComment("c1.6", "无图", 70),
    )
    picks = [ref for ref, _ in memes.preselect([item])]
    # 中位数 55：c1.1 / c1.2 / c1.4 低于中位数，c1.3 评论太长，c1.6 没有配图。
    assert picks == ["c1.5#0"]
    assert memes.preselect([_item()]) == []


def test_picture_only_comment_counts_and_formats_sort_before_likes() -> None:
    item = _item(
        SourceComment("c1.1", "", 10, ("https://i0.hdslb.com/a.jpg",)),
        SourceComment("c1.2", "", 8, ("https://i0.hdslb.com/b.png",)),
        SourceComment("c1.3", "好", 5),
    )
    assert [r for r, _ in memes.preselect([item])] == ["c1.2#0", "c1.1#0"]


def test_merge_puts_digest_picks_first_dedupes_and_caps() -> None:
    merged = memes.merge_picks(
        [("c1.1#0", "模型挑的")], [("c1.1#0", "预选"), ("c1.2#0", "预选"), ("c1.3#0", "预选")], 2
    )
    assert merged == [("c1.1#0", "模型挑的"), ("c1.2#0", "预选")]
    assert memes.merge_picks([("a", "x")], [("b", "y")], 0) == []


@pytest.mark.asyncio
async def test_session_sends_preselected_pictures_to_the_vision_judge_up_to_the_cap(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    fetch = FakeFetcher(
        {
            "/popular"   : popular_payload(bili_video(1, "视频")),
            "/reply/main": comments_payload(
                *[bili_reply("笑死", 10 + n, (f"https://i0.hdslb.com/{n}.png",)) for n in range(4)]
            ),
            "/web-interface/view": {"code": -404},
            "/tag/archive"       : {"code": -404},
        }
    )
    seen: list[str] = []

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return '{"looked_at": [{"item": "i1", "reaction": "好", "feeling": "like"}]}'

    async def download(url: str, limit: int) -> tuple[bytes, str] | None:
        seen.append(url)
        return url.encode() + b"-bytes", ".png"

    verdicts: list[str] = []

    async def vision(messages: list[dict[str, Any]]) -> str:
        verdicts.append("x")
        return '{"is_reaction_meme": false, "safe_for_group": true}'

    deps = SessionDeps(
        fetch    = fetch,
        complete = complete,
        rng      = random.Random(1),
        now      = lambda: MOMENT,
        download = download,
        vision   = vision,
    )
    await run_session(
        store,
        make_cfg(max_vision_per_session=2),
        planner.Decision("hot_scroll", "s", {}, {"source": "bili_popular"}),
        deps,
    )
    assert len(seen) == 2 and len(verdicts) == 2  # 消化一张都没挑，预选补足到上限


# ───────────────────────────── B1 探针 ─────────────────────────────


@pytest.mark.asyncio
async def test_probe_mode_reports_online_life_only_when_enabled(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    response, dirty = tc.turn_context(
        store, make_cfg(), {"version": 1, "mode": "probe"}, MOMENT, random.Random(0)
    )
    assert response == {"version": 1, "online_life": True} and not dirty
    context       = make_context(tmp_path / "on")
    runtime       = life_main.get_runtime(context)
    runtime.clock = fixed_clock(MOMENT)
    assert await life_main.turn_context({"version": 1, "mode": "probe"}, context) == {
        "version"    : 1,
        "online_life": True,
    }
    off = make_context(tmp_path / "off", life={"enabled": False})
    assert await life_main.turn_context({"version": 1, "mode": "probe"}, off) is None


# ───────────────────────────── B2 节律 ─────────────────────────────


def _choose(cfg, presence, state=None, **kw: Any) -> planner.Decision:
    return planner.choose(
        make_cfg(choice_temperature=0.5, **cfg),
        state or default_state(),
        kw.pop("threads", []),
        {},
        MOMENT,
        kw.pop("rng", random.Random(1)),
        presence,
    )


def _thread() -> dict[str, Any]:
    return {
        "id"        : "t1",
        "title"     : "课题",
        "next_query": "q",
        "status"    : "open",
        "steps"     : 0,
        "updated_at": NOW,
    }


def test_thread_gets_a_morning_bonus_within_two_hours_of_waking() -> None:
    base = _choose({}, Presence("free", "", None, None), threads=[_thread()]).scores[
        "thread_continue"
    ]
    morning = _choose({}, Presence("free", "", None, 90), threads=[_thread()]).scores[
        "thread_continue"
    ]
    late = _choose({}, Presence("free", "", None, 150), threads=[_thread()]).scores[
        "thread_continue"
    ]
    assert morning == base + 6 and late == base


def test_wake_time_comes_from_the_sleep_slot_in_the_routine_and_in_quiet_hours(
    tmp_path: Path,
) -> None:
    context, cfg = make_context(tmp_path), make_cfg()
    write_persona_card(context, tmp_path, ("每天 00:30-08:00 睡觉（睡）",))
    assert pb.availability(context, cfg, at(9, 0)).minutes_since_wake in range(60, 66)
    assert pb.availability(context, cfg, at(10, 5)).minutes_since_wake is None  # 超过 2 小时
    assert pb.availability(context, cfg, at(7, 0)).availability == "asleep"
    write_persona_card(context, tmp_path, None)
    assert pb.availability(
        context, make_cfg(quiet_hours="00:30-08:00"), at(8, 30)
    ).minutes_since_wake in range(30, 36)


def test_activity_affinity_anime_meal_and_no_browse() -> None:
    anime = _choose({}, Presence("free", "刷番", None))
    assert anime.scores["interest_dive"] == 45.0
    topics = {
        _choose({}, Presence("free", "刷番", None), rng=random.Random(n)).params.get("query")
        for n in range(40)
    }
    assert {"新番", "动画推荐", "番剧", "动漫混剪", "轻小说"} & topics
    meal = _choose({"max_items_per_session": 6}, Presence("free", "吃午饭", None))
    assert (
        meal.scores["hot_scroll"] == 50.0
        and meal.choice == "hot_scroll"
        and meal.params["item_cap"] == 3
    )
    assert "item_cap" not in _choose({}, FREE).params
    custom = _choose({"activity_affinity": {"meal": ["夜宵"]}}, Presence("free", "吃夜宵", None))
    assert custom.scores["hot_scroll"] == 50.0
    for activity in ("社团活动", "上课", "做实验", "期末考试"):
        out = planner.decide(
            make_cfg(),
            default_state(),
            [],
            [],
            {},
            MOMENT,
            Presence("free", activity, None),
            random.Random(0),
        )
        assert (out.choice, out.reason) == ("rest", "activity_busy")
    assert (
        planner.decide(
            make_cfg(session_probability=1.0),
            default_state(),
            [],
            [],
            {},
            MOMENT,
            Presence("free", "吃饭", None),
            random.Random(0),
        ).choice
        != "rest"
    )


@pytest.mark.asyncio
async def test_meal_time_caps_the_items_digested(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    fetch = FakeFetcher(
        {
            "/popular"           : popular_payload(*[bili_video(n, f"视频{n}") for n in range(1, 9)]),
            "/reply/main"        : comments_payload(),
            "/web-interface/view": {"code": -404},
            "/tag/archive"       : {"code": -404},
        }
    )
    ai = FakeAI({"looked_at": []})

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    deps = SessionDeps(fetch=fetch, complete=complete, rng=random.Random(1), now=lambda: MOMENT)
    decision = planner.Decision("hot_scroll", "s", {}, {"source": "bili_popular", "item_cap": 3})
    await run_session(store, make_cfg(), decision, deps)
    import json

    assert len(json.loads(ai.calls[0][1][1]["content"])["untrusted_material"]["items"]) == 3


# ───────────────────────────── B3 分享候选 ─────────────────────────────


def _episode(n: int, ago: float, **kw: Any) -> dict[str, Any]:
    return {
        "id"       : f"e{n}",
        "at"       : NOW - ago,
        "day"      : "2026-11-05",
        "activity" : "hot_scroll",
        "source"   : "bili_popular",
        "item_id"  : f"bili:{n}",
        "title"    : f"视频{n}",
        "reaction" : "挺好玩",
        "feeling"  : "like",
        "share"    : True,
        "topics"   : ["猫"],
        "term_keys": [],
        "thread_id": "",
        "gist"     : "讲猫",
        "uploader" : "up主",
        "url"      : f"https://www.bilibili.com/video/BV{n}",
        **kw,
    }


def _proactive(store: LifeStore, **extra: Any) -> dict[str, Any] | None:
    payload = {"version": 1, "mode": "proactive", **extra}
    return tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0))[0]


def test_proactive_mode_offers_recent_tellable_episodes_minus_excluded(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.episodes += [
        _episode(1, 2 * HOUR),
        _episode(2, 5 * HOUR),
        _episode(3, 30 * HOUR),
        _episode(4, -HOUR),
        _episode(5, HOUR, share=False),
        _episode(6, 50 * HOUR),
        _episode(7, HOUR, share=False, feeling="meh"),
        _episode(8, HOUR, share=False, auto=True),
    ]
    response = _proactive(store)
    ids      = [c["episode_id"] for c in response["share_candidates"]]
    # 两天内、已刷到；她标了想分享的在前，喜欢的跟在后面；没感觉的和自动补的不算
    assert ids == ["e1", "e2", "e3", "e5"]
    first = response["share_candidates"][0]
    assert (
        "「视频1」" in first["line"]
        and "（UP主 up主）" in first["line"]
        and "讲的是：讲猫" in first["line"]
    )
    assert "（链接 https://www.bilibili.com/video/BV1）" in first["line"] and first["topics"] == [
        "猫"
    ]
    assert not first["line"].startswith("- ") and response["life_lines"] == ""
    assert first["heard_here"] is False
    assert [c["episode_id"] for c in _proactive(store, exclude_ids=["e1"])["share_candidates"]] == [
        "e2",
        "e3",
        "e5",
    ]
    assert _proactive(store, exclude_ids=["e1", "e2", "e3", "e5", "zz"]) is None
    assert _proactive(new_store(tmp_path / "empty")) is None


# ───────────────────────────── B4 意图 ─────────────────────────────


def _call(store: LifeStore, text: str, slots: list[str] | None = None) -> dict[str, Any] | None:
    payload = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : text,
        "recent"       : [],
        "bot_recent"   : [],
        "unknown_terms": [],
    }
    return tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0), slots)[0]


def test_plan_questions_get_the_rest_of_todays_routine_and_the_threads_next_step(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    store.threads.append(
        {
            "id"        : "t1",
            "title"     : "猎户座怎么拍",
            "why"       : "",
            "next_query": "猎户座 拍摄 曝光",
            "status"    : "open",
            "steps"     : 1,
            "notes"     : [],
            "takeaway"  : "",
        }
    )
    for text in ("等会干嘛", "今晚有什么安排", "明天打算干啥", "你下午有啥打算"):
        lines = _call(store, text, ["20:00 社团活动", "22:30 打游戏"])["life_lines"]
        assert "接下来的安排（只是打算，还没发生）：" in lines
        assert "- 20:00 社团活动" in lines and "- 想接着查：猎户座 拍摄 曝光（还没查完）" in lines
        assert "这些是依据，不是要讲的清单" in lines
    assert _call(store, "今天吃什么") is None  # 没问打算就不给
    four = _call(store, "等会干嘛", ["a", "b", "c", "d"])["life_lines"]
    assert four.count("\n- ") == 4  # 3 个时段 + 小课题


def test_upcoming_plans_come_from_todays_remaining_slots(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    write_persona_card(
        context,
        tmp_path,
        (
            "每天 00:30-08:00 睡觉（睡）",
            "每天 15:00-17:00 社团活动（闲）",
            "每天 19:00-21:00 自习（忙）",
            "工作日 21:00-22:00 刷番（闲）",
            "周末 10:00-11:00 补觉（闲）",
            "每天 23:00-23:50 洗漱（闲）",
        ),
    )
    plans = pb.upcoming_plans(context, MOMENT)  # 周四 14:00
    assert plans == ["15:00 社团活动", "19:00 自习", "21:00 刷番"]
    assert pb.upcoming_plans(context, at(22, 0)) == ["23:00 洗漱"]
    write_persona_card(context, tmp_path, None)
    assert pb.upcoming_plans(context, MOMENT) == []


@pytest.mark.asyncio
async def test_service_adds_the_routine_slots_only_for_plan_questions(tmp_path: Path) -> None:
    context       = make_context(tmp_path)
    runtime       = life_main.get_runtime(context)
    runtime.clock = fixed_clock(MOMENT)
    write_persona_card(context, tmp_path, ("每天 20:00-22:00 社团活动（闲）",))
    response = await life_main.turn_context(
        {"version": 1, "mode": "reply", "text": "等会干嘛"}, context
    )
    assert "- 20:00 社团活动" in response["life_lines"]
    other = await life_main.turn_context({"version": 1, "mode": "reply", "text": "你好"}, context)
    assert other is None or "社团活动" not in str(other)


def test_recent_week_like_and_link_intents_keep_the_guidance_line(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.episodes.append(_episode(1, HOUR))
    store.state["diary"] = {"day": "2026-11-05", "text": "今天刷了猫视频", "episode_ids": ["e1"]}
    store.journal.append(
        {
            "kind"       : "weekly",
            "at"         : NOW - HOUR,
            "week"       : "2026-W45",
            "text"       : "这周看了不少猫",
            "dates"      : ["2026-11-04"],
            "episode_ids": ["e1"],
        }
    )
    store.snapshots.append(
        {
            "topic"     : "猫",
            "tier"      : 2,
            "stage"     : "repeated",
            "created_at": NOW - HOUR,
            "text"      : "这两周常看猫相关的内容（2次，喜欢的居多）",
        }
    )
    today = _call(store, "你今天都干嘛了")["life_lines"]
    assert "今天大概：今天刷了猫视频" in today and "「视频1」" in today
    week = _call(store, "这周过得怎么样")["life_lines"]
    assert "这周大概：这周看了不少猫" in week and "最近的你：这两周常看猫" in week
    like = _call(store, "你最近迷上啥了")["life_lines"]
    assert "最近的你：这两周常看猫" in like
    link = _call(store, "刚说的视频叫啥，链接发我")["life_lines"]
    assert "（UP主 up主）" in link and "（链接 https://www.bilibili.com/video/BV1）" in link
    for lines in (today, week, like, link):
        assert "打算和做完分开说；兴趣变了要前后都有记录才算" in lines
    assert "bilibili.com" not in today


def test_follow_up_about_the_video_she_just_mentioned_brings_its_gist_back(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.episodes += [
        _episode(
            1,
            2 * HOUR,
            title  = "猫咪第一次见到雪的反应",
            gist   = "猫咪在阳台第一次看到下雪",
            share  = False,
            topics = [],
        ),
        _episode(2, 3 * HOUR, title="新手三脚架怎么选", gist="介绍三脚架", share=False, topics=[]),
    ]
    payload = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : "那个猫的视频讲啥的",
        "recent"       : [],
        "bot_recent"   : ["刚刷到个猫咪第一次见雪的视频，歪头盯着雪花"],
        "unknown_terms": [],
    }
    response, _ = tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0))
    lines = response["life_lines"]
    assert "讲的是：猫咪在阳台第一次看到下雪" in lines and "新手三脚架" not in lines
    quiet = dict(payload, bot_recent=["今天食堂不错"])
    assert tc.turn_context(store, make_cfg(), quiet, MOMENT, random.Random(0))[0] is None


def test_share_line_tells_the_quote_and_keeps_only_her_stance(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.episodes += [
        _episode(
            1,
            HOUR,
            share          = False,
            reaction       = "评论区太会接了",
            highlight      = "建议改名叫电子榨菜大全",
            highlight_from = "comment",
        ),
        _episode(
            2,
            2 * HOUR,
            feeling        = "mixed",
            aspects        = {"like": "实验细节", "dislike": "标题党"},
            highlight      = "270度才不粘",
            highlight_from = "passage",
        ),
    ]
    first, second = _proactive(store)["share_candidates"]
    # 记下了原文的排在只有感想的前面；有原文时只带态度，当时那句感想不再给
    assert [first["episode_id"], second["episode_id"]] == ["e2", "e1"]
    assert "里面有一句：「270度才不粘」" in first["line"]
    assert "你喜欢实验细节，不太喜欢标题党" in first["line"]
    assert "评论区有一句：「建议改名叫电子榨菜大全」，你挺喜欢" in second["line"]
    assert "评论区太会接了" not in second["line"] and "你当时觉得" not in second["line"]
    assert "（链接 https://www.bilibili.com/video/BV1）" in second["line"]


def test_topic_researched_for_this_chat_is_marked_and_ranked_first(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.episodes += [
        _episode(1, HOUR),
        _episode(2, 20 * HOUR, share=False, feeling="meh", chat_topic="新番推荐"),
    ]
    here = _proactive(
        store, is_private=False, topics=[{"topic": "新番推荐", "keywords": ["十月"]}]
    )["share_candidates"]
    assert [(c["episode_id"], c["heard_here"]) for c in here] == [("e2", True), ("e1", False)]
    assert "（听群里在聊「新番推荐」才去搜的）" in here[0]["line"]
    # 别的会话聊出来的话题：这条只是她没什么感觉的一条内容，不算可说的事，也不提是听群里聊的
    elsewhere = _proactive(store, is_private=False, topics=[{"topic": "显卡涨价", "keywords": []}])[
        "share_candidates"
    ]
    assert [c["episode_id"] for c in elsewhere] == ["e1"]
    assert store.state.get("heard_topics", []) == []  # 主动开口带来的话题词不算又听到一次


def test_fresh_learning_is_offered_once(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.learnings.append(
        {
            "id"        : "l1",
            "topic"     : "锅具预热",
            "text"      : "预热没有通用的固定秒数",
            "confidence": "verified",
            "created_at": NOW - 5 * HOUR,
        }
    )
    store.learnings.append(
        {"id": "l0", "topic": "旧事", "text": "很久以前查的", "created_at": NOW - 200 * HOUR}
    )
    got = _proactive(store)["share_candidates"]
    assert [c["episode_id"] for c in got] == ["learning:l1"]
    assert "你这几天自己查明白了一件事（锅具预热）" in got[0]["line"]
    assert "预热没有通用的固定秒数" in got[0]["line"]
    assert _proactive(store, exclude_ids=["learning:l1"]) is None
