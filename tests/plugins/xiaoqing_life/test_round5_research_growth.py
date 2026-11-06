"""第五轮里程碑 2：有限步骤的自主研究、方法复用、矛盾与走向、搁置、规划器改动。"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import (
    commands,
    digest,
    lifecycle,
    methods,
    planner,
    reinforce,
    research,
    wind_down,
)
from plugins.xiaoqing_life import threads as th
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.config import parse_life_config
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.planner import THREAD, Decision
from plugins.xiaoqing_life.session import SessionDeps, _liked_count, run_session
from plugins.xiaoqing_life.sources import subtitle
from plugins.xiaoqing_life.sources.base import SourceError, SourceItem
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.plugins.xiaoqing_life.support import (
    FakeAI,
    FakeFetcher,
    at,
    comments_payload,
    make_cfg,
)

pytestmark = pytest.mark.asyncio

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()
DAY    = 86400.0


@pytest.fixture(autouse=True)
def _fresh_wbi_cache() -> None:
    subtitle.reset_cache()


# ───────────────────────────── 夹具 ─────────────────────────────


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


def _thread(**kw: Any) -> dict[str, Any]:
    return {
        "id"            : "t1",
        "title"         : "猫为什么爱纸箱",
        "topic"         : "猫",
        "why"           : "好奇",
        "next_query"    : "猫 纸箱",
        "status"        : "open",
        "steps"         : 0,
        "empty_streak"  : 0,
        "cooldown_until": 0.0,
        "notes"         : [],
        "takeaway"      : "",
        "created_at"    : NOW - DAY,
        "updated_at"    : NOW - DAY,
        **kw,
    }


def _with_thread(tmp_path: Path, **kw: Any) -> LifeStore:
    store = _store(tmp_path)
    store.threads.append(_thread(**kw))
    return store


def _continue() -> Decision:
    return Decision(THREAD, "best_score", {THREAD: 70.0}, {"thread_id": "t1", "query": "猫 纸箱"})


def _video(aid: int, title: str, desc: str = "简介") -> dict[str, Any]:
    return {"id": aid, "bvid": f"BV{aid}", "title": title, "description": desc, "play": 10}


BAIKE = {
    "title"   : "纸箱",
    "desc"    : "包装用品",
    "abstract": "纸箱是用瓦楞纸板制成的包装容器。猫很喜欢钻进去。",
}
WIKI = {
    "query": {
        "pages": {
            "5": {
                "pageid" : 5,
                "index"  : 1,
                "title"  : "纸箱",
                "extract": "紙箱是常見的包裝材料。貓喜歡鑽進紙箱。",
            }
        }
    }
}


def _fetch(*videos: dict[str, Any], **extra: Any) -> FakeFetcher:
    routes: dict[str, Any] = {
        "/search/type": {"code": 0, "data": {"result": list(videos)}},
        "/reply/main" : comments_payload(),
        "Baike"       : BAIKE,
        "wikipedia"   : WIKI,
    }
    routes.update(extra)
    return FakeFetcher(routes)


def _deps(fetch: FakeFetcher, ai: FakeAI, **kw: Any) -> SessionDeps:
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    return SessionDeps(
        fetch=fetch, complete=complete, rng=random.Random(1), now=lambda: MOMENT, **kw
    )


def _finding(text: str, quote: str, refs: list[str], revises: str = "") -> dict[str, Any]:
    return {"text": text, "quote": quote, "refs": refs, "revises": revises}


def _reply(**kw: Any) -> dict[str, Any]:
    return {"looked_at": [], "status": "progress", "next": {"action": "stop"}, **kw}


def _look(item: str = "i1", feeling: str = "like", **kw: Any) -> dict[str, Any]:
    return {"item": item, "reaction": "有意思", "feeling": feeling, "topics": ["猫"], **kw}


def _calls(fetch: FakeFetcher, fragment: str) -> list[tuple[str, dict[str, str]]]:
    return [c for c in fetch.calls if fragment in c[0]]


# ───────────────────────────── 第一轮行动 ─────────────────────────────


async def test_first_action_prefers_a_legal_saved_next_action() -> None:
    cfg = make_cfg()
    thread = _thread(next_action={"action": "lookup", "arg": "瓦楞纸"})
    assert research.first_action(cfg, default_state(), thread, NOW) == research.Next(
        "lookup", "瓦楞纸"
    )
    thread = _thread(next_action={"action": "search", "arg": "猫 窝"})
    assert research.first_action(cfg, default_state(), thread, NOW) == research.Next(
        "search", "猫 窝"
    )
    # 不合法的（deep_read 只在循环内有意义、参数为空、来源关闭）都忽略，回到偏好规则
    for bad in (
        {"action": "deep_read", "arg": "i1"},
        {"action": "search", "arg": ""},
        {"action": "bogus", "arg": "x"},
    ):
        got = research.first_action(cfg, default_state(), _thread(next_action=bad), NOW)
        assert got == research.Next("search", "猫 纸箱")
    off = make_cfg(sources={"bili_search": False})
    got = research.first_action(
        off, default_state(), _thread(next_action={"action": "search", "arg": "猫 窝"}), NOW
    )
    assert got == research.Next("lookup", "猫")


async def test_first_action_follows_the_research_action_ema() -> None:
    cfg   = make_cfg()
    state = default_state()
    assert research.first_action(cfg, state, _thread(), NOW).action == "search"  # 平手取 search
    reinforce.update_preference(state, "research_action", "search", 0.0)
    reinforce.update_preference(state, "research_action", "lookup", 1.0)
    assert research.first_action(cfg, state, _thread(), NOW) == research.Next("lookup", "猫")
    state["preferences"]["research_action"].update(search=1.0, lookup=0.2)
    assert research.first_action(cfg, state, _thread(), NOW).action == "search"


async def test_first_action_uses_lookup_when_search_is_off_or_unhealthy() -> None:
    off = make_cfg(sources={"bili_search": False})
    assert research.first_action(off, default_state(), _thread(), NOW) == research.Next(
        "lookup", "猫"
    )
    # 没有 topic 时取标题前 12 字
    plain = _thread(topic="", title="猫为什么特别特别特别爱纸箱呢")
    got = research.first_action(off, default_state(), plain, NOW)
    assert got is not None and got.arg == "猫为什么特别特别特别爱纸箱呢"[:12]
    shaky = default_state()
    reinforce.record_source(shaky, "bili_search", False, NOW)
    reinforce.record_source(shaky, "bili_search", False, NOW)
    assert research.first_action(make_cfg(), shaky, _thread(), NOW).action == "lookup"
    nothing = make_cfg(sources={"bili_search": False, "baike_lookup": False, "wiki_read": False})
    assert research.first_action(nothing, default_state(), _thread(), NOW) is None


async def test_no_source_or_missing_thread_is_a_quiet_skip(tmp_path: Path) -> None:
    cfg = make_cfg(sources={"bili_search": False, "baike_lookup": False, "wiki_read": False})
    store = _with_thread(tmp_path)
    ai    = FakeAI()
    out   = await run_session(store, cfg, _continue(), _deps(_fetch(), ai))
    assert (out.status, out.reason) == ("skipped", "no_source") and ai.calls == []
    gone = Decision(THREAD, "x", {}, {"thread_id": "nope"})
    out  = await run_session(_store(tmp_path / "b"), make_cfg(), gone, _deps(_fetch(), ai))
    assert out.reason == "thread_missing" and store.episodes == []


# ───────────────────────────── 单轮核对与提交 ─────────────────────────────


async def test_findings_become_notes_with_ids_quotes_and_episodes(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    reply = _reply(
        looked_at     = [_look()],
        findings      = [_finding("纸箱让猫有安全感", "猫咪为什么爱钻纸箱", ["i1"])],
        open_question = "为什么偏偏是纸箱",
        next          = {"action": "search", "arg": "猫 安全感"},
    )
    fetch = _fetch(_video(7, "猫咪为什么爱钻纸箱"))
    await run_session(
        store, make_cfg(research_max_rounds=1), _continue(), _deps(fetch, FakeAI(reply))
    )
    thread = store.threads[0]
    note   = thread["notes"][0]
    assert note["id"] == "n1" and note["kind"] == "finding" and note["note"] == "纸箱让猫有安全感"
    assert note["quote"] == "猫咪为什么爱钻纸箱" and note["episode_id"] == store.episodes[0]["id"]
    assert note["at"] == NOW
    assert thread["open_question"] == "为什么偏偏是纸箱"
    assert thread["next_action"] == {"action": "search", "arg": "猫 安全感"}
    assert thread["next_query"] == "猫 安全感" and thread["steps"] == 1
    assert thread["actions"][0] == {
        "round" : 1,
        "action": "search",
        "arg"   : "猫 纸箱",
        "useful": True,
        "at"    : NOW,
    }
    assert (
        store.episodes[0]["activity"] == "thread_continue"
        and store.episodes[0]["thread_id"] == "t1"
    )
    assert store.episodes[0]["depth"] in ("skim", "read")


async def test_findings_are_checked_against_the_material(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    reply = _reply(
        looked_at = [_look()],
        findings  = [
            _finding("编造的", "材料里没有的话", ["i1"]),
            _finding("越界编号", "猫咪为什么爱钻纸箱", ["i9"]),
            _finding("没有编号", "猫咪为什么爱钻纸箱", []),
            _finding("含违禁词的", "猫咪为什么爱钻纸箱", ["i1"]),
            _finding("好的一条", "猫咪为什么爱钻纸箱", ["i1"]),
            _finding("好的二条", "为什么爱钻", ["i1"]),
        ],
    )
    await run_session(
        store,
        make_cfg(research_max_rounds=1, blocked_keywords=["违禁词"]),
        _continue(),
        _deps(_fetch(_video(7, "猫咪为什么爱钻纸箱")), FakeAI(reply)),
    )
    assert [n["note"] for n in store.threads[0]["notes"]] == ["好的一条", "好的二条"]
    # 要点最多 3 条
    store = _with_thread(tmp_path / "cap")
    many  = _reply(
        looked_at = [_look()],
        findings  = [_finding(f"要点{n}", "猫咪为什么爱钻纸箱", ["i1"]) for n in range(5)],
    )
    await run_session(
        store,
        make_cfg(research_max_rounds=1),
        _continue(),
        _deps(_fetch(_video(7, "猫咪为什么爱钻纸箱")), FakeAI(many)),
    )
    assert len(store.threads[0]["notes"]) == 3


async def test_revises_marks_old_note_superseded_and_records_a_correction(tmp_path: Path) -> None:
    old = [{"episode_id": "e0", "note": "猫是怕冷才钻纸箱"}]  # 旧笔记没有 ID
    store = _with_thread(tmp_path, notes=old, steps=1)
    reply = _reply(
        looked_at = [_look()],
        findings  = [
            _finding("其实是为了安全感", "猫咪为什么爱钻纸箱", ["i1"], revises="n1"),
            _finding("这条 revises 的不存在", "为什么爱钻", ["i1"], revises="n99"),
        ],
    )
    await run_session(
        store,
        make_cfg(research_max_rounds=1),
        _continue(),
        _deps(_fetch(_video(7, "猫咪为什么爱钻纸箱")), FakeAI(reply)),
    )
    thread = store.threads[0]
    ids    = [n["id"] for n in thread["notes"]]
    assert ids == ["n1", "n2", "n3"]  # 旧笔记按顺序补了 n1
    assert thread["notes"][0]["superseded_by"] == "n2" and "superseded_by" not in thread["notes"][2]
    assert len(thread["corrections"]) == 1
    fix = thread["corrections"][0]
    assert (fix["before"], fix["after"]) == ("猫是怕冷才钻纸箱", "其实是为了安全感")
    assert fix["episode_id"] == thread["notes"][1]["episode_id"] and fix["at"] == NOW
    assert thread["events"][-1]["kind"] == "correction"


async def test_corrections_are_capped_at_four_and_superseded_notes_cannot_be_revised() -> None:
    thread = _thread(notes=[{"episode_id": "e", "note": "起点"}])
    th.ensure_note_ids(thread)
    for n in range(6):
        live = th.live_notes(thread)[-1]["id"]
        th.add_finding(
            thread, episode_id="e", text=f"想法{n}", quote="q", revises=live, now=NOW + n
        )
    assert len(thread["corrections"]) == th.MAX_CORRECTIONS
    assert thread["corrections"][-1]["after"] == "想法5"
    assert "n1" not in {n["id"] for n in th.live_notes(thread)}
    before = len(thread["corrections"])
    th.add_finding(
        thread, episode_id="e", text="x", quote="q", revises="n1", now=NOW
    )  # n1 已被取代
    assert len(thread["corrections"]) == before


async def test_old_notes_get_ids_without_touching_existing_ones() -> None:
    thread = _thread(notes=[{"id": "n4", "note": "a"}, {"note": "b"}, {"note": "c"}])
    th.ensure_note_ids(thread)
    assert [n["id"] for n in thread["notes"]] == ["n4", "n5", "n6"]
    th.ensure_note_ids(thread)
    assert [n["id"] for n in thread["notes"]] == ["n4", "n5", "n6"]


async def test_cited_items_not_in_looked_at_become_auto_episodes(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    reply = _reply(
        looked_at = [_look("i1")],
        findings  = [_finding("第二个视频里的说法", "第二个视频标题", ["i2"])],
    )
    fetch = _fetch(_video(7, "猫咪为什么爱钻纸箱"), _video(8, "第二个视频标题"))
    await run_session(
        store, make_cfg(research_max_rounds=1), _continue(), _deps(fetch, FakeAI(reply))
    )
    manual, auto = store.episodes
    assert manual["auto"] is False and auto["auto"] is True
    assert (auto["feeling"], auto["reaction"], auto["topics"]) == ("meh", "", [])
    assert store.threads[0]["notes"][0]["episode_id"] == auto["id"]
    # 自动补的经历没有兴趣证据：证据只来自 looked_at 里的 i1
    items = {e["item"] for entry in store.interests.values() for e in entry["evidence"]}
    assert items == {manual["item_id"]}


async def test_next_action_is_validated(tmp_path: Path) -> None:
    fetch = _fetch(_video(7, "猫咪为什么爱钻纸箱"))
    from plugins.xiaoqing_life.sources import bilibili as bili

    found = await bili.search(fetch, "猫", [])
    nxt   = research._next
    assert nxt({"action": "bogus", "arg": "x"}, found) == research.Next()
    assert nxt({"action": "search", "arg": "   "}, found) == research.Next()
    assert nxt({"action": "search", "arg": "猫窝" * 25}, found).arg == "猫窝" * 15
    assert nxt({"action": "lookup", "arg": "瓦楞纸" * 10}, found).arg == "瓦楞纸" * 4
    assert nxt({"action": "deep_read", "arg": "i1"}, found) == research.Next("deep_read", "bili:7")
    assert nxt({"action": "deep_read", "arg": "i2"}, found) == research.Next()
    assert nxt({"action": "deep_read", "arg": "bili:7"}, found) == research.Next()
    assert nxt("stop", found) == research.Next() and nxt(None, found) == research.Next()
    from plugins.xiaoqing_life.sources import baike

    card = await baike.read(FakeFetcher({"Baike": BAIKE}), "纸箱", [])
    assert card is not None
    assert nxt({"action": "deep_read", "arg": "i1"}, [card]) == research.Next()  # 非 B站条目


async def test_status_progress_without_findings_is_an_empty_step(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    reply = _reply(looked_at=[_look()], findings=[_finding("x", "不存在的摘录", ["i1"])])
    fetch = _fetch(_video(7, "无关视频"))
    await run_session(store, make_cfg(), _continue(), _deps(fetch, FakeAI(reply)))
    thread = store.threads[0]
    assert thread["steps"] == 0 and thread["empty_streak"] == 1 and thread["notes"] == []
    assert thread["actions"][0]["useful"] is False
    # answered 没有有效要点也不算答出
    store = _with_thread(tmp_path / "b")
    reply = _reply(looked_at=[_look()], status="answered")
    await run_session(
        store, make_cfg(), _continue(), _deps(_fetch(_video(7, "视频")), FakeAI(reply))
    )
    assert store.threads[0]["status"] == "open"


async def test_answered_ends_the_loop_and_moves_to_reviewing(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI(
        _reply(
            looked_at     = [_look()],
            findings      = [_finding("查明白了", "猫咪为什么爱钻纸箱", ["i1"])],
            status        = "answered",
            next          = {"action": "search", "arg": "还想查别的"},
            open_question = "还有什么",
        ),
        _reply(),
    )
    fetch = _fetch(_video(7, "猫咪为什么爱钻纸箱"))
    out   = await run_session(store, make_cfg(), _continue(), _deps(fetch, ai))
    assert len(ai.calls) == 1  # 答出就结束，不再进下一轮
    thread = store.threads[0]
    assert thread["status"] == "reviewing" and thread["open_question"] == ""
    assert out.details["thread_progress"] == 2  # min(1 条要点, 2) + 答出 1


# ───────────────────────────── 多轮 ─────────────────────────────


async def test_three_rounds_split_the_span_and_commit_each_round(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI(
        _reply(
            looked_at = [_look()],
            findings  = [_finding("第一条", "猫咪为什么爱钻纸箱", ["i1"])],
            next      = {"action": "lookup", "arg": "纸箱"},
        ),
        _reply(
            looked_at = [_look("i1", "meh")],
            findings  = [_finding("第二条", "包装容器", ["p1.1"])],
            next      = {"action": "search", "arg": "猫 窝"},
        ),
        _reply(
            looked_at = [_look()],
            findings  = [_finding("第三条", "钻纸箱的猫", ["i1"])],
            next      = {"action": "search", "arg": "还有"},
        ),
    )
    fetch = FakeFetcher(
        {
            "/search/type": lambda p: {
                "code": 0,
                "data": {
                    "result": [
                        _video(7, "猫咪为什么爱钻纸箱")
                        if p["keyword"] == "猫 纸箱"
                        else _video(9, "钻纸箱的猫")
                    ]
                },
            },
            "/reply/main": comments_payload(),
            "Baike"      : BAIKE,
            "wikipedia"  : WIKI,
        }
    )
    out = await run_session(store, make_cfg(), _continue(), _deps(fetch, ai))
    assert len(ai.calls) == 3 and out.details["rounds"] == 3
    thread = store.threads[0]
    assert [a["action"] for a in thread["actions"]] == ["search", "lookup", "search"]
    assert [a["round"] for a in thread["actions"]] == [1, 2, 3]
    assert all(a["useful"] for a in thread["actions"])
    assert [n["id"] for n in thread["notes"]] == ["n1", "n2", "n3"]
    # 一次研究只算一步：三轮都有收获也不会一次就查到上限
    assert thread["steps"] == 1 and thread["status"] == "open"
    # 名义时段按轮数均分，每轮的经历落在自己那一段里
    start, end = store.state["last_session_at"], store.state["last_session_end"]
    seg = (end - start) / 3
    assert 20 * 60 <= end - start <= 40 * 60
    for n, episode in enumerate(store.episodes):
        assert start + seg * n < episode["at"] < start + seg * (n + 1)
    # 第二轮的材料是百科 / 维基，经历来源相应
    assert [e["source"] for e in store.episodes][1] in ("baike_read", "wiki_read")
    # 研究行动偏好按 useful 更新；计 3 次研究提示里有轮次信息
    prefs = store.state["preferences"]["research_action"]
    assert prefs["search"] == 1.0 and prefs["lookup"] == 1.0
    second = json.loads(ai.calls[1][1][1]["content"])
    assert second["round"] == 2 and second["rounds_left"] == 1
    assert second["own_notes"][0]["id"] == "n1" and second["question"]["title"] == "猫为什么爱纸箱"
    assert {i["id"] for i in second["untrusted_material"]["items"]} >= {"i1"}
    assert out.details["thread_progress"] == 2


async def test_rounds_are_capped_by_config(tmp_path: Path) -> None:
    ai = FakeAI(*[_reply(next={"action": "search", "arg": "再搜"}) for _ in range(5)])
    for rounds in (1, 2):
        store = _with_thread(tmp_path / str(rounds))
        ai = FakeAI(*[_reply(next={"action": "search", "arg": "再搜"}) for _ in range(5)])
        fetch                        = _fetch(_video(7 + rounds, f"视频{rounds}"))
        fetch.routes["/search/type"] = lambda p: {
            "code": 0,
            "data": {
                "result": [_video(100 + len(p["keyword"]) + int(random.random() * 1e6), "新视频")]
            },
        }
        await run_session(
            store, make_cfg(research_max_rounds=rounds), _continue(), _deps(fetch, ai)
        )
        assert len(ai.calls) == rounds
    assert parse_life_config({"research_max_rounds": 9}).research_max_rounds == 5
    assert parse_life_config({"research_max_rounds": 0}).research_max_rounds == 1
    assert make_cfg().research_max_rounds == 3 and make_cfg().park_days == 3
    assert parse_life_config({"park_days": 99}).park_days == 30
    assert make_cfg().session_minutes["thread_continue"] == (20, 40)


async def test_deep_read_action_reads_the_chosen_video_deeper(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI(
        _reply(looked_at=[_look()], next={"action": "deep_read", "arg": "i1"}),
        _reply(
            findings = [_finding("简介里写了原因", "完整简介里说明了原因", ["p1.1"])],
            next     = {"action": "stop"},
        ),
    )
    page                        = comments_payload()
    page["data"]["top_replies"] = [{"content": {"message": "UP主置顶补充说明"}}]
    fetch                       = _fetch(
        _video(7, "猫咪为什么爱钻纸箱"),
        **{
            "/web-interface/view": {
                "code": 0,
                "data": {"desc": "完整简介里说明了原因。", "owner": {"name": "up"}, "cid": 701},
            },
            "/reply/main": page,
        },
    )
    await run_session(store, make_cfg(), _continue(), _deps(fetch, ai))
    second = json.loads(ai.calls[1][1][1]["content"])["untrusted_material"]["items"]
    assert len(second) == 1 and second[0]["id"] == "i1"
    kinds = {p["kind"] for p in second[0]["passages"]}
    assert {"desc", "pinned"} <= kinds
    assert store.threads[0]["notes"][0]["note"] == "简介里写了原因"
    assert [a["action"] for a in store.threads[0]["actions"]] == ["search", "deep_read"]
    # 登录时深读含字幕：subtitles_ok 为真时才请求 player 接口
    assert _calls(fetch, "player/wbi") == []
    store2 = _with_thread(tmp_path / "b")
    ai2    = FakeAI(
        _reply(looked_at=[_look()], next={"action": "deep_read", "arg": "i1"}),
        _reply(),
    )
    routes = {
        "/web-interface/nav": {
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                }
            },
        },
        "/player/wbi/v2": {
            "code": 0,
            "data": {
                "subtitle": {
                    "subtitles": [{"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/x.json"}]
                }
            },
        },
        "aisubtitle.hdslb.com": {"body": [{"from": 1, "content": "字幕内容"}]},
    }
    fetch2 = _fetch(
        _video(7, "猫咪为什么爱钻纸箱"),
        **{
            "/web-interface/view": {"code": 0, "data": {"desc": "简介", "cid": 701}},
            **routes,
        },
    )
    await run_session(
        store2, make_cfg(), _continue(), _deps(fetch2, ai2, subtitles_ok=lambda: True)
    )
    assert len(_calls(fetch2, "player/wbi")) == 2  # 第一轮预选的深读 + 第二轮的 deep_read
    material = json.loads(ai2.calls[1][1][1]["content"])["untrusted_material"]["items"]
    assert any(p["kind"] == "subtitle" for p in material[0]["passages"])
    assert store2.episodes[-1]["depth"] == "watched"


async def test_lookup_reads_baike_and_wiki_with_passage_refs(tmp_path: Path) -> None:
    store = _with_thread(tmp_path, next_action={"action": "lookup", "arg": "纸箱"})
    ai = FakeAI(
        _reply(
            looked_at = [_look("i1"), _look("i2", "like")],
            findings  = [_finding("纸箱是瓦楞纸做的", "用瓦楞纸板制成", ["p1.1"])],
        )
    )
    fetch = _fetch()
    await run_session(store, make_cfg(), _continue(), _deps(fetch, ai))
    sent = json.loads(ai.calls[0][1][1]["content"])["untrusted_material"]["items"]
    assert [i["source"] for i in sent] == ["baike_read", "wiki_read"]
    assert sent[0]["passages"][0]["id"] == "p1.1" and sent[1]["passages"][0]["id"] == "p2.1"
    assert "紙箱" in sent[1]["passages"][0]["text"]  # 维基繁体原字
    assert [e["source"] for e in store.episodes] == ["baike_read", "wiki_read"]
    assert all(e["depth"] == "read" for e in store.episodes)
    assert _calls(fetch, "search/type") == []
    # wiki_read 关闭：只有百科
    store2 = _with_thread(tmp_path / "b", next_action={"action": "lookup", "arg": "纸箱"})
    ai2 = FakeAI(_reply())
    await run_session(
        store2, make_cfg(sources={"wiki_read": False}), _continue(), _deps(_fetch(), ai2)
    )
    only = json.loads(ai2.calls[0][1][1]["content"])["untrusted_material"]["items"]
    assert [i["source"] for i in only] == ["baike_read"]


async def test_lookup_items_are_capped_and_blocked_text_drops_the_item(tmp_path: Path) -> None:
    store = _with_thread(tmp_path, next_action={"action": "lookup", "arg": "纸箱"})
    ai = FakeAI(_reply())
    await run_session(
        store, make_cfg(blocked_keywords=["包装容器"]), _continue(), _deps(_fetch(), ai)
    )
    items = json.loads(ai.calls[0][1][1]["content"])["untrusted_material"]["items"]
    assert [i["source"] for i in items] == ["wiki_read"]  # 百科那条含屏蔽词，整条丢弃
    many = {
        "query": {
            "pages": {
                str(n): {"pageid": n, "index": n, "title": f"纸箱{n}", "extract": f"介绍{n}。"}
                for n in range(1, 6)
            }
        }
    }
    store = _with_thread(tmp_path / "b", next_action={"action": "lookup", "arg": "纸箱"})
    ai2 = FakeAI(_reply())
    await run_session(store, make_cfg(), _continue(), _deps(_fetch(wikipedia=many), ai2))
    assert len(json.loads(ai2.calls[0][1][1]["content"])["untrusted_material"]["items"]) == 3


async def test_research_collects_no_memes(tmp_path: Path) -> None:
    store  = _with_thread(tmp_path)
    called = []

    async def download(url: str, limit: int) -> None:
        called.append(url)

    async def vision(messages: list[dict[str, Any]]) -> str:
        called.append("vision")
        return "{}"

    picture = {
        "content": {"message": "表情", "pictures": [{"img_src": "https://i0.hdslb.com/a.png"}]},
        "like"   : 1,
        "replies": [],
    }
    fetch = _fetch(_video(7, "猫咪为什么爱钻纸箱"), **{"/reply/main": comments_payload(picture)})
    reply = _reply(looked_at=[_look()], meme_pictures=[{"ref": "c1.1#0", "why": "像表情"}])
    await run_session(
        store,
        make_cfg(research_max_rounds=1),
        _continue(),
        _deps(fetch, FakeAI(reply), download=download, vision=vision),
    )
    assert called == [] and store.memes == {}


# ───────────────────────────── 失败与取消 ─────────────────────────────


async def test_first_round_without_material_follows_the_failure_path(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI()
    fail  = _fetch(**{"/search/type": SourceError("x", "code_412")})
    out   = await run_session(
        store,
        make_cfg(sources={"baike_lookup": False, "wiki_read": False}),
        _continue(),
        _deps(fail, ai),
    )
    assert out.status == "failed" and out.reason == "all_sources_failed" and ai.calls == []
    assert store.state["last_failure_at"] > 0 and store.episodes == []
    assert store.threads[0]["actions"][0]["useful"] is False
    assert store.state["preferences"]["research_action"]["search"] == -0.5


async def test_all_seen_first_round_records_an_empty_step(tmp_path: Path) -> None:
    store                = _with_thread(tmp_path)
    store.seen["bili:7"] = NOW - 10
    ai                   = FakeAI()
    fetch                = _fetch(_video(7, "旧视频"))
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
    assert store.threads[0]["cooldown_until"] == NOW + 180 * 60


async def test_later_round_without_material_just_ends_early(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI(
        _reply(
            looked_at = [_look()],
            findings  = [_finding("第一条", "猫咪为什么爱钻纸箱", ["i1"])],
            next      = {"action": "deep_read", "arg": "i1"},
        )
    )
    fetch = _fetch(
        _video(7, "猫咪为什么爱钻纸箱"), **{"/web-interface/view": SourceError("x", "T")}
    )
    fetch.routes["/reply/main"] = SourceError("x", "T")
    out = await run_session(store, make_cfg(), _continue(), _deps(fetch, ai))
    # deep_read 没读到新内容也不是失败；第二轮仍然用了一次模型，空回复被当成没有收获
    assert out.status == "ok" and store.threads[0]["steps"] == 1
    # 不存在于本轮材料的条目：行动取不到材料，提前结束
    store = _with_thread(tmp_path / "b")
    ai = FakeAI(_reply(looked_at=[_look()], next={"action": "lookup", "arg": "纸箱"}))
    fetch = _fetch(_video(7, "视频"), Baike=SourceError("b", "T"), wikipedia=SourceError("w", "T"))
    out = await run_session(store, make_cfg(), _continue(), _deps(fetch, ai))
    assert out.status == "ok" and len(ai.calls) == 1
    assert [a["action"] for a in store.threads[0]["actions"]] == ["search", "lookup"]
    assert store.threads[0]["actions"][1]["useful"] is False


async def test_budget_exhausted_stops_the_loop_but_keeps_committed_rounds(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI(
        _reply(
            looked_at = [_look()],
            findings  = [_finding("第一条", "猫咪为什么爱钻纸箱", ["i1"])],
            next      = {"action": "search", "arg": "接着"},
        ),
        _reply(),
    )
    fetch = _fetch(_video(7, "猫咪为什么爱钻纸箱"))
    out = await run_session(store, make_cfg(max_llm_calls_per_day=1), _continue(), _deps(fetch, ai))
    assert len(ai.calls) == 1 and store.state["llm_calls_today"] == 1
    assert out.status == "ok" and len(store.episodes) == 1 and store.threads[0]["steps"] == 1
    # 第一轮就没有预算：跳过，不算失败
    store = _with_thread(tmp_path / "b")
    out   = await run_session(
        store, make_cfg(max_llm_calls_per_day=0), _continue(), _deps(fetch, FakeAI())
    )
    assert out.status == "skipped" and out.reason == "llm_exhausted"
    assert "last_failure_at" not in store.state or store.state["last_failure_at"] == 0.0


async def test_model_failure_and_unparseable_output_in_round_one(tmp_path: Path) -> None:
    for reply, reason in (
        (RuntimeError("boom"), "model_failed"),
        ("不是 JSON", "digest_unparseable"),
    ):
        store = _with_thread(tmp_path / reason)
        out   = await run_session(
            store, make_cfg(), _continue(), _deps(_fetch(_video(7, "视频")), FakeAI(reply))
        )
        assert (out.status, out.reason) == ("failed", reason) and store.episodes == []
    store = _with_thread(tmp_path / "x")
    off = _deps(_fetch(_video(7, "视频")), FakeAI(), reasoning_ok=False)
    assert (await run_session(store, make_cfg(), _continue(), off)).reason == "model_not_configured"


async def test_cancellation_keeps_rounds_that_were_already_committed(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    calls = []

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        calls.append(1)
        if len(calls) == 1:
            return json.dumps(
                _reply(
                    looked_at = [_look()],
                    findings  = [_finding("第一条", "猫咪为什么爱钻纸箱", ["i1"])],
                    next      = {"action": "lookup", "arg": "纸箱"},
                )
            )
        raise asyncio.CancelledError

    deps = SessionDeps(
        fetch    = _fetch(_video(7, "猫咪为什么爱钻纸箱")),
        complete = complete,
        rng      = random.Random(1),
        now      = lambda: MOMENT,
    )
    with pytest.raises(asyncio.CancelledError):
        await run_session(store, make_cfg(), _continue(), deps)
    assert len(store.episodes) == 1 and store.threads[0]["notes"][0]["note"] == "第一条"
    assert store.threads[0]["steps"] == 1


# ───────────────────────────── 搁置 ─────────────────────────────


async def test_stuck_parks_the_thread_and_ends_the_loop(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI(
        _reply(
            looked_at     = [_look()],
            status        = "stuck",
            stuck_reason  = "资料不够新",
            open_question = "到底是不是怕冷",
            next          = {"action": "search", "arg": "猫 怕冷"},
        ),
        _reply(),
    )
    out    = await run_session(store, make_cfg(), _continue(), _deps(_fetch(_video(7, "视频")), ai))
    thread = store.threads[0]
    assert len(ai.calls) == 1 and out.status == "ok"
    assert thread["status"] == "parked" and thread["park_reason"] == "资料不够新"
    assert thread["resume_hint"] == "到底是不是怕冷" and thread["park_count"] == 1
    assert thread["parked_until"] == NOW + 3 * DAY and thread["events"][-1]["kind"] == "parked"
    assert th.active_threads(store.threads) == []


async def test_stuck_without_reason_uses_the_default_and_hint_falls_back_to_next(
    tmp_path: Path,
) -> None:
    store = _with_thread(tmp_path)
    reply = _reply(looked_at=[_look()], status="stuck", next={"action": "search", "arg": "换个词"})
    await run_session(
        store, make_cfg(park_days=5), _continue(), _deps(_fetch(_video(7, "视频")), FakeAI(reply))
    )
    thread = store.threads[0]
    assert thread["park_reason"] == "资料不太够" and thread["resume_hint"] == "换个词"
    assert thread["parked_until"] == NOW + 5 * DAY


async def test_third_stuck_drops_the_thread_with_the_park_reason(tmp_path: Path) -> None:
    store = _with_thread(tmp_path, park_count=2)
    reply = _reply(looked_at=[_look()], status="stuck", stuck_reason="还是不够")
    await run_session(
        store, make_cfg(), _continue(), _deps(_fetch(_video(7, "视频")), FakeAI(reply))
    )
    thread = store.threads[0]
    assert thread["status"] == "dropped" and thread["drop_reason"] == "还是不够"


async def test_parked_thread_resumes_only_after_parked_until_with_a_penalty() -> None:
    cfg = make_cfg(choice_temperature=0.5)

    def scores(thread: dict[str, Any], now: float = NOW) -> dict[str, float]:
        moment = MOMENT.replace() if now == NOW else at(14, 0, day=9)
        return planner.choose(cfg, default_state(), [thread], {}, moment, random.Random(1)).scores

    open_score = scores(_thread())[THREAD]
    parked = _thread(status="parked", parked_until=NOW + 60, parked_at=NOW - DAY)
    assert THREAD not in scores(parked)  # 还没到时间
    ready = _thread(status="parked", parked_until=NOW - 60, parked_at=NOW - DAY)
    assert scores(ready)[THREAD] == pytest.approx(open_score - 8.0)
    # 到时间但步数已满的搁置课题仍可续做；冷却中的不行
    assert THREAD in scores(_thread(status="parked", parked_until=NOW - 1, steps=3))
    assert THREAD not in scores(
        _thread(status="parked", parked_until=NOW - 1, cooldown_until=NOW + 5)
    )


async def test_selected_parked_thread_goes_back_to_open(tmp_path: Path) -> None:
    store = _with_thread(
        tmp_path,
        status       = "parked",
        parked_until = NOW - 60,
        parked_at    = NOW - DAY,
        park_count   = 1,
        park_reason  = "资料不太够",
    )
    reply = _reply(
        looked_at=[_look()], findings=[_finding("有新发现", "猫咪为什么爱钻纸箱", ["i1"])]
    )
    await run_session(
        store,
        make_cfg(),
        _continue(),
        _deps(_fetch(_video(7, "猫咪为什么爱钻纸箱")), FakeAI(reply)),
    )
    thread = store.threads[0]
    assert (
        thread["status"] == "open" and thread["parked_until"] == 0.0 and thread["park_count"] == 1
    )


async def test_parked_threads_do_not_count_for_open_slots_but_are_capped_at_two() -> None:
    cfg = make_cfg(max_open_threads=1)
    threads = [_thread(id="p1", status="parked", parked_at=NOW - 3 * DAY, title="老的")]
    opened = th.open_thread(threads, cfg, title="新问题", why="", next_query="", now=NOW)
    assert opened is not None  # 搁置的不占名额
    third = [
        _thread(id=f"p{n}", status="parked", parked_at=NOW - (5 - n) * DAY, title=f"课题{n}")
        for n in range(3)
    ]
    fourth = _thread(id="s", title="卡住的")
    third.append(fourth)
    th.park(third, fourth, make_cfg(), reason="不够", hint="", now=NOW)
    parked = [t for t in third if t["status"] == "parked"]
    assert len(parked) == 2
    gone = [t for t in third if t["status"] == "dropped"]
    assert {t["id"] for t in gone} == {"p0", "p1"} or len(gone) == 2
    assert min(parked, key=lambda t: t["parked_at"])["parked_at"] >= NOW - 3 * DAY
    assert all(t["drop_reason"] for t in gone)


async def test_parked_for_thirty_days_is_dropped_and_parked_notes_stay_protected() -> None:
    old = _thread(id="a", status="parked", parked_at=NOW - 31 * DAY, notes=[{"episode_id": "e1"}])
    fresh = _thread(id="b", status="parked", parked_at=NOW - 5 * DAY, notes=[{"episode_id": "e2"}])
    threads = [old, fresh]
    assert th.protected_episode_ids(threads) == {"e1", "e2"}
    th.prune_threads(threads, NOW)
    assert old["status"] == "dropped" and old["drop_reason"] == "放太久了"
    assert fresh["status"] == "parked"
    assert th.protected_episode_ids(threads) == {"e2"}
    assert any(t["id"] == "b" for t in threads)


async def test_wind_down_may_drop_a_parked_thread_with_a_reason(tmp_path: Path) -> None:
    store = _with_thread(tmp_path, status="parked", parked_at=NOW - DAY, park_reason="资料不太够")
    store.episodes.append(
        {
            "id"      : "e1",
            "at"      : NOW - 600,
            "day"     : "2026-11-05",
            "title"   : "视频",
            "reaction": "r",
            "feeling" : "meh",
            "gist"    : "",
            "topics"  : [],
        }
    )
    reply = {
        "diary"  : {"date": "2026-11-05", "text": "今天记一下。", "episode_ids": ["e1"]},
        "threads": [{"id": "t1", "action": "drop", "reason": "没兴趣了" + "字" * 40}],
    }
    ai = FakeAI(reply)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    result = await wind_down.run_wind_down(
        store, make_cfg(), at(23, 0), complete, persona="p", moment_line="", reasoning_ok=True
    )
    assert result.dropped == 1
    thread = store.threads[0]
    assert thread["status"] == "dropped" and 0 < len(thread["drop_reason"]) <= 30
    sent = json.loads(ai.calls[0][1][1]["content"])["untrusted_record"]["threads"]
    assert sent[0]["status"] == "parked"


# ───────────────────────────── 聊天侧小课题行 ─────────────────────────────


def _chat(store: LifeStore, text: str, recent: list[str] | None = None) -> str:
    payload = {
        "version"   : 1, "mode": "reply", "text": text, "recent": recent or [],
        "bot_recent": [], "unknown_terms": [],
    }  # fmt: skip
    got = tc.turn_context(store, make_cfg(), payload, MOMENT, random.Random(0))[0]
    return str(got["life_lines"]) if got else ""


async def test_chat_thread_lines_for_open_parked_and_dropped(tmp_path: Path) -> None:
    store = _with_thread(
        tmp_path,
        notes=[
            {"id": "n1", "note": "旧想法", "episode_id": "e", "superseded_by": "n2"},
            {"id": "n2", "note": "新想法", "episode_id": "e"},
        ],
        open_question="为什么是纸箱",
    )
    text = _chat(store, "你最近在查猫为什么爱纸箱吗")
    assert (
        "你在查的小事：猫为什么爱纸箱（还没查完；上次查到：新想法；还没弄懂：为什么是纸箱）" in text
    )
    assert "旧想法" not in text
    store.threads[0].update(open_question="")
    assert "还没查完；上次查到：新想法）" in _chat(store, "猫为什么爱纸箱")
    store.threads[0].update(
        notes=[], status="parked", park_reason="资料不太够", resume_hint="对照两个说法"
    )
    assert (
        "你查过一阵：猫为什么爱纸箱，查到一半资料不太够，先放着了（下次想从对照两个说法接着看）"
        in _chat(store, "猫为什么爱纸箱")
    )
    store.threads[0]["resume_hint"] = ""
    assert "先放着了" in _chat(store, "猫为什么爱纸箱") and "下次想从" not in _chat(
        store, "猫为什么爱纸箱"
    )
    store.threads[0]["status"] = "dropped"
    assert "猫为什么爱纸箱" not in _chat(store, "猫为什么爱纸箱")


# ───────────────────────────── 方法 ─────────────────────────────


def _method(**kw: Any) -> dict[str, Any]:
    return {
        "id"        : "m1", "text": "先找较早的原始出处", "applies_to": "梗的出处", "from_thread": "t0",
        "created_at": NOW - DAY, "uses": 0, "wins": 0, "last_used": 0.0, "status": "active", **kw,
    }  # fmt: skip


async def test_method_selection_needs_overlap_or_wins_and_caps_at_two() -> None:
    thread = _thread(title="哈基米的出处", topic="梗")
    pool = [
        _method(id="a", text="先找较早的原始出处", applies_to="梗的出处"),
        _method(id="b", text="对照两个说法", applies_to="天文", wins=2),
        _method(id="c", text="完全无关的做法", applies_to="烹饪"),
        _method(id="d", text="查梗的出处时看转载", applies_to="梗", wins=0),
        _method(id="e", text="旧方法", applies_to="梗的出处", status="retired"),
    ]
    picked = methods.select(pool, thread)
    assert len(picked) == 2 and {m["id"] for m in picked} <= {"a", "b", "d"}
    assert "c" not in {m["id"] for m in picked} and "e" not in {m["id"] for m in picked}
    assert methods.select([pool[2]], thread) == []
    assert [m["id"] for m in methods.select([pool[1]], thread)] == ["b"]  # wins ≥ 1 也给


async def test_method_use_is_recorded_once_per_thread_and_shown_in_the_prompt(
    tmp_path: Path,
) -> None:
    store = _with_thread(tmp_path, title="梗的出处查询", topic="梗")
    store.methods.append(_method())
    ai    = FakeAI(_reply(), _reply())
    fetch = _fetch(_video(7, "视频"), _video(8, "视频2"))
    await run_session(store, make_cfg(research_max_rounds=1), _continue(), _deps(fetch, ai))
    first = json.loads(ai.calls[0][1][1]["content"])
    assert first["methods"] == ["先找较早的原始出处"]
    assert store.methods[0]["uses"] == 1 and store.methods[0]["last_used"] == NOW
    assert store.threads[0]["methods_used"] == ["m1"]
    store.seen.clear()
    await run_session(
        store, make_cfg(research_max_rounds=1), _continue(), _deps(fetch, FakeAI(_reply()))
    )
    assert store.methods[0]["uses"] == 1  # 同一个小课题只记一次


async def test_done_thread_gives_wins_and_poor_methods_retire() -> None:
    pool = [_method(id="m1", uses=1, wins=0), _method(id="m2", uses=4, wins=0), _method(id="m3")]
    thread = _thread(methods_used=["m1", "m2"])
    methods.on_thread_done(pool, thread)
    assert [m["wins"] for m in pool] == [1, 1, 0]
    assert pool[0]["status"] == "active"  # uses 不足 3
    assert pool[1]["status"] == "retired"  # 4 次用、1 次赢 < 1/3
    ok = [_method(uses=3, wins=1)]
    methods.on_thread_done(ok, _thread(methods_used=["m1"]))
    assert ok[0]["wins"] == 2 and ok[0]["status"] == "active"


def _actions() -> list[dict[str, Any]]:
    return [
        {"round": 1, "action": "search", "arg": "梗 出处", "useful": False, "at": NOW},
        {"round": 2, "action": "lookup", "arg": "梗", "useful": True, "at": NOW},
    ]


def _extract(pool: list[dict[str, Any]], raw: Any, state: dict[str, Any] | None = None, **kw: Any):
    thread = _thread(status="done", actions=_actions())
    return methods.apply_extracted(
        pool,
        raw,
        kw.get("finished", [thread]),
        (),
        "2026-11-05",
        state if state is not None else {},
        NOW,
    )


async def test_method_extraction_validates_action_indices_and_thread() -> None:
    row = {"thread_id": "t1", "text": "先查百科再搜", "applies_to": "梗", "actions": [0, 1]}
    pool: list[dict[str, Any]] = []
    assert _extract(pool, [row]) is True
    assert pool[0]["status"] == "active" and pool[0]["from_thread"] == "t1" and pool[0]["uses"] == 0
    for bad in (
        {**row, "actions": [5]},  # 下标不存在
        {**row, "actions": [0]},  # 没有 useful 的
        {**row, "actions": []},
        {**row, "actions": ["0"]},
        {**row, "thread_id": "t9"},  # 不是今天 done 的
        {**row, "text": ""},
    ):
        empty: list[dict[str, Any]] = []
        assert _extract(empty, [bad]) is False and empty == []
    blocked: list[dict[str, Any]] = []
    thread = _thread(status="done", actions=_actions())
    assert not methods.apply_extracted(blocked, [row], [thread], ("百科",), "d", {}, NOW)
    assert methods.apply_extracted([], [row], [], (), "d", {}, NOW) is False


async def test_method_extraction_is_once_a_day_and_similar_gives_wins() -> None:
    row = {"thread_id": "t1", "text": "先找较早的原始出处", "applies_to": "梗", "actions": [1]}
    pool = [_method()]
    state: dict[str, Any] = {}
    assert _extract(pool, [row], state) is True
    assert len(pool) == 1 and pool[0]["wins"] == 1  # 相似：只加 wins，不新建
    new = {**row, "text": "完全不同的另一个做法呢"}
    assert _extract(pool, [new], state) is False  # 全天最多 1 条
    assert _extract(pool, [new], {}) is True and len(pool) == 2


async def test_method_pool_is_capped_at_thirty() -> None:
    pool = [_method(id=f"m{n}", text=f"方法{n}号做法", created_at=float(n)) for n in range(30)]
    pool[3]["status"] = "retired"
    row               = {"thread_id": "t1", "text": "全新的做法内容", "applies_to": "梗", "actions": [1]}
    assert _extract(pool, [row]) is True
    assert len(pool) == 30 and "m3" not in {m["id"] for m in pool}


async def test_wind_down_extracts_a_method_and_exposes_actions(tmp_path: Path) -> None:
    store = _with_thread(
        tmp_path,
        status       = "reviewing",
        actions      = _actions(),
        notes        = [{"episode_id": "e1", "note": "n"}],
        methods_used = ["m1"],
    )
    store.methods.append(_method(uses=1))
    store.episodes.append(
        {
            "id"      : "e1",
            "at"      : NOW - 600,
            "day"     : "2026-11-05",
            "title"   : "视频",
            "reaction": "r",
            "feeling" : "like",
            "gist"    : "",
            "topics"  : [],
        }
    )
    reply = {
        "diary"  : {"date": "2026-11-05", "text": "今天查完了。", "episode_ids": ["e1"]},
        "threads": [
            {"id": "t1", "action": "done", "takeaway": "纸箱让猫安心", "episode_ids": ["e1"]}
        ],
        "methods": [
            {"thread_id": "t1", "text": "先查百科再搜视频", "applies_to": "猫", "actions": [1]}
        ],
    }
    ai = FakeAI(reply)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    await wind_down.run_wind_down(
        store, make_cfg(), at(23, 0), complete, persona="p", moment_line="", reasoning_ok=True
    )
    record = json.loads(ai.calls[0][1][1]["content"])["untrusted_record"]["threads"][0]
    assert record["actions"][1] == {"index": 1, "action": "lookup", "arg": "梗", "useful": True}
    assert store.threads[0]["status"] == "done"
    assert len(store.methods) == 2 and store.methods[0]["wins"] == 1  # 用过的方法赢了一次
    assert (
        store.methods[1]["text"] == "先查百科再搜视频" and store.state["method_day"] == "2026-11-05"
    )


async def test_chat_gives_a_method_when_asked_how_she_found_out(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.methods += [_method(wins=2), _method(id="m2", text="没帮上忙的", wins=0)]
    assert "你自己摸索出来的查法：先找较早的原始出处" in _chat(store, "你怎么知道的？")
    assert "没帮上忙的" not in _chat(store, "你怎么知道的？")
    assert "查法" not in _chat(store, "今天天气怎么样")
    store.methods[0]["wins"] = 0
    assert "查法" not in _chat(store, "你怎么知道的？")


# ───────────────────────────── 矛盾（mixed）与走向 ─────────────────────────────


async def test_mixed_feeling_needs_both_parts_else_meh() -> None:
    from plugins.xiaoqing_life.sources.base import SourceItem

    items = [SourceItem("bili:1", "bili_popular", "新游戏评测", "简介", "u", 1, ())]
    raw   = {
        "looked_at": [
            {
                "item"         : "i1",
                "reaction"     : "又爱又气",
                "feeling"      : "mixed",
                "liked_part"   : "手感",
                "disliked_part": "氪金",
            }
        ]
    }
    got = digest.validate(raw, items, max_memes=0).looked_at[0]
    assert (got.feeling, got.liked_part, got.disliked_part) == ("mixed", "手感", "氪金")
    for lacking in ({"liked_part": "手感"}, {"disliked_part": "氪金"}, {}):
        row = {"item": "i1", "reaction": "又爱又气", "feeling": "mixed", **lacking}
        down = digest.validate({"looked_at": [row]}, items, max_memes=0).looked_at[0]
        assert (down.feeling, down.liked_part, down.disliked_part) == ("meh", "", "")
    long = {
        "item"         : "i1",
        "reaction"     : "r",
        "feeling"      : "mixed",
        "liked_part"   : "很" * 30,
        "disliked_part": "烦",
    }
    assert (
        len(digest.validate({"looked_at": [long]}, items, max_memes=0).looked_at[0].liked_part)
        <= 12
    )


async def test_mixed_episode_keeps_aspects_and_gives_half_evidence_and_half_a_like(
    tmp_path: Path,
) -> None:
    store = _with_thread(tmp_path)
    mixed = _look(feeling="mixed", liked_part="手感", disliked_part="氪金", topics=["独立游戏"])
    reply = _reply(looked_at=[mixed])
    out = await run_session(
        store,
        make_cfg(research_max_rounds=1),
        _continue(),
        _deps(_fetch(_video(7, "游戏评测")), FakeAI(reply)),
    )
    episode = store.episodes[0]
    assert episode["feeling"] == "mixed" and episode["aspects"] == {
        "like"   : "手感",
        "dislike": "氪金",
    }
    # 研究里的 mixed 多半在说资料好不好用：经历留着两个侧面，兴趣证据只是普通的 +0.5
    evidence = store.interests["独立游戏"]["evidence"][0]
    assert evidence["delta"] == 0.5 and "aspect_like" not in evidence and not evidence.get("mixed")
    assert out.details["liked"] == 0.5
    # 浏览里的 mixed：证据带两个侧面，供兴趣走向用
    row = {**episode, "id": "e-browse", "item_id": "bili:8"}
    from plugins.xiaoqing_life.session import _learn

    _learn(store, digest.DigestResult(), [row], NOW)
    browse = store.interests["独立游戏"]["evidence"][-1]
    assert browse["mixed"] and (browse["aspect_like"], browse["aspect_dislike"]) == ("手感", "氪金")
    assert _liked_count([{"feeling": "like"}, {"feeling": "mixed"}, {"feeling": "meh"}]) == 1.5
    assert (
        reinforce.session_reward(
            liked=0.5, disliked=0, new_known=0, new_expressions=0, thread_progress=0, memes=0
        )
        == 0.5
    )


def _entry(*rows: tuple[str, float, float, bool]) -> dict[str, Any]:
    """证据：(条目, 几天前, delta, mixed)。"""

    evidence = []
    for item, ago, delta, mixed in rows:
        record: dict[str, Any] = {
            "episode": "e",
            "item"   : item,
            "at"     : NOW - ago * DAY,
            "delta"  : delta,
        }
        if mixed:
            record.update(mixed=True, aspect_like="手感", aspect_dislike="氪金")
        evidence.append(record)
    return {"base": 0.0, "evidence": evidence, "updated_at": NOW, "stage": "none"}


async def test_tone_mixed_rules() -> None:
    two_mixed = _entry(("a", 3, 0.5, True), ("b", 10, 0.5, True))
    assert lifecycle.interest_tone(two_mixed, NOW) == "mixed"
    old_mixed = _entry(("a", 3, 0.5, True), ("b", 22, 0.5, True))  # 第二条超出 21 天
    assert lifecycle.interest_tone(old_mixed, NOW) == ""
    one_each = _entry(("a", 3, 1, False), ("b", 5, -1, False))  # 合计 2 < 3
    assert lifecycle.interest_tone(one_each, NOW) == ""
    plus_one = _entry(("a", 3, 1, False), ("b", 5, -1, False), ("c", 6, 0.5, True))
    assert lifecycle.interest_tone(plus_one, NOW) == "mixed"
    same_item = _entry(("a", 3, 1, False), ("a", 4, 1, False), ("b", 5, -1, False))
    assert lifecycle.interest_tone(same_item, NOW) == ""  # 按不同条目数


async def test_tone_cooling_and_warming_rules() -> None:
    cooling = _entry(("a", 20, 1, False), ("b", 25, 1, False), ("c", 3, -1, False))
    assert lifecycle.interest_tone(cooling, NOW) == "cooling"
    with_mixed = _entry(("a", 20, 1, False), ("b", 25, 1, False), ("c", 3, 0.5, True))
    assert lifecycle.interest_tone(with_mixed, NOW) == "cooling"
    silent = _entry(("a", 20, 1, False), ("b", 25, 1, False))  # 这一期完全没证据：不算变化
    assert lifecycle.interest_tone(silent, NOW) == ""
    still_liked = _entry(
        ("a", 20, 1, False), ("b", 25, 1, False), ("c", 3, 1, False), ("d", 4, -1, False)
    )
    assert lifecycle.interest_tone(still_liked, NOW) != "cooling"
    one_before = _entry(("a", 20, 1, False), ("b", 25, -1, False), ("c", 3, -1, False))
    assert lifecycle.interest_tone(one_before, NOW) == ""  # 前一期正向只有 1
    warming = _entry(
        ("a", 20, 1, False), ("b", 3, 1, False), ("c", 5, 1, False), ("d", 6, 1, False)
    )
    assert lifecycle.interest_tone(warming, NOW) == "warming"
    short = _entry(("a", 20, 1, False), ("b", 3, 1, False))  # 需要 ≥ 前一期 + 2
    assert lifecycle.interest_tone(short, NOW) == ""
    none_before = _entry(("b", 3, 1, False), ("c", 5, 1, False), ("d", 6, 1, False))
    assert lifecycle.interest_tone(none_before, NOW) == ""  # 前一期要有证据


async def test_tone_snapshots_use_the_fixed_texts_and_come_once_per_fourteen_days() -> None:
    interests = {
        "独立游戏": _entry(("a", 3, 0.5, True), ("b", 10, 0.5, True)),
        "猫"      : _entry(("a", 20, 1, False), ("b", 25, 1, False), ("c", 3, -1, False)),
        "天文"    : _entry(("a", 20, 1, False), ("b", 3, 1, False), ("c", 5, 1, False), ("d", 6, 1, False)),
        "拍照"    : {"base": 0, "evidence": [
            {"episode": "", "item": "x", "at": NOW - DAY, "delta": 0.5, "mixed": True},
            {"episode": "", "item": "y", "at": NOW - 2 * DAY, "delta": 0.5, "mixed": True},
        ], "updated_at": NOW, "stage": "none"},
    }  # fmt: skip
    snaps: list[dict[str, Any]] = []
    lifecycle.consolidate(interests, snaps, NOW)
    tone = {s["topic"]: s for s in snaps if s.get("kind") == "tone"}
    assert tone["独立游戏"]["text"] == "对独立游戏有点又爱又嫌：手感还挺喜欢，氪金有点看不下去"
    assert tone["拍照"]["text"] == "对拍照的感觉有点复杂"  # 没有 aspects
    assert tone["猫"]["text"] == "前阵子挺爱看猫的，这两周没那么上头了"
    assert tone["天文"]["text"] == "天文最近越看越上头"
    assert {s["tone"] for s in tone.values()} == {"mixed", "cooling", "warming"}
    before = len(snaps)
    lifecycle.consolidate(interests, snaps, NOW + 5 * DAY)
    assert len([s for s in snaps if s.get("kind") == "tone"]) == len(tone) and len(snaps) == before
    lifecycle.consolidate(interests, snaps, NOW + 15 * DAY)  # 14 天后若走向仍成立才会再生成
    for topic in tone:
        assert len([s for s in snaps if s.get("kind") == "tone" and s["topic"] == topic]) <= 2


async def test_chat_gets_tone_lines_on_topic_and_with_the_recap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.snapshots.append(
        {"topic": "独立游戏", "tier": 0, "kind": "tone", "tone": "mixed", "stage": "mixed",
         "created_at": NOW - 2 * DAY, "text": "对独立游戏有点又爱又嫌：手感还挺喜欢，氪金有点看不下去"}
    )  # fmt: skip
    assert "对独立游戏有点又爱又嫌" in _chat(store, "最近有没有玩独立游戏")
    assert "对独立游戏有点又爱又嫌" in _chat(store, "你最近喜欢什么")
    assert "又爱又嫌" not in _chat(store, "今天吃什么")
    store.snapshots[0]["created_at"] = NOW - 15 * DAY  # 超过 14 天不再因话题给
    assert "又爱又嫌" not in _chat(store, "最近有没有玩独立游戏")


# ───────────────────────────── 话题与规划器 ─────────────────────────────


async def test_thread_topic_comes_from_digest_then_interests_then_title(tmp_path: Path) -> None:
    from plugins.xiaoqing_life.sources.base import SourceItem

    items = [SourceItem("bili:1", "bili_popular", "标题", "简介", "u", 1, ())]
    raw   = {
        "thread"   : {"action": "open", "title": "为什么星星会拖线", "topic": "天文摄影"},
        "looked_at": [],
    }
    assert digest.validate(raw, items, max_memes=0).thread.topic == "天文摄影"  # type: ignore[union-attr]
    interests = {"天文观测": {"base": 1.0, "evidence": []}}
    assert th.pick_topic(interests, "天文摄影", "标题", "") == "天文摄影"
    assert th.pick_topic(interests, "", "拍天文的问题", "天文观测技巧") == "天文观测"
    assert th.pick_topic({}, "", "一个很长很长的小课题标题啊", "无关") == "一个很长很长的小课"[:8]
    store  = _store(tmp_path)
    opened = th.open_thread(
        store.threads,
        make_cfg(),
        title      = "为什么星星会拖线",
        why        = "",
        next_query = "",
        now        = NOW,
        topic      = "天文摄影",
    )
    assert (
        opened is not None
        and opened["topic"] == "天文摄影"
        and opened["events"][0]["kind"] == "opened"
    )
    assert opened["park_count"] == 0 and opened["actions"] == [] and opened["corrections"] == []


async def test_thread_events_are_a_bounded_record_of_what_happened() -> None:
    thread = _thread()
    th.park([thread], thread, make_cfg(), reason="r", hint="", now=NOW)
    th.drop(thread, NOW, "不查了")
    th.add_finding(thread, episode_id="e", text="x", quote="q", revises="", now=NOW)
    assert [e["kind"] for e in thread["events"]] == ["parked", "dropped"]
    for n in range(20):
        th.on_thread_event(thread, "opened", now=NOW + n)
    assert len(thread["events"]) == th.MAX_EVENTS


async def test_thread_candidate_no_longer_requires_bili_search() -> None:
    cfg = make_cfg(sources={"bili_search": False}, choice_temperature=0.5)
    score = planner.choose(cfg, default_state(), [_thread()], {}, MOMENT, random.Random(1)).scores
    assert THREAD in score
    only_wiki = make_cfg(
        sources={"bili_search": False, "baike_lookup": False}, choice_temperature=0.5
    )
    assert (
        THREAD
        in planner.choose(
            only_wiki, default_state(), [_thread()], {}, MOMENT, random.Random(1)
        ).scores
    )
    none = make_cfg(sources={"bili_search": False, "baike_lookup": False, "wiki_read": False})
    assert (
        THREAD
        not in planner.choose(
            none, default_state(), [_thread()], {}, MOMENT, random.Random(1)
        ).scores
    )


async def test_unhealthy_bili_search_penalizes_only_interest_dive() -> None:
    cfg = make_cfg(choice_temperature=0.5)
    state = default_state()
    base  = planner.choose(cfg, state, [_thread()], {}, MOMENT, random.Random(1)).scores
    reinforce.record_source(state, "bili_search", False, NOW)
    reinforce.record_source(state, "bili_search", False, NOW)
    shaky = planner.choose(cfg, state, [_thread()], {}, MOMENT, random.Random(1)).scores
    assert shaky[THREAD] == base[THREAD]
    assert shaky[planner.INTEREST] == base[planner.INTEREST] - planner.UNRELIABLE_SEARCH_PENALTY


async def test_status_and_threads_commands_show_the_new_fields(tmp_path: Path) -> None:
    store = _with_thread(
        tmp_path,
        status      = "parked",
        park_reason = "资料不太够",
        notes       = [{"id": "n1", "note": "最新的要点", "episode_id": "e"}],
        corrections = [{"before": "a", "after": "b"}],
    )
    text = commands.threads_text(store)
    assert "[parked]" in text and "搁置原因：资料不太够" in text
    assert "最新要点：最新的要点" in text and "更正 1 次" in text
    assert commands.methods_text(store) == "还没有自己摸索出的查法。"
    store.methods.append(_method(uses=2, wins=1))
    assert "先找较早的原始出处" in commands.methods_text(
        store
    ) and "帮上忙 1 次" in commands.methods_text(store)
    status = commands.status_text(store, make_cfg(), Presence("free", "", None), MOMENT)
    assert "方法 1 条" in status
    assert commands.route("methods") == ("methods", "") and commands.route("方法") == (
        "methods",
        "",
    )


async def test_manifest_lists_the_methods_subcommand() -> None:
    manifest = json.loads(
        (Path(__file__).parents[3] / "plugins" / "xiaoqing_life" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    subs = {s["name"]: s for s in manifest["commands"][0]["subcommands"]}
    assert subs["methods"]["aliases"] == ["方法"] and subs["methods"]["permission"] == "bot_admin"


async def test_methods_collection_is_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.methods.append(_method())
    await store.flush()
    again = LifeStore(tmp_path / "life")
    again.load()
    assert again.methods[0]["id"] == "m1"


# ───────────────────────────── 复查补充 ─────────────────────────────


async def test_repeat_item_in_a_later_round_merges_into_the_same_episode(tmp_path: Path) -> None:
    store = _with_thread(tmp_path)
    ai    = FakeAI(
        _reply(looked_at=[_look()], next={"action": "deep_read", "arg": "i1"}),
        _reply(
            looked_at = [_look()],
            findings  = [_finding("简介里写了原因", "完整简介里说明了原因", ["p1.1"])],
            next      = {"action": "stop"},
        ),
    )
    fetch = _fetch(
        _video(7, "猫咪为什么爱钻纸箱"),
        **{
            "/web-interface/view": {
                "code": 0,
                "data": {"desc": "完整简介里说明了原因。", "owner": {"name": "up"}, "cid": 701},
            },
            "/reply/main": comments_payload(),
        },
    )
    out = await run_session(
        store, make_cfg(max_deep_reads_per_session=0), _continue(), _deps(fetch, ai)
    )
    # 第二轮把同一条读深了：并进第一轮的经历，depth 升级，要点引用它；喜欢只算一次
    assert len(store.episodes) == 1 and store.episodes[0]["depth"] == "read"
    assert store.threads[0]["notes"][-1]["episode_id"] == store.episodes[0]["id"]
    assert out.episodes == 1 and out.details["liked"] == 1.0
    assert store.threads[0]["steps"] == 1


async def test_followup_thread_inherits_the_parent_topic(tmp_path: Path) -> None:
    store = _with_thread(tmp_path, status="done", topic="天文摄影", depth=0)
    parent = store.threads[0]
    ok     = wind_down.apply_followup(
        store,
        {
            "parent_id" : parent["id"],
            "title"     : "星轨要拍多久",
            "why"       : "想试试",
            "next_query": "星轨 曝光",
        },
        [parent],
        make_cfg(),
        "2026-11-05",
        NOW,
    )
    assert ok and store.threads[-1]["topic"] == "天文摄影"
    assert store.threads[-1]["parent_id"] == parent["id"]


async def test_lookup_skips_entries_read_within_seven_days(tmp_path: Path) -> None:
    store = _with_thread(tmp_path, next_action={"action": "lookup", "arg": "猫"})
    fetch = _fetch(**{"Baike": BAIKE, "wikipedia": WIKI})
    await run_session(
        store, make_cfg(), _continue(), _deps(fetch, FakeAI(_reply(looked_at=[_look()])))
    )
    assert store.seen
    store2 = store
    for item_id in list(store2.seen):
        store2.seen[item_id] = NOW - 60
    ai = FakeAI(_reply())
    store2.threads[0].update(status="open", next_action={"action": "lookup", "arg": "猫"})
    out = await run_session(
        store2, make_cfg(sources={"bili_search": False}), _continue(), _deps(fetch, ai)
    )
    assert out.reason == "all_seen" and ai.calls == []


async def test_finding_quote_may_join_sentences_from_several_cited_passages() -> None:
    texts = {
        "p1.1": "摄像机位置保持不动，随着时间的流逝，恒星本身发生了移动。",
        "p3.1": "曝光时间越长，亮痕越长。另有一句。",
    }
    joined = "摄像机位置保持不动，随着时间的流逝，恒星本身发生了移动。曝光时间越长，亮痕越长。"
    assert research.finding_quote(joined, ["p1.1", "p3.1"], texts)
    # 某一句不在所引编号里：整条不算
    forged = "摄像机位置保持不动，随着时间的流逝，恒星本身发生了移动。地球其实是平的呢。"
    assert research.finding_quote(forged, ["p1.1", "p3.1"], texts) is None
    # 只引了其中一个编号：另一句找不到
    assert research.finding_quote(joined, ["p1.1"], texts) is None
    # 大半是没核对的短碎片：不算
    assert research.finding_quote("曝光时间越长。是。的。对。吧。嗯。", ["p3.1"], texts) is None


async def test_next_actions_on_disabled_sources_become_stop() -> None:
    cfg = make_cfg(sources={"bili_search": False, "baike_lookup": True, "wiki_read": False})
    items   = [SourceItem("baike_read:x", "baike_read", "星轨", "", "", 0, ())]
    allowed = research.available_actions(cfg, {}, items, NOW)
    assert allowed == ("lookup", "stop")
    assert (
        research._next({"action": "search", "arg": "星轨 原因"}, items, allowed) == research.Next()
    )
    assert research._next({"action": "lookup", "arg": "地球自转"}, items, allowed) == research.Next(
        "lookup", "地球自转"
    )
    bili = [SourceItem("bili:1", "bili_search", "视频", "", "", 0, ())]
    assert "deep_read" in research.available_actions(make_cfg(), {}, bili, NOW)
    unhealthy = {"source_health": {"bili_search": {"fails": 2, "until": NOW + 3600}}}
    assert "search" not in research.available_actions(make_cfg(), unhealthy, bili, NOW)


async def test_prompt_lists_the_available_actions(tmp_path: Path) -> None:
    store = _with_thread(tmp_path, next_action={"action": "lookup", "arg": "猫"})
    ai = FakeAI(_reply(looked_at=[_look()], next={"action": "search", "arg": "猫 纸箱"}))
    fetch = _fetch(**{"Baike": BAIKE, "wikipedia": WIKI})
    cfg = make_cfg(sources={"bili_search": False})
    await run_session(store, cfg, _continue(), _deps(fetch, ai))
    payload = json.loads(ai.calls[0][1][1]["content"])
    assert payload["available_actions"] == ["lookup", "stop"]
    assert len(ai.calls) == 1  # 想 search 但搜索关着：当作 stop
    assert _calls(fetch, "search/type") == []


async def test_first_round_falls_back_to_the_other_opening_once(tmp_path: Path) -> None:
    # 词条这周读过了：第一轮改去 B站 搜一次，而不是整次研究白跑
    store = _with_thread(tmp_path, next_action={"action": "lookup", "arg": "猫"})
    for item_id in ("baike_read:纸箱", "wiki_read:5"):
        store.seen[item_id] = NOW - 60
    ai = FakeAI(_reply(looked_at=[_look()], next={"action": "stop"}))
    fetch = _fetch(_video(7, "猫咪为什么爱钻纸箱"), **{"Baike": BAIKE, "wikipedia": WIKI})
    out   = await run_session(store, make_cfg(), _continue(), _deps(fetch, ai))
    assert out.status == "ok" and len(ai.calls) == 1
    assert [a["action"] for a in store.threads[0]["actions"]] == ["lookup", "search"]
    assert _calls(fetch, "search/type")[0][1]["keyword"] == "猫 纸箱"
    # 两条路都没有：照旧记空步
    store.threads[0].update(next_action={"action": "lookup", "arg": "猫"})
    store.seen["bili:7"] = NOW - 60
    ai2                  = FakeAI()
    out                  = await run_session(store, make_cfg(), _continue(), _deps(fetch, ai2))
    assert out.reason == "all_seen" and ai2.calls == []
