"""对照 dafeiyu：朋友们在聊的话题成为上网方向；学到的东西分“查实了”和“只看到有人这么说”。"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import chat_topics, dossier, knowledge, planner, research, wind_down
from plugins.xiaoqing_life import threads as th
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.activity import SessionDeps
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.planner import INTEREST, Decision
from plugins.xiaoqing_life.session import run_session
from plugins.xiaoqing_life.sources.base import SourceItem
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.plugins.xiaoqing_life.support import (
    FakeAI,
    FakeFetcher,
    at,
    comments_payload,
    make_cfg,
    new_store,
)

MOMENT = at(23, 0)  # 2026-11-05 周四，睡前
NOW    = MOMENT.timestamp()
DAY    = 86400.0
FREE   = Presence("free", "", None)


class Always:
    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value

    def randint(self, low: int, high: int) -> int:
        return low


def _payload(text: str = "你看了新番吗", *, private: bool = False, **kw: Any) -> dict[str, Any]:
    return {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : text,
        "recent"       : [],
        "bot_recent"   : [],
        "is_private"   : private,
        "unknown_terms": [],
        **kw,
    }


TOPICS = [
    {"topic": "新番推荐", "keywords": ["芙莉莲", "十月番"]},
    {"topic": "期末考试", "keywords": []},
]


# ───────────────────────────── 听到朋友们在聊什么 ─────────────────────────────


def test_group_topics_are_heard_and_private_ones_are_not(tmp_path: Path) -> None:
    store, cfg = new_store(tmp_path), make_cfg()
    _, dirty = tc.turn_context(store, cfg, _payload(topics=TOPICS), MOMENT, random.Random(0))
    assert dirty and [h["topic"] for h in store.state["heard_topics"]] == ["新番推荐", "期末考试"]
    tc.turn_context(store, cfg, _payload(topics=TOPICS[:1]), MOMENT, random.Random(0))
    assert store.state["heard_topics"][0]["turns"] == 2
    private = new_store(tmp_path / "p")
    tc.turn_context(private, cfg, _payload(private=True, topics=TOPICS), MOMENT, random.Random(0))
    assert private.state["heard_topics"] == []
    # 没写 is_private 的旧请求也不收
    legacy = new_store(tmp_path / "l")
    request = _payload(topics=TOPICS)
    del request["is_private"]
    tc.turn_context(legacy, cfg, request, MOMENT, random.Random(0))
    assert legacy.state["heard_topics"] == []


def test_heard_topics_are_bounded_and_cleaned() -> None:
    rows = chat_topics.clean_topics(
        [
            {
                "topic"   : "超长的话题名字一二三四五六七八九十",
                "keywords": ["很长很长的关键词呀", 3, "b"],
            },
            {"topic": "x"},
            "bad",
            {"topic": "第四条只看前三条", "keywords": []},
        ]
    )
    assert [r["topic"] for r in rows] == [
        "超长的话题名字一二三四五"
    ]  # 每轮最多看 3 条，话题 ≤12 字
    assert rows[0]["keywords"] == ["很长很长的关键词", "b"]
    state = default_state()
    assert (
        chat_topics.record_heard(state, [{"topic": "黑名单话题", "keywords": []}], NOW, ("黑名单",))
        is False
    )
    for n in range(50):
        distinct = "".join(chr(0x4E00 + 3 * n + k) for k in range(3))  # 互不相似的话题
        chat_topics.record_heard(state, [{"topic": distinct, "keywords": []}], NOW + n, ())
    assert len(state["heard_topics"]) == chat_topics.MAX_HEARD
    chat_topics.prune(state, NOW + 20 * DAY)
    assert state["heard_topics"] == []


def test_review_offers_recent_unpicked_topics_and_only_accepts_offered_ones() -> None:
    state = default_state()
    chat_topics.record_heard(state, TOPICS, NOW - 3600, ())
    chat_topics.record_heard(state, [{"topic": "旧话题", "keywords": []}], NOW - 5 * DAY, ())
    offered = chat_topics.for_review(state, NOW)
    assert [h["topic"] for h in offered] == ["新番推荐", "期末考试"]
    picks = [
        {"topic": "新番推荐", "query": "十月新番 推荐", "why": "大家都在追"},
        {"topic": "没给过的话题", "query": "x", "why": ""},
        {"topic": "期末考试", "query": "", "why": "没写搜索词"},
    ]
    assert chat_topics.apply_review(state, offered, picks, NOW, ()) == 1
    statuses = {h["topic"]: h["status"] for h in state["heard_topics"]}
    assert statuses["新番推荐"] == "picked" and statuses["期末考试"] == "skipped"
    assert [p["query"] for p in chat_topics.fresh(state, NOW)] == ["十月新番 推荐"]
    assert chat_topics.for_review(state, NOW + 60) == []
    # 没挑的过一周、又听到了才再给她看
    chat_topics.record_heard(state, [{"topic": "期末考试", "keywords": []}], NOW + 8 * DAY, ())
    assert [h["topic"] for h in chat_topics.for_review(state, NOW + 8 * DAY)] == ["期末考试"]


@pytest.mark.asyncio
async def test_wind_down_lets_her_pick_public_topics_from_what_friends_talked_about(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    store.episodes.append(
        {
            "id"      : "e1",
            "at"      : NOW - 600,
            "title"   : "视频",
            "reaction": "好",
            "feeling" : "like",
            "topics"  : [],
        }
    )
    chat_topics.record_heard(store.state, TOPICS, NOW - 3600, ())
    reply = {
        "diary"         : {"date": "2026-11-05", "text": "今天刷了视频。", "episode_ids": ["e1"]},
        "friends_topics": [{"topic": "新番推荐", "query": "十月新番", "why": "群里都在聊"}],
    }
    ai = FakeAI(reply)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    result = await wind_down.run_wind_down(
        store, make_cfg(), MOMENT, complete, persona="", moment_line="", reasoning_ok=True
    )
    record = json.loads(ai.calls[0][1][1]["content"])["untrusted_record"]
    assert [h["topic"] for h in record["friends_topics_heard"]] == ["新番推荐", "期末考试"]
    assert "谁的私事" in ai.calls[0][1][0]["content"]
    assert result.details["friends_topics"] == 1
    assert [p["topic"] for p in store.state["chat_topics"]] == ["新番推荐"]


# ───────────────────────────── 去看看朋友们在聊的 ─────────────────────────────


def _picked(state: dict[str, Any], at_: float = NOW) -> None:
    state["chat_topics"] = [
        {"topic": "新番推荐", "query": "十月新番", "why": "", "picked_at": at_, "tried_at": 0.0}
    ]


def test_a_fresh_pick_raises_interest_dives_and_skips_the_dice() -> None:
    cfg   = make_cfg()
    state = default_state() | {"last_session_end": NOW - 5 * 3600}
    morning = at(11, 0, day=6)
    assert (
        planner.gate(
            cfg, state, [], morning, FREE, Always(0.99), running=False, manual=False
        ).reason
        == "dice"
    )
    _picked(state)
    assert (
        planner.gate(cfg, state, [], morning, FREE, Always(0.99), running=False, manual=False)
        is None
    )
    decision = planner.choose(cfg, state, [], {}, morning, Always(0.0))
    assert decision.factors["chat_topic"] == planner.CHAT_TOPIC_BONUS
    params = planner._interest_params(
        cfg, state, {}, morning.timestamp(), Always(0.0), [], anime=False, overdue=False
    )
    assert params == {"query": "十月新番", "explored": True, "chat_topic": "新番推荐"}
    # 看过一次就不再是火花，也不再加分
    chat_topics.mark_tried(state, "新番推荐", morning.timestamp())
    assert chat_topics.fresh(state, morning.timestamp()) == []
    assert "chat_topic" not in planner.choose(cfg, state, [], {}, morning, Always(0.0)).factors


def _deps(fetch: FakeFetcher, ai: FakeAI, moment: datetime) -> SessionDeps:
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    return SessionDeps(fetch=fetch, complete=complete, rng=random.Random(1), now=lambda: moment)


@pytest.mark.asyncio
async def test_a_dive_on_a_friends_topic_records_why_and_never_penalizes_the_topic(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    morning = at(11, 0, day=6)
    _picked(store.state)
    fetch = FakeFetcher(
        {
            "/search/type": {
                "code": 0,
                "data": {
                    "result": [
                        {
                            "id"         : 7,
                            "bvid"       : "BV7",
                            "title"      : "十月新番盘点",
                            "description": "简介",
                            "play"       : 9,
                        }
                    ]
                },
            },
            "/reply/main": comments_payload(),
        }
    )
    reply = {
        "looked_at": [{"item": "i1", "reaction": "一般", "feeling": "meh", "topics": ["新番"]}]
    }
    ai       = FakeAI(reply)
    decision = Decision(
        INTEREST, "sampled", {}, {"query": "十月新番", "explored": True, "chat_topic": "新番推荐"}
    )
    await run_session(store, make_cfg(), decision, _deps(fetch, ai, morning))
    assert store.episodes[0]["chat_topic"] == "新番推荐"
    assert "群里朋友们最近在聊「新番推荐」" in ai.calls[0][1][0]["content"]
    assert store.state["chat_topics"][0]["tried_at"] > 0
    assert store.state["topic_penalty"] == {}  # 没看到喜欢的也不记负面证据
    line = tc.episode_line(store.episodes[0], at(20, 0, day=6), make_cfg())
    assert "听群里在聊「新番推荐」才去搜的" in line


# ───────────────────────────── 知识门控 ─────────────────────────────


def _ep(eid: str, uploader: str = "", source: str = "bili_search", item: str = "") -> dict:
    return {"id": eid, "item_id": item or f"bili:{eid}", "uploader": uploader, "source": source}


def test_one_uploader_is_one_source_and_encyclopedias_count_as_checked() -> None:
    same = [_ep("a", "老王"), _ep("b", "老王")]
    assert knowledge.assess(same) == (knowledge.HYPOTHESIS, 1)
    assert knowledge.assess([_ep("a", "老王"), _ep("c", "小李")]) == (knowledge.VERIFIED, 2)
    assert (
        knowledge.assess([_ep("w", source="wiki_read", item="wiki_read:1")])[0]
        == knowledge.VERIFIED
    )
    assert knowledge.assess([_ep("a"), _ep("b")]) == (knowledge.VERIFIED, 2)  # 没 UP 主按条目算
    assert knowledge.hedge("水会悬浮", knowledge.HYPOTHESIS).endswith(knowledge.UNVERIFIED_NOTE)
    assert knowledge.hedge("水会悬浮", knowledge.VERIFIED) == "水会悬浮"


def test_rumor_and_ad_material_cannot_back_a_finding() -> None:
    for text in ("震惊！99%的人不知道", "加微信领福利", "转发保平安", "限时秒杀点击下方链接"):
        assert knowledge.is_rumor(text), text
    assert not knowledge.is_rumor("莱顿弗罗斯特效应是一种物理现象")
    items = [
        SourceItem("bili:1", "bili_search", "震惊！锅这样用会爆炸", "简介", "", 1, ()),
        SourceItem("bili:2", "bili_search", "锅的导热原理", "铁的导热比不锈钢好", "", 1, ()),
    ]
    raw = {
        "findings": [
            {"text": "锅会爆炸", "quote": "锅这样用会爆炸", "refs": ["i1"]},
            {"text": "铁导热更好", "quote": "铁的导热比不锈钢好", "refs": ["i2"]},
        ]
    }
    result = research.validate_round(raw, items, {"notes": []}, ())
    assert [f.text for f in result.findings] == ["铁导热更好"]


def _thread(tid: str, status: str, notes: list[str], **kw: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "title": f"课题{tid}",
        "topic": "烹饪物理",
        "why": "",
        "next_query": "锅 导热",
        "status": status,
        "steps": 1,
        "empty_streak": 0,
        "notes": [{"id": f"n{i}", "episode_id": n, "note": "要点"} for i, n in enumerate(notes)],
        "takeaway": "",
        "created_at": NOW - DAY,
        "updated_at": NOW - 100,
        "depth": 0,
        **kw,
    }


def _episode(eid: str, uploader: str, source: str = "bili_search") -> dict[str, Any]:
    return {
        "id"      : eid,
        "at"      : NOW - 600,
        "item_id" : f"bili:{eid}",
        "uploader": uploader,
        "source"  : source,
        "title"   : f"视频{eid}",
        "reaction": "有意思",
        "feeling" : "like",
        "topics"  : [],
    }


async def _wind(store: LifeStore, reply: dict[str, Any]) -> tuple[Any, FakeAI]:
    ai = FakeAI({"diary": {"date": "2026-11-05", "text": "今天。", "episode_ids": ["e1"]}, **reply})

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    result = await wind_down.run_wind_down(
        store, make_cfg(), MOMENT, complete, persona="", moment_line="", reasoning_ok=True
    )
    return result, ai


@pytest.mark.asyncio
async def test_a_single_source_takeaway_is_a_hypothesis_and_gets_a_verification_thread(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    store.episodes += [_episode("e1", "老王"), _episode("e2", "老王")]
    store.threads.append(_thread("t1", "reviewing", ["e1", "e2"], kind="fact"))
    done = {"id": "t1", "action": "done", "takeaway": "铁锅导热更好", "episode_ids": ["e1"]}
    result, _ = await _wind(store, {"threads": [done]})
    learning = store.learnings[0]
    assert (learning["confidence"], learning["sources"]) == (knowledge.HYPOTHESIS, 1)
    check = store.threads[-1]
    assert check["title"] == "核实：课题t1" and check["parent_id"] == "t1"
    assert (check["verifies"], check["claim"], check["kind"]) == (
        learning["id"],
        "铁锅导热更好",
        "fact",
    )
    assert result.details["verify"] == 1 and store.state["done_on"]["verify"] == "2026-11-05"
    assert check["claim_basis"] == []  # 夹具里的笔记没有摘录
    # 聊天时说清还没核实；档案也一样
    req = tc.clean_payload(_payload("最近学到啥"))
    lines, _ = tc.learning_lines(store, req, NOW)
    assert lines[0] == f"- 铁锅导热更好{knowledge.UNVERIFIED_NOTE}"
    dossier.sync(store, make_cfg(), NOW)
    question = next(q for q in store.dossiers["烹饪物理"]["questions"] if q["thread_id"] == "t1")
    assert dossier._question_text(question).startswith("查到一个说法：铁锅导热更好")
    # 一天最多开一个
    assert not wind_down.open_verification(store, make_cfg(), [store.threads[0]], "2026-11-05", NOW)


@pytest.mark.asyncio
async def test_practice_takeaways_stay_hedged_but_are_not_auto_verified(tmp_path: Path) -> None:
    """做法、体验类的收获只有一个 UP 主很正常：照实说没核实，不占小课题名额去核实。"""

    store = new_store(tmp_path)
    store.episodes += [_episode("e1", "老王")]
    store.threads.append(_thread("t1", "reviewing", ["e1"], kind="practice"))
    done = {"id": "t1", "action": "done", "takeaway": "先热锅再放油", "episode_ids": ["e1"]}
    result, _ = await _wind(store, {"threads": [done]})
    assert store.learnings[0]["confidence"] == knowledge.HYPOTHESIS
    assert [t["id"] for t in store.threads] == ["t1"] and result.details["verify"] == 0


@pytest.mark.asyncio
async def test_two_independent_sources_need_no_verification(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.episodes += [_episode("e1", "老王"), _episode("e2", "小李")]
    store.threads.append(_thread("t1", "reviewing", ["e1", "e2"]))
    done = {"id": "t1", "action": "done", "takeaway": "铁锅导热更好", "episode_ids": ["e1"]}
    await _wind(store, {"threads": [done]})
    assert store.learnings[0]["confidence"] == knowledge.VERIFIED
    assert [t["id"] for t in store.threads] == ["t1"]


def _verifying(store: LifeStore, check_uploader: str) -> dict[str, Any]:
    store.episodes += [_episode("e1", "老王"), _episode("e9", check_uploader)]
    store.threads.append(_thread("t1", "done", ["e1"], takeaway="铁锅导热更好"))
    store.learnings.append(
        {"id": "l1", "topic": "课题t1", "text": "铁锅导热更好", "thread_id": "t1", "episode_ids": ["e1"],
         "created_at": NOW - DAY, "last_used": 0.0, "confidence": knowledge.HYPOTHESIS, "sources": 1}
    )  # fmt: skip
    check = _thread(
        "v1", "reviewing", ["e9"], parent_id="t1", depth=1, verifies="l1", claim="铁锅导热更好"
    )
    store.threads.append(check)
    return check


@pytest.mark.asyncio
async def test_a_confirmed_check_from_another_uploader_verifies_the_old_learning(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    _verifying(store, "小李")
    row = {
        "id"         : "v1",
        "action"     : "done",
        "takeaway"   : "对上了",
        "episode_ids": ["e9"],
        "verdict"    : "confirmed",
    }
    followup = {"parent_id": "v1", "title": "再查", "why": "", "done_when": "x", "next_query": "q"}
    result, ai = await _wind(store, {"threads": [row], "followup": followup})
    assert len(store.learnings) == 1  # 更新原来那条，不另记
    learning = store.learnings[0]
    assert (learning["confidence"], learning["verified_by"]) == (knowledge.VERIFIED, "v1")
    assert th.get_thread(store.threads, "t1")["confidence"] == knowledge.VERIFIED
    assert not result.followup and len(store.threads) == 2  # 核实课题不往下延伸，也不再开核实
    sent = json.loads(ai.calls[0][1][1]["content"])["untrusted_record"]["threads"]
    assert sent[0]["claim"] == "铁锅导热更好"


@pytest.mark.asyncio
async def test_a_check_from_the_same_uploader_does_not_count(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    _verifying(store, "老王")
    row = {
        "id"         : "v1",
        "action"     : "done",
        "takeaway"   : "对上了",
        "episode_ids": ["e9"],
        "verdict"    : "confirmed",
    }
    await _wind(store, {"threads": [row]})
    learning = store.learnings[0]
    assert learning["confidence"] == knowledge.HYPOTHESIS and learning["verify_failed"] is True


@pytest.mark.asyncio
async def test_a_refuted_claim_is_corrected_and_the_dossier_notes_the_change(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    _verifying(store, "百科")
    store.episodes[-1]["source"] = "baike_read"
    row                          = {
        "id"         : "v1",
        "action"     : "done",
        "takeaway"   : "铸铁锅导热其实比铝锅差",
        "episode_ids": ["e9"],
        "verdict"    : "refuted",
    }
    await _wind(store, {"threads": [row]})
    learning = store.learnings[0]
    assert (
        learning["text"] == "铸铁锅导热其实比铝锅差"
        and learning["confidence"] == knowledge.VERIFIED
    )
    parent = th.get_thread(store.threads, "t1")
    assert parent is not None
    # 原小课题的收获也换成核实后的说法：聊天“你前几天查过……”不再挂着被推翻的那句
    assert parent["takeaway"] == "铸铁锅导热其实比铝锅差"
    assert parent["confidence"] == knowledge.VERIFIED
    assert (parent["corrections"][-1]["before"], parent["corrections"][-1]["after"]) == (
        "铁锅导热更好",
        "铸铁锅导热其实比铝锅差",
    )


@pytest.mark.asyncio
async def test_dropping_a_check_leaves_the_learning_unverified(tmp_path: Path) -> None:
    store           = new_store(tmp_path)
    check           = _verifying(store, "小李")
    check["status"] = "open"
    await _wind(store, {"threads": [{"id": "v1", "action": "drop", "reason": "找不到"}]})
    assert store.learnings[0]["verify_failed"] is True
    assert store.learnings[0]["confidence"] == knowledge.HYPOTHESIS


def test_the_research_prompt_carries_the_claim_being_checked() -> None:
    check = _thread(
        "v1",
        "open",
        [],
        claim       = "铁锅导热更好",
        claim_basis = ["铸铁的导热系数比不锈钢高"],
        done_when   = "找到另一个来源对上这个说法",
    )
    messages = research.build_messages(
        persona="", thread=check, items=[], methods=[], round_no=1, rounds_left=2
    )
    assert "question.claim 不为空时" in messages[0]["content"]
    question = json.loads(messages[1]["content"])["question"]
    assert question["claim"] == "铁锅导热更好"
    assert question["claim_basis"] == ["铸铁的导热系数比不锈钢高"]


@pytest.mark.asyncio
async def test_a_check_that_found_evidence_goes_to_review_instead_of_parking(
    tmp_path: Path,
) -> None:
    """真实回放：第二轮维基对上了，第三轮 B站没东西写了 stuck，整个核实被搁置、依据白查了。"""

    store = new_store(tmp_path)
    check = _thread(
        "t1", "open", [], kind="fact", claim="水珠跳是因为蒸气层", next_query="莱顿弗罗斯特效应"
    )
    store.threads.append(check)
    page = {
        "pageid" : 1,
        "index"  : 1,
        "title"  : "莱顿弗罗斯特效应",
        "extract": "液体在炽热表面上形成蒸气层。",
    }
    fetch = FakeFetcher({"Baike": {}, "wikipedia": {"query": {"pages": {"1": page}}}})
    reply = {
        "looked_at": [],
        "findings" : [
            {"text": "维基说会形成蒸气层", "quote": "液体在炽热表面上形成蒸气层", "refs": ["p1.1"]}
        ],
        "status": "stuck",
        "next"  : {"action": "stop"},
    }
    decision = Decision("thread_continue", "sampled", {}, {"thread_id": "t1", "query": ""})
    await run_session(
        store, make_cfg(research_max_rounds=1), decision, _deps(fetch, FakeAI(reply), MOMENT)
    )
    assert check["status"] == "reviewing" and len(check["notes"]) == 1
    # 什么都没查到的核实照常搁置
    empty = new_store(tmp_path / "e")
    other = _thread("t1", "open", [], kind="fact", claim="x", next_query="莱顿弗罗斯特效应")
    empty.threads.append(other)
    nothing = {**reply, "findings": []}
    fetch2  = FakeFetcher({"Baike": {}, "wikipedia": {"query": {"pages": {"1": page}}}})
    await run_session(
        empty, make_cfg(research_max_rounds=1), decision, _deps(fetch2, FakeAI(nothing), MOMENT)
    )
    assert other["status"] == "parked"


@pytest.mark.asyncio
async def test_terms_baike_does_not_know_are_not_queued_again_for_a_month(tmp_path: Path) -> None:
    """真实回放：“依托答辩”百科没有词条；聊天每次听到本群没确认的说法都会交过来，不能每次都去查。"""

    store = new_store(tmp_path)
    cfg   = make_cfg()
    tc.turn_context(
        store, cfg, _payload(unknown_terms=["依托答辩", "city不city"]), MOMENT, random.Random(0)
    )
    assert store.state["lookup_queue"] == ["依托答辩", "city不city"]
    card     = {"title": "city不city", "desc": "网络流行语", "abstract": "时髦不时髦。"}
    fetch    = FakeFetcher({"Baike": lambda p: card if p["bk_key"] == "city不city" else {}})
    decision = Decision("term_lookup", "forced", {}, {"terms": ["依托答辩", "city不city"]})
    await run_session(store, cfg, decision, _deps(fetch, FakeAI(), MOMENT))
    assert store.terms["city不city"]["verified"] is True
    assert list(store.state["lookup_misses"]) == ["依托答辩"]
    later = at(12, 0, day=20)
    tc.turn_context(store, cfg, _payload(unknown_terms=["依托答辩"]), later, random.Random(0))
    assert store.state["lookup_queue"] == []
    much_later = at(12, 0, day=30).replace(month=12)
    tc.turn_context(store, cfg, _payload(unknown_terms=["依托答辩"]), much_later, random.Random(0))
    assert store.state["lookup_queue"] == ["依托答辩"]  # 过了 30 天再查一次
