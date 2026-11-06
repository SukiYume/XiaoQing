"""整次上网：假 HTTP + 假模型端到端，成功落地以及各条失败路径。"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life.planner import (
    HOT_SCROLL,
    INTEREST,
    TERM_LOOKUP,
    THREAD,
    Decision,
)
from plugins.xiaoqing_life.session import SessionDeps, run_session
from plugins.xiaoqing_life.sources.base import SourceError
from plugins.xiaoqing_life.stores import LifeStore
from tests.plugins.xiaoqing_life.support import (
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

DIGEST = {
    "looked_at": [
        {
            "item"    : "i1",
            "reaction": "猫猫好可爱，想养",
            "feeling" : "like",
            "share"   : True,
            "topics"  : ["猫"],
        },
        {"item": "i2", "reaction": "显卡太贵了", "feeling": "dislike", "topics": ["装机"]},
    ],
    "terms": [
        {
            "term"   : "哈基米",
            "meaning": "对猫咪的爱称",
            "usage"  : "看到猫时感叹",
            "tone"   : "夸",
            "quote"  : "哈基米这也太可爱",
            "refs"   : ["c1.1"],
        },
        {"term": "凭空词", "meaning": "编的", "quote": "不存在的摘录", "refs": ["c1.1"]},
    ],
    "expressions": [
        {
            "situation": "当想夸可爱时",
            "pattern"  : "X基础，Y不基础",
            "kind"     : "phrase",
            "quote"    : "猫猫基础",
            "refs"     : ["c1.2"],
        },
    ],
    "meme_pictures": [],
    "thread"       : {
        "action"    : "open",
        "title"     : "猫为什么爱纸箱",
        "why"       : "好奇",
        "next_query": "猫 纸箱 原因",
        "note"      : "",
        "refs"      : ["i1"],
    },
}


def _fetch(**extra: Any) -> FakeFetcher:
    routes: dict[str, Any] = {
        "/popular"   : popular_payload(bili_video(1, "猫猫的一天"), bili_video(2, "新显卡评测")),
        "/reply/main": lambda params: (
            comments_payload(
                bili_reply("哈基米这也太可爱了吧"), bili_reply("这就是传说中的猫猫基础")
            )
            if params["oid"] == "1"
            else comments_payload(bili_reply("价格劝退"))
        ),
    }
    routes.update(extra)
    return FakeFetcher(routes)


def _deps(fetch: FakeFetcher, ai: FakeAI, **kw: Any) -> SessionDeps:
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    return SessionDeps(
        fetch    = fetch,
        complete = complete,
        rng      = random.Random(1),
        now      = lambda: MOMENT,
        persona  = "你是小青。",
        **kw,
    )


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


def _hot() -> Decision:
    return Decision(HOT_SCROLL, "best_score", {HOT_SCROLL: 40.0}, {"source": "bili_popular"})


@pytest.mark.asyncio
async def test_successful_session_lands_everything_with_evidence(tmp_path: Path) -> None:
    store, ai = _store(tmp_path), FakeAI(DIGEST)
    outcome = await run_session(store, make_cfg(), _hot(), _deps(_fetch(), ai))

    assert (outcome.status, outcome.episodes, outcome.terms) == ("ok", 2, 1)
    assert len(ai.calls) == 1 and ai.calls[0][0] == "reasoning"
    assert [e["item_id"] for e in store.episodes] == ["bili:1", "bili:2"]
    first = store.episodes[0]
    assert (first["activity"], first["feeling"], first["share"], first["day"]) == (
        HOT_SCROLL,
        "like",
        True,
        "2026-11-05",
    )
    assert first["term_keys"] == ["哈基米"] and first["title"] == "猫猫的一天"
    # 词条只收有真实摘录的那个，且来源是它出现的条目
    assert list(store.terms) == ["哈基米"] and store.terms["哈基米"]["sources"] == ["bili:1"]
    assert store.terms["哈基米"]["status"] == "candidate"
    assert store.expressions[0]["pattern"] == "X基础，Y不基础"
    assert (
        store.interests["猫"]["stage"] == "tentative"
        and store.interests["装机"]["evidence"][0]["delta"] == -1
    )
    assert store.threads[0]["title"] == "猫为什么爱纸箱" and store.threads[0]["status"] == "open"
    assert set(store.seen) == {"bili:1", "bili:2"}
    state = store.state
    assert (state["sessions_today"], state["llm_calls_today"], state["day"]) == (1, 1, "2026-11-05")
    assert state["last_decision"]["choice"] == HOT_SCROLL and state["last_session_at"] == NOW
    assert state["activity_log"][0]["start"] == NOW and state["recent"][-1]["choice"] == HOT_SCROLL
    assert "interest_dive" not in json.dumps(state["last_decision"])
    # 重启后仍在
    again = _store(tmp_path)
    assert len(again.episodes) == 2 and again.state["llm_calls_today"] == 1
    # 直接送进模型的材料里没有链接
    assert "http" not in ai.calls[0][1][1]["content"]


@pytest.mark.asyncio
async def test_second_run_with_only_seen_items_makes_no_model_call(tmp_path: Path) -> None:
    store, ai = _store(tmp_path), FakeAI(DIGEST)
    await run_session(store, make_cfg(), _hot(), _deps(_fetch(), ai))
    outcome = await run_session(store, make_cfg(), _hot(), _deps(_fetch(), ai))
    assert outcome.reason == "all_seen" and len(ai.calls) == 1
    assert len(store.episodes) == 2 and store.state["llm_calls_today"] == 1


@pytest.mark.asyncio
async def test_all_sources_failing_records_a_failure_and_nothing_else(tmp_path: Path) -> None:
    store, ai = _store(tmp_path), FakeAI(DIGEST)
    down = {
        name: SourceError("x", "Timeout")
        for name in ("/popular", "/ranking", "hot-lists", "hotSearch")
    }
    outcome = await run_session(store, make_cfg(), _hot(), _deps(FakeFetcher(down), ai))
    assert (outcome.status, outcome.reason) == ("failed", "all_sources_failed")
    assert ai.calls == [] and store.episodes == [] and store.terms == {} and store.seen == {}
    assert store.state["llm_calls_today"] == 0 and store.state["last_failure_at"] == NOW


@pytest.mark.asyncio
async def test_hot_scroll_falls_back_to_the_next_enabled_source(tmp_path: Path) -> None:
    store, ai = _store(tmp_path), FakeAI({"looked_at": []})
    fetch = _fetch(
        **{
            "/popular" : SourceError("x", "Timeout"),
            "hot-lists": {
                "data": [
                    {
                        "target"     : {"id": 5, "title": "知乎热门问题", "excerpt": "e"},
                        "detail_text": "9 万热度",
                    }
                ]
            },
        }
    )
    outcome = await run_session(store, make_cfg(), _hot(), _deps(fetch, ai))
    assert outcome.status == "ok" and outcome.items == 1
    assert "zhihu_hot:5" in store.seen


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [RuntimeError("boom"), "不是 json", ""])
async def test_model_failure_or_garbage_yields_no_learning_but_burns_budget(
    tmp_path: Path, reply: Any
) -> None:
    store, ai = _store(tmp_path), FakeAI(reply)
    outcome = await run_session(store, make_cfg(), _hot(), _deps(_fetch(), ai))
    assert outcome.status == "failed"
    assert (
        store.episodes == []
        and store.terms == {}
        and store.expressions == []
        and store.threads == []
    )
    assert store.seen == {}  # 没消化成功的条目不算看过
    assert store.state["llm_calls_today"] == 1 and store.state["last_failure_at"] == NOW


@pytest.mark.asyncio
async def test_missing_model_route_skips_the_call_and_the_budget(tmp_path: Path) -> None:
    store, ai = _store(tmp_path), FakeAI(DIGEST)
    outcome = await run_session(store, make_cfg(), _hot(), _deps(_fetch(), ai, reasoning_ok=False))
    assert outcome.reason == "model_not_configured" and ai.calls == []
    assert store.state["llm_calls_today"] == 0 and store.episodes == []
    assert store.state["last_failure_at"] == NOW


@pytest.mark.asyncio
async def test_exhausted_daily_model_budget_makes_no_call(tmp_path: Path) -> None:
    store, ai = _store(tmp_path), FakeAI(DIGEST)
    outcome = await run_session(
        store, make_cfg(max_llm_calls_per_day=0), _hot(), _deps(_fetch(), ai)
    )
    assert outcome.status == "skipped" and ai.calls == [] and store.episodes == []


def _thread_store(tmp_path: Path, **thread: Any) -> LifeStore:
    store = _store(tmp_path)
    store.threads.append(
        {
            "id"            : "t1",
            "title"         : "猫为什么爱纸箱",
            "why"           : "好奇",
            "next_query"    : "猫 纸箱",
            "status"        : "open",
            "steps"         : 0,
            "empty_streak"  : 0,
            "cooldown_until": 0.0,
            "notes"         : [],
            "takeaway"      : "",
            "created_at"    : NOW - 86400,
            "updated_at"    : NOW - 86400,
            **thread,
        }
    )
    return store


def _continue() -> Decision:
    return Decision(THREAD, "best_score", {THREAD: 70.0}, {"thread_id": "t1", "query": "猫 纸箱"})


def _search(*videos: dict[str, Any]) -> dict[str, Any]:
    return {"/search/type": {"code": 0, "data": {"result": list(videos)}}}


def _video(aid: int, title: str) -> dict[str, Any]:
    return {"id": aid, "bvid": f"BV{aid}", "title": title, "description": "简介", "play": 10}


@pytest.mark.asyncio
async def test_thread_continue_progress_then_step_limit_moves_to_reviewing(tmp_path: Path) -> None:
    store = _thread_store(tmp_path, steps=2)
    digest = {
        "looked_at": [
            {"item": "i1", "reaction": "原来猫喜欢躲着", "feeling": "like", "topics": ["猫"]}
        ],
        "findings": [{"text": "纸箱让猫有安全感", "quote": "猫咪为什么爱钻纸箱", "refs": ["i1"]}],
        "status"  : "progress",
        "next"    : {"action": "stop"},
    }
    fetch = FakeFetcher(
        {**_search(_video(7, "猫咪为什么爱钻纸箱")), "/reply/main": comments_payload()}
    )
    await run_session(
        store, make_cfg(thread_max_steps=3), _continue(), _deps(fetch, FakeAI(digest))
    )
    thread = store.threads[0]
    assert thread["steps"] == 3 and thread["status"] == "reviewing"
    assert thread["notes"][0]["episode_id"] == store.episodes[0]["id"]
    assert thread["notes"][0]["note"] == "纸箱让猫有安全感" and thread["notes"][0]["id"] == "n1"
    assert store.episodes[0]["thread_id"] == "t1"
    assert fetch.calls[0][1]["keyword"] == "猫 纸箱"


@pytest.mark.asyncio
async def test_thread_continue_answered_moves_to_reviewing_immediately(tmp_path: Path) -> None:
    store  = _thread_store(tmp_path)
    digest = {
        "looked_at": [{"item": "i1", "reaction": "懂了", "feeling": "like"}],
        "findings" : [{"text": "查明白了", "quote": "答案视频", "refs": ["i1"]}],
        "status"   : "answered",
    }
    fetch = FakeFetcher({**_search(_video(7, "答案视频")), "/reply/main": comments_payload()})
    await run_session(store, make_cfg(), _continue(), _deps(fetch, FakeAI(digest)))
    assert store.threads[0]["status"] == "reviewing" and store.threads[0]["takeaway"] == ""


@pytest.mark.asyncio
async def test_thread_continue_without_news_cools_down_after_two_empty_steps(
    tmp_path: Path,
) -> None:
    store = _thread_store(tmp_path)
    fetch = FakeFetcher({**_search(_video(7, "旧视频")), "/reply/main": comments_payload()})
    store.seen["bili:7"] = NOW - 100  # 已经看过（只开搜索：不换成查词条）
    ai = FakeAI()
    await run_session(
        store,
        make_cfg(sources={"baike_lookup": False, "wiki_read": False}),
        _continue(),
        _deps(fetch, ai),
    )
    assert store.threads[0]["empty_streak"] == 1 and ai.calls == []
    await run_session(
        store,
        make_cfg(sources={"baike_lookup": False, "wiki_read": False}),
        _continue(),
        _deps(fetch, ai),
    )
    assert store.threads[0]["cooldown_until"] == NOW + 180 * 60 and store.threads[0]["steps"] == 0


@pytest.mark.asyncio
async def test_digest_without_progress_counts_as_an_empty_step(tmp_path: Path) -> None:
    store  = _thread_store(tmp_path)
    digest = {
        "looked_at": [{"item": "i1", "reaction": "没啥用", "feeling": "meh"}],
        "thread"   : {"action": "none"},
    }
    fetch = FakeFetcher({**_search(_video(7, "无关视频")), "/reply/main": comments_payload()})
    await run_session(store, make_cfg(), _continue(), _deps(fetch, FakeAI(digest)))
    assert store.threads[0]["steps"] == 0 and store.threads[0]["empty_streak"] == 1


@pytest.mark.asyncio
async def test_interest_dive_search_failure_falls_back_to_popular(tmp_path: Path) -> None:
    store    = _store(tmp_path)
    fetch    = _fetch(**{"/search/type": SourceError("x", "code_412")})
    decision = Decision(INTEREST, "best_score", {}, {"query": "天文摄影"})
    outcome  = await run_session(
        store, make_cfg(), decision, _deps(fetch, FakeAI({"looked_at": []}))
    )
    assert outcome.status == "ok" and outcome.items == 2


@pytest.mark.asyncio
async def test_term_lookup_verifies_slang_and_settles_the_queue(tmp_path: Path) -> None:
    store                       = _store(tmp_path)
    store.state["lookup_queue"] = ["哈基米", "普通词", "超时词", "第四个"]
    slang                       = {"title": "哈基米", "desc": "网络流行语", "abstract": "对猫咪的爱称。"}

    def baike(params):
        if params["bk_key"] == "超时词":
            raise SourceError("baike", "TimeoutError")
        return slang if params["bk_key"] == "哈基米" else {}

    ai       = FakeAI()
    decision = Decision(TERM_LOOKUP, "best_score", {}, {"terms": ["哈基米", "普通词", "超时词"]})
    outcome  = await run_session(
        store, make_cfg(), decision, _deps(FakeFetcher({"Baike": baike}), ai)
    )
    assert outcome.terms == 1 and ai.calls == []  # 查词不调模型
    entry = store.terms["哈基米"]
    assert entry["status"] == "known" and entry["verified"] and entry["sources"] == ["baike:哈基米"]
    assert store.state["lookup_queue"] == ["超时词", "第四个"]  # 查不到丢弃，出错的留着
    assert store.episodes == []


@pytest.mark.asyncio
async def test_term_lookup_with_every_request_failing_is_a_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.state["lookup_queue"] = ["甲"]
    decision = Decision(TERM_LOOKUP, "best_score", {}, {"terms": ["甲"]})
    fetch = FakeFetcher({"Baike": SourceError("baike", "TimeoutError")})
    outcome = await run_session(store, make_cfg(), decision, _deps(fetch, FakeAI()))
    assert outcome.status == "failed" and store.state["lookup_queue"] == ["甲"]
    assert store.state["last_failure_at"] == NOW


@pytest.mark.asyncio
async def test_logs_never_contain_external_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canary = "CANARY_外部原文_8841"
    fetch  = FakeFetcher(
        {
            "/popular"   : popular_payload(bili_video(1, f"{canary}标题")),
            "/reply/main": comments_payload(bili_reply(f"{canary}评论")),
        }
    )
    digest = {"looked_at": [{"item": "i1", "reaction": f"想法{canary}", "feeling": "like"}]}
    store, ai = _store(tmp_path), FakeAI(digest, RuntimeError(canary))
    with caplog.at_level("DEBUG"):
        await run_session(store, make_cfg(), _hot(), _deps(fetch, ai))
        down = FakeFetcher({"/popular": SourceError(canary, "Timeout")})
        await run_session(store, make_cfg(), _hot(), _deps(down, ai))
        failed = FakeFetcher(
            {
                "/popular"   : popular_payload(bili_video(2, "另一个")),
                "/reply/main": comments_payload(),
            }
        )
        await run_session(store, make_cfg(), _hot(), _deps(failed, ai))
    assert caplog.records
    assert all(canary not in record.getMessage() for record in caplog.records)
