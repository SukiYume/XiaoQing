"""第五轮里程碑 3：小作品、主题档案、780 字预算。"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import commands, dossier, planner, wind_down, works
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.config import parse_life_config
from plugins.xiaoqing_life.planner import WORK_ON, Decision
from plugins.xiaoqing_life.session import SessionDeps, run_session
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.plugins.xiaoqing_life.support import FakeAI, FakeFetcher, at, make_cfg

pytestmark = pytest.mark.asyncio

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()
DAY    = 86400.0


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


def _episode(n: int, *, title: str = "", topics: list[str] | None = None, feeling: str = "like",
             ago: float = 3600.0, **kw: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "id"      : f"e{n}", "at": NOW - ago, "day": "2026-11-05", "title": title or f"视频{n}",
        "reaction": "挺喜欢", "feeling": feeling, "gist": "", "tags": [], "topics": topics or ["独立游戏"],
        "source"  : "bili_popular", **kw,
    }  # fmt: skip


def _work(**kw: Any) -> dict[str, Any]:
    return {
        "id"        : "w1", "title": "独立游戏片单", "form": "list", "topic": "独立游戏", "why": "常刷到",
        "intro"     : "", "status": "drafting", "entries": [], "entry_seq": 0, "revisions": 0,
        "created_at": NOW - 3 * DAY, "updated_at": NOW - 3 * DAY, "done_at": 0.0,
        "last_op_at": 0.0, "last_add_at": 0.0, "last_ops": [], **kw,
    }  # fmt: skip


def _entry(n: int, kind: str = "point", text: str = "", **kw: Any) -> dict[str, Any]:
    return {
        "id": f"w{n}", "kind": kind, "text": text or f"条目{n}", "episode_ids": ["e1"], "check": "",
        "added_at": NOW - DAY * (10 - n), "revised_at": NOW - DAY, **kw,
    }  # fmt: skip


# ───────────────────────────── 开始 ─────────────────────────────


def _repeated_interest(store: LifeStore, topic: str = "独立游戏") -> None:
    store.interests[topic] = {
        "base"    : 0.0, "updated_at": NOW, "stage": "repeated",
        "evidence": [
            {"episode": "e", "item": "a", "at": NOW - DAY, "delta": 1},
            {"episode": "e", "item": "b", "at": NOW - 2 * DAY, "delta": 1},
        ],
    }  # fmt: skip


def _start_raw(**kw: Any) -> dict[str, Any]:
    return {
        "action": "start", "title": "独立游戏片单", "form": "list", "topic": "独立游戏",
        "why"   : "最近常刷到", "episode_ids": ["e1", "e2"], **kw,
    }  # fmt: skip


async def test_start_needs_a_grounded_topic(tmp_path: Path) -> None:
    cfg   = make_cfg()
    store = _store(tmp_path)
    store.episodes += [_episode(1), _episode(2)]
    # 依据 1：并入的兴趣是 repeated
    assert works.start_work(store, cfg, _start_raw(episode_ids=[]), NOW) is None
    _repeated_interest(store)
    made = works.start_work(store, cfg, _start_raw(episode_ids=[]), NOW)
    assert made is not None and made["status"] == "drafting" and made["form"] == "list"
    assert store.state["last_work_start"] == NOW and store.works == [made]
    # 依据 2：引用 ≥2 条喜欢的同话题经历
    other = _store(tmp_path / "b")
    other.episodes += [_episode(1), _episode(2)]
    assert works.start_work(other, cfg, _start_raw(episode_ids=["e1"]), NOW) is None
    assert works.start_work(other, cfg, _start_raw(), NOW) is not None
    # 经历要是喜欢的、同话题的
    dull = _store(tmp_path / "c")
    dull.episodes += [_episode(1, feeling="meh"), _episode(2, topics=["做饭"])]
    assert works.start_work(dull, cfg, _start_raw(), NOW) is None
    # 依据 3：30 天内 done 的小课题话题相近
    done = _store(tmp_path / "d")
    done.threads.append(
        {
            "id"        : "t",
            "title"     : "游戏手感",
            "topic"     : "独立游戏",
            "status"    : "done",
            "updated_at": NOW - 5 * DAY,
        }
    )
    assert works.start_work(done, cfg, _start_raw(episode_ids=[]), NOW) is not None
    done.works.clear()
    done.state.pop("last_work_start")
    done.threads[0]["updated_at"] = NOW - 40 * DAY
    assert works.start_work(done, cfg, _start_raw(episode_ids=[]), NOW) is None


async def test_start_respects_slots_gap_blocklist_and_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _repeated_interest(store)
    store.episodes += [_episode(1), _episode(2)]
    assert works.start_work(store, make_cfg(max_active_works=0), _start_raw(), NOW) is None
    for bad in (
        _start_raw(form="poem"), _start_raw(title=""), _start_raw(topic=""),
        _start_raw(action="stop"), _start_raw(title="含违禁词的"),
    ):  # fmt: skip
        assert works.start_work(store, make_cfg(blocked_keywords=["违禁词"]), bad, NOW) is None
    assert works.start_work(store, make_cfg(), "nope", NOW) is None
    first = works.start_work(store, make_cfg(), _start_raw(title="游戏片单合集" * 8), NOW)
    assert first is not None and len(first["title"]) == 20
    # 名额满了
    assert works.start_work(store, make_cfg(), _start_raw(), NOW + 30 * DAY) is None
    # 名额够但间隔不到 work_start_gap_days
    roomy = make_cfg(max_active_works=2)
    assert works.start_work(store, roomy, _start_raw(title="第二件"), NOW + 3 * DAY) is None
    assert works.start_work(store, roomy, _start_raw(title="第二件"), NOW + 8 * DAY) is not None
    assert make_cfg().max_active_works == 1 and make_cfg().work_start_gap_days == 7
    assert make_cfg().dossier_keep_days == 365 and make_cfg().session_minutes["work_on"] == (15, 30)
    assert parse_life_config({"max_active_works": 9}).max_active_works == 3
    assert parse_life_config({"dossier_keep_days": 1}).dossier_keep_days == 30


async def test_candidate_topics_are_capped_and_exclude_running_works(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for topic in ("独立游戏", "天文摄影", "骑行", "烘焙"):
        _repeated_interest(store, topic)
    assert len(works.candidate_topics(store, make_cfg(), NOW)) == 3
    assert works.candidate_topics(store, make_cfg(max_active_works=0), NOW) == []
    store.works.append(_work(topic="独立游戏"))
    assert works.candidate_topics(store, make_cfg(max_active_works=2), NOW)[:1] != ["独立游戏"]
    assert works.candidate_topics(store, make_cfg(), NOW) == []  # 名额满
    gap = _store(tmp_path / "g")
    _repeated_interest(gap)
    gap.state["last_work_start"] = NOW - DAY
    assert works.candidate_topics(gap, make_cfg(), NOW) == []
    liked = _store(tmp_path / "l")
    liked.episodes += [_episode(1), _episode(2)]
    assert works.candidate_topics(liked, make_cfg(), NOW) == ["独立游戏"]


async def _wind(store: LifeStore, reply: dict[str, Any], **cfg: Any):
    ai = FakeAI(reply)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    result = await wind_down.run_wind_down(
        store, make_cfg(**cfg), at(23, 0), complete, persona="p", moment_line="", reasoning_ok=True
    )
    return result, ai


def _diary(ids: list[str]) -> dict[str, Any]:
    return {"date": "2026-11-05", "text": "今天看了点游戏。", "episode_ids": ids}


async def test_wind_down_offers_candidates_and_starts_at_most_one_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _repeated_interest(store)
    store.episodes += [_episode(1), _episode(2)]
    reply = {"diary": _diary(["e1"]), "work": _start_raw()}
    _result, ai = await _wind(store, reply)
    record = json.loads(ai.calls[0][1][1]["content"])["untrusted_record"]
    assert record["work_candidates"] == ["独立游戏"] and record["works"] == []
    assert len(store.works) == 1 and store.works[0]["title"] == "独立游戏片单"
    assert "独立游戏" in store.dossiers  # 开作品同时建了档案
    # 没有候选时不给这一项，且模型硬给也不会开
    plain = _store(tmp_path / "p")
    plain.episodes.append(_episode(1, feeling="meh"))
    _r, ai2 = await _wind(plain, {"diary": _diary(["e1"]), "work": _start_raw(episode_ids=["e1"])})
    assert "work_candidates" not in json.loads(ai2.calls[0][1][1]["content"])["untrusted_record"]
    assert plain.works == []
    # 已有的作品出现在记录里
    again = _store(tmp_path / "a")
    again.works.append(_work())
    again.episodes.append(_episode(1))
    _r, ai3 = await _wind(again, {"diary": _diary(["e1"])})
    shown = json.loads(ai3.calls[0][1][1]["content"])["untrusted_record"]["works"]
    assert shown[0]["id"] == "w1" and shown[0]["entries"] == 0


# ───────────────────────────── 整理：op 核对 ─────────────────────────────

GROUNDS = {"e1": "星露谷物语 模拟经营 独立游戏", "e2": "空洞骑士 银河城"}


def _apply(work: dict[str, Any], rows: Any, reading: dict[str, str] | None = None,
           grounds: dict[str, str] | None = None, blocked: tuple[str, ...] = ()):  # fmt: skip
    return works.apply_ops(work, rows, grounds or GROUNDS, reading or {}, blocked, NOW)


async def test_item_names_must_appear_in_the_cited_episodes() -> None:
    work = _work()
    rows = [
        {"op": "add", "kind": "item", "text": "星露谷物语：种田很治愈", "episode_ids": ["e1"]},
        {"op": "add", "kind": "item", "text": "塞尔达传说：没看过却想放", "episode_ids": ["e1"]},
        {"op": "add", "kind": "item", "text": "没有冒号的一句话", "episode_ids": ["e1"]},
        {"op": "add", "kind": "item", "text": "空：名字太短", "episode_ids": ["e2"]},
    ]
    applied = _apply(work, rows)
    assert [e["text"] for _op, e in applied] == ["星露谷物语：种田很治愈"]
    entry = work["entries"][0]
    assert entry["id"] == "w1" and entry["episode_ids"] == ["e1"] and entry["kind"] == "item"
    # 英文冒号、全半角、大小写不敏感
    more = _apply(
        work, [{"op": "add", "kind": "item", "text": "空洞 骑士: 好难", "episode_ids": ["e2"]}]
    )
    assert len(more) == 1


async def test_ops_need_cited_known_episodes_and_valid_kinds() -> None:
    work = _work()
    bad  = [
        {"op": "add", "kind": "point", "text": "没引用", "episode_ids": []},
        {"op": "add", "kind": "point", "text": "引用了不存在的", "episode_ids": ["e9"]},
        {"op": "add", "kind": "point", "text": "一个对一个错", "episode_ids": ["e1", "e9"]},
        {"op": "add", "kind": "poem", "text": "未知类型", "episode_ids": ["e1"]},
        {"op": "add", "kind": "point", "text": "", "episode_ids": ["e1"]},
        {"op": "bogus", "kind": "point", "text": "未知 op", "episode_ids": ["e1"]},
        "junk",
        {"op": "add", "kind": "todo", "text": "没有 check", "episode_ids": ["e1"]},
    ]
    assert _apply(work, bad) == [] and work["entries"] == []
    ok = _apply(
        work,
        [
            {
                "op"         : "add",
                "kind"       : "todo",
                "text"       : "查一下发售日",
                "episode_ids": ["e1"],
                "check"      : "星露谷",
            }
        ],
    )
    assert work["entries"][0]["check"] == "星露谷" and len(ok) == 1
    assert _apply(work, "not a list") == []


async def test_ops_are_capped_at_three_and_entries_at_twelve() -> None:
    work = _work()
    rows = [
        {"op": "add", "kind": "point", "text": f"想法{n}", "episode_ids": ["e1"]} for n in range(6)
    ]
    assert len(_apply(work, rows)) == 3
    full = _work(entries=[_entry(n) for n in range(1, 13)], entry_seq=12)
    assert _apply(full, rows[:1]) == [] and len(full["entries"]) == 12
    assert (
        len(
            _apply(
                full,
                [
                    {"op": "remove", "entry": "w1"},
                    {"op": "add", "kind": "point", "text": "新", "episode_ids": ["e1"]},
                ],
            )
        )
        == 2
    )


async def test_revise_and_remove_need_an_existing_entry() -> None:
    work = _work(entries=[_entry(1, "item", "星露谷物语：旧评价"), _entry(2, "point", "旧想法")])
    rows = [
        {"op": "revise", "entry": "w1", "text": "星露谷物语：新评价", "episode_ids": ["e1"]},
        {"op": "revise", "entry": "w9", "text": "不存在", "episode_ids": ["e1"]},
        {"op": "revise", "entry": "w2", "text": "没引用", "episode_ids": []},
    ]
    applied = _apply(work, rows)
    assert [(op, e["id"]) for op, e in applied] == [("revise", "w1")]
    assert work["entries"][0]["text"] == "星露谷物语：新评价"
    # item 条目改写后名字也要还在所引经历里
    assert (
        _apply(
            work,
            [{"op": "revise", "entry": "w1", "text": "塞尔达传说：乱改", "episode_ids": ["e1"]}],
        )
        == []
    )
    assert [
        op
        for op, _ in _apply(
            work, [{"op": "remove", "entry": "w2"}, {"op": "remove", "entry": "w2"}]
        )
    ] == ["remove"]
    assert [e["id"] for e in work["entries"]] == ["w1"]


async def test_verify_turns_a_todo_into_a_point_only_with_the_reading() -> None:
    todo = _entry(1, "todo", "查一下是不是真的", check="真相")
    work = _work(entries=[todo, _entry(2, "point", "确定的点")])
    grounds = {**GROUNDS, "i1": "真相 百科词条"}
    reading = {"i1": "e-reading"}
    # 只引用自己的老经历：不行；对非 todo：不行
    assert (
        _apply(
            work,
            [{"op": "verify", "entry": "w1", "text": "确认了", "episode_ids": ["e1"]}],
            reading,
            grounds,
        )
        == []
    )
    assert (
        _apply(
            work,
            [{"op": "verify", "entry": "w2", "text": "x", "episode_ids": ["i1"]}],
            reading,
            grounds,
        )
        == []
    )
    applied = _apply(
        work,
        [{"op": "verify", "entry": "w1", "text": "确认了：是真的", "episode_ids": ["i1"]}],
        reading,
        grounds,
    )
    assert [op for op, _ in applied] == ["verify"]
    assert work["entries"][0]["kind"] == "point" and work["entries"][0]["check"] == ""
    assert work["entries"][0]["episode_ids"] == ["e-reading"]  # 编号换成了真实的经历 ID
    assert work["last_add_at"] == NOW


async def test_entry_text_passes_blocklist_and_profanity() -> None:
    work = _work()
    rows = [
        {"op": "add", "kind": "point", "text": "含违禁词的一条", "episode_ids": ["e1"]},
        {"op": "add", "kind": "point", "text": "这游戏就是个傻逼", "episode_ids": ["e1"]},
        {"op": "add", "kind": "point", "text": "正常的一条", "episode_ids": ["e1"]},
    ]
    assert [e["text"] for _o, e in _apply(work, rows, blocked=("违禁词",))] == ["正常的一条"]


async def test_done_needs_four_solid_entries() -> None:
    work = _work(entries=[_entry(1), _entry(2), _entry(3), _entry(4, "todo", check="x")])
    works.finish_status(work, "done", NOW)
    assert work["status"] == "drafting"
    work["entries"].append(_entry(5))
    works.finish_status(work, "drafting", NOW)
    assert work["status"] == "drafting"
    works.finish_status(work, "done", NOW)
    assert work["status"] == "done" and work["done_at"] == NOW


async def test_record_ops_updates_counters_and_keeps_three_recent_texts() -> None:
    work = _work()
    works.record_ops(work, [], NOW)
    assert work["revisions"] == 0 and work["last_op_at"] == 0.0
    ops = [("add", {"text": f"文字{n}"}) for n in range(5)] + [("remove", {"text": "删掉的"})]
    works.record_ops(work, ops, NOW)  # type: ignore[arg-type]
    assert work["revisions"] == 1 and work["updated_at"] == NOW and work["last_op_at"] == NOW
    assert work["last_ops"] == ["文字2", "文字3", "文字4"]


# ───────────────────────────── 整理：会话 ─────────────────────────────

BAIKE = {
    "title"   : "银河城",
    "desc"    : "游戏类型",
    "abstract": "银河城是一类探索型游戏。核心是能力解锁。",
}


def _deps(fetch: FakeFetcher, ai: FakeAI, **kw: Any) -> SessionDeps:
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    return SessionDeps(
        fetch=fetch, complete=complete, rng=random.Random(1), now=lambda: MOMENT, **kw
    )


def _decision() -> Decision:
    return Decision(WORK_ON, "sampled", {WORK_ON: 40.0}, {"work_id": "w1", "query": "独立游戏片单"})


def _work_store(tmp_path: Path, **kw: Any) -> LifeStore:
    store = _store(tmp_path)
    store.episodes += [
        _episode(1, title="星露谷物语 评测", tags=["独立游戏"]),
        _episode(2, title="空洞骑士 通关"),
    ]
    store.works.append(_work(**kw))
    return store


async def test_work_on_applies_ops_and_rewards_half_per_op(tmp_path: Path) -> None:
    store = _work_store(tmp_path)
    reply = {
        "ops": [
            {"op": "add", "kind": "item", "text": "星露谷物语：种田很治愈", "episode_ids": ["e1"]},
            {"op": "add", "kind": "item", "text": "空洞骑士：好难但上头", "episode_ids": ["e2"]},
            {"op": "add", "kind": "item", "text": "没看过的游戏：不该进来", "episode_ids": ["e2"]},
        ],
        "intro" : "我最近迷上的几个独立游戏",
        "status": "drafting",
    }
    ai    = FakeAI(reply)
    fetch = FakeFetcher({})
    out   = await run_session(store, make_cfg(), _decision(), _deps(fetch, ai))
    work  = store.works[0]
    assert out.status == "ok" and out.details["ops"] == 2
    assert [e["text"] for e in work["entries"]] == [
        "星露谷物语：种田很治愈",
        "空洞骑士：好难但上头",
    ]
    assert work["intro"] == "我最近迷上的几个独立游戏" and work["revisions"] == 1
    assert work["last_op_at"] == NOW and len(work["last_ops"]) == 2
    assert store.state["last_reward"] == 1.0
    assert store.state["preferences"]["activity"]["work_on"] == 1.0
    assert store.state["sessions_today"] == 1 and store.state["last_work_on_at"] == NOW
    assert store.state["activity_log"][-1]["end"] > store.state["activity_log"][-1]["start"]
    span = store.state["last_session_end"] - store.state["last_session_at"]
    assert 15 * 60 <= span <= 30 * 60
    assert len(ai.calls) == 1 and fetch.calls == []  # 没有 todo：不查百科
    sent = json.loads(ai.calls[0][1][1]["content"])
    assert {e["id"] for e in sent["untrusted_record"]["episodes"]} == {"e1", "e2"}
    assert "untrusted_material" not in sent and sent["work"]["entries"] == []


async def test_work_on_reward_is_capped_and_empty_ops_reward_nothing(tmp_path: Path) -> None:
    store = _work_store(tmp_path)
    rows  = [
        {"op": "add", "kind": "point", "text": f"想法{n}", "episode_ids": ["e1"]} for n in range(3)
    ]
    await run_session(store, make_cfg(), _decision(), _deps(FakeFetcher({}), FakeAI({"ops": rows})))
    assert store.state["last_reward"] == 1.5  # 3 个 op × 0.5，上限 1.5
    idle = _work_store(tmp_path / "i")
    await run_session(idle, make_cfg(), _decision(), _deps(FakeFetcher({}), FakeAI({"ops": []})))
    assert idle.state["last_reward"] == 0.0 and idle.works[0]["revisions"] == 0


async def test_work_on_checks_the_earliest_todo_with_baike_and_wiki(tmp_path: Path) -> None:
    store = _work_store(
        tmp_path,
        entries=[
            _entry(1, "todo", "银河城是不是一个类型", check="银河城", added_at=NOW - 5 * DAY),
            _entry(2, "todo", "另一件事", check="别的词", added_at=NOW - DAY),
        ],
        entry_seq=2,
    )
    wiki = {
        "query": {
            "pages": {
                "3": {"pageid": 3, "index": 1, "title": "银河城", "extract": "銀河城是一類遊戲。"}
            }
        }
    }
    reply = {
        "ops": [
            {"op": "verify", "entry": "w1", "text": "银河城是一类探索型游戏", "episode_ids": ["i1"]}
        ],
        "looked_at": [
            {"item": "i1", "reaction": "原来如此", "feeling": "like", "topics": ["银河城"]}
        ],
    }
    ai    = FakeAI(reply)
    fetch = FakeFetcher({"Baike": BAIKE, "wikipedia": wiki})
    await run_session(store, make_cfg(), _decision(), _deps(fetch, ai))
    assert [c[1].get("bk_key") for c in fetch.calls if "Baike" in c[0]] == ["银河城"]  # 只查最早的
    sent = json.loads(ai.calls[0][1][1]["content"])
    assert [i["source"] for i in sent["untrusted_material"]["items"]] == ["baike_read", "wiki_read"]
    reading = [e for e in store.episodes if e.get("activity") == "work_on"]
    assert reading and reading[0]["source"] == "baike_read"
    first = store.works[0]["entries"][0]
    assert first["kind"] == "point" and first["episode_ids"] == [reading[0]["id"]]
    assert "baike_read:银河城" in store.seen
    # 读物产生的词条 / 表达 / 兴趣证据：作品流程不学
    assert store.terms == {} and store.expressions == []
    assert "银河城" not in store.interests
    # 再来一次：看过的（7 天内）不再读
    store.works[0]["entries"].append(
        _entry(3, "todo", "再确认", check="银河城", added_at=NOW - 6 * DAY)
    )
    store.works[0]["entries"][0]["kind"] = "todo"
    ai2                                  = FakeAI({"ops": []})
    fetch2                               = FakeFetcher({"Baike": BAIKE, "wikipedia": wiki})
    await run_session(store, make_cfg(), _decision(), _deps(fetch2, ai2))
    assert "untrusted_material" not in json.loads(ai2.calls[0][1][1]["content"])


async def test_work_on_without_lookup_sources_still_works(tmp_path: Path) -> None:
    store = _work_store(tmp_path, entries=[_entry(1, "todo", "待查", check="词")], entry_seq=1)
    off = make_cfg(sources={"baike_lookup": False, "wiki_read": False})
    fetch = FakeFetcher({})
    await run_session(store, off, _decision(), _deps(fetch, FakeAI({"ops": []})))
    assert fetch.calls == []


async def test_work_on_failure_paths_and_budget(tmp_path: Path) -> None:
    store = _work_store(tmp_path)
    out   = await run_session(
        store, make_cfg(max_llm_calls_per_day=0), _decision(), _deps(FakeFetcher({}), FakeAI())
    )
    assert (out.status, out.reason) == ("skipped", "llm_exhausted")
    out = await run_session(
        store, make_cfg(), _decision(), _deps(FakeFetcher({}), FakeAI("不是 JSON"))
    )
    assert out.reason == "digest_unparseable" and store.works[0]["revisions"] == 0
    out = await run_session(
        store, make_cfg(), _decision(), _deps(FakeFetcher({}), FakeAI(RuntimeError("x")))
    )
    assert out.reason == "model_failed"
    out = await run_session(
        store, make_cfg(), _decision(), _deps(FakeFetcher({}), FakeAI(), reasoning_ok=False)
    )
    assert out.reason == "model_not_configured"
    gone = Decision(WORK_ON, "x", {}, {"work_id": "nope"})
    assert (
        await run_session(store, make_cfg(), gone, _deps(FakeFetcher({}), FakeAI()))
    ).reason == "work_missing"


async def test_work_text_never_feeds_terms_expressions_or_interests(tmp_path: Path) -> None:
    store  = _work_store(tmp_path)
    before = copy.deepcopy((store.terms, store.expressions, store.interests))
    reply  = {
        "ops": [
            {"op": "add", "kind": "point", "text": "哈基米这个词很可爱", "episode_ids": ["e1"]}
        ],
        "terms": [{"term": "哈基米", "meaning": "猫", "quote": "哈基米", "refs": ["i1"]}],
    }
    await run_session(store, make_cfg(), _decision(), _deps(FakeFetcher({}), FakeAI(reply)))
    after = (store.terms, store.expressions, store.interests)
    assert all(v["evidence"] == [] for v in after[2].values())  # 没有任何兴趣证据
    assert after[0] == before[0] and after[1] == before[1]


async def test_work_text_is_not_a_source_for_episodes_or_reading_refs() -> None:
    # 作品条目不会出现在 ref_texts：模型不能拿作品文字当摘录来源
    from plugins.xiaoqing_life import digest

    assert digest.ref_texts([]) == {}


# ───────────────────────────── 规划器 ─────────────────────────────


def _score(work: dict[str, Any], episodes: list[dict[str, Any]], state: dict[str, Any] | None = None,
           threads: list[dict[str, Any]] | None = None, now: float = NOW):  # fmt: skip
    return works.work_on_score(work, state or default_state(), episodes, threads or [], now)


async def test_work_on_score_formula() -> None:
    work = _work()
    fresh = [_episode(n, ago=3600.0 * n) for n in range(1, 4)]
    assert _score(work, []) is None  # 没有新材料也没有 todo：没有候选
    assert _score(work, fresh[:1]) == 32 + 6 * 1
    assert _score(work, fresh) == 32 + 6 * 2  # min(新材料, 2)
    todo = _work(entries=[_entry(1, "todo", check="词")])
    assert _score(todo, []) == 32 + 6
    assert _score(todo, fresh) == 32 + 12 + 6
    streak = {
        **default_state(),
        "recent": [{"choice": WORK_ON}, {"choice": "hot_scroll"}, {"choice": WORK_ON}],
    }
    assert _score(work, fresh, streak) == 32 + 12 - 15
    one = {
        **default_state(),
        "recent": [{"choice": WORK_ON}, {"choice": "hot_scroll"}, {"choice": "hot_scroll"}],
    }
    assert _score(work, fresh, one) == 44
    cooling = {**default_state(), "last_work_on_at": NOW - 6 * 3600}
    assert _score(work, fresh, cooling) == 44 - 20
    assert _score(work, fresh, {**default_state(), "last_work_on_at": NOW - 13 * 3600}) == 44
    both = {**streak, "last_work_on_at": NOW - 3600}
    assert _score(todo, fresh, both) == 32 + 12 + 6 - 15 - 20


async def test_new_material_counts_only_related_liked_episodes_since_the_last_tidy() -> None:
    work = _work(last_op_at=NOW - 2 * 3600)
    mine = [
        _episode(1, ago=3600.0),  # 之后的相关喜欢：算
        _episode(2, ago=3 * 3600.0),  # 整理之前：不算
        _episode(3, ago=1800.0, feeling="meh"),  # 不喜欢：不算
        _episode(4, ago=1800.0, topics=["做饭"]),  # 不相关：不算
        _episode(5, ago=1800.0, feeling="mixed"),  # mixed 算
        _episode(6, ago=1800.0, auto=True),  # 自动补的：不算
    ]
    assert [e["id"] for e in works.related_episodes(work, mine)] == ["e5", "e1"]
    threads = [
        {
            "topic"     : "独立游戏",
            "title"     : "课题",
            "notes"     : [{"kind": "finding", "note": "要点", "at": NOW - 600}],
            "takeaway"  : "收获",
            "updated_at": NOW - 600,
        },
        {
            "topic"     : "独立游戏",
            "title"     : "旧课题",
            "notes"     : [],
            "takeaway"  : "上次整理前就有的收获",
            "updated_at": NOW - 5 * 3600,
        },
        {
            "topic": "做饭",
            "title": "别的",
            "notes": [{"kind": "finding", "note": "无关", "at": NOW}],
        },
    ]
    notes = works.related_notes(work, threads)
    assert {n["text"] for n in notes} == {"要点", "收获"}


async def test_planner_offers_work_on_with_an_activity_preference(tmp_path: Path) -> None:
    cfg = make_cfg(choice_temperature=0.5)
    work   = _work()
    scores = planner.choose(
        cfg, default_state(), [], {}, MOMENT, random.Random(1), works=[work], episodes=[_episode(1)]
    ).scores
    assert scores[WORK_ON] == 38.0
    state                = default_state()
    state["preferences"] = {"activity": {WORK_ON: 2.0, "hot_scroll": -1.0}}
    boosted              = planner.choose(
        cfg, state, [], {}, MOMENT, random.Random(1), works=[work], episodes=[_episode(1)]
    ).scores
    assert boosted[WORK_ON] > 38.0
    off = planner.choose(
        make_cfg(max_active_works=0),
        default_state(),
        [],
        {},
        MOMENT,
        random.Random(1),
        works    = [work],
        episodes = [_episode(1)],
    ).scores
    assert WORK_ON not in off
    done = planner.choose(
        cfg,
        default_state(),
        [],
        {},
        MOMENT,
        random.Random(1),
        works=[_work(status="done")],
        episodes=[_episode(1)],
    ).scores
    assert WORK_ON not in done
    decision = planner.decide(
        cfg,
        default_state(),
        [_episode(1)],
        [],
        {},
        MOMENT,
        planner.Presence("free", "", None),
        random.Random(1),
        manual = True,
        works  = [work],
    )
    assert WORK_ON in decision.scores
    assert commands._CHOICES["work_on"] == "整理作品"


async def test_work_on_counts_toward_session_limits(tmp_path: Path) -> None:
    store = _work_store(tmp_path)
    await run_session(store, make_cfg(), _decision(), _deps(FakeFetcher({}), FakeAI({"ops": []})))
    assert store.state["sessions_today"] == 1 and store.state["recent"][-1]["choice"] == WORK_ON
    assert planner._window_minutes(store.state, store.state["last_session_end"], 180) > 0


# ───────────────────────────── 搁置 / 保留 ─────────────────────────────


async def test_drafting_works_untouched_for_21_days_are_shelved_and_old_ones_dropped() -> None:
    stale = _work(id="a", created_at=NOW - 22 * DAY, updated_at=NOW - 22 * DAY)
    live = _work(
        id="b", created_at=NOW - 22 * DAY, last_op_at=NOW - 5 * DAY, updated_at=NOW - 5 * DAY
    )
    old_done = _work(id="c", status="done", updated_at=NOW - 181 * DAY)
    new_done = _work(id="d", status="done", updated_at=NOW - 179 * DAY)
    old_shelf = _work(id="e", status="shelved", updated_at=NOW - 181 * DAY)
    rows = [stale, live, old_done, new_done, old_shelf]
    works.housekeep(rows, NOW)
    assert stale["status"] == "shelved" and live["status"] == "drafting"
    assert [w["id"] for w in rows] == ["a", "b", "d"]


# ───────────────────────────── 聊天侧：作品 ─────────────────────────────


def _chat(store: LifeStore, text: str, *, recent: list[str] | None = None, bot: list[str] | None = None,
          mode: str = "reply", exclude: list[str] | None = None, moment: Any = MOMENT):  # fmt: skip
    payload = {
        "version": 1, "mode": mode, "text": text, "recent": recent or [], "bot_recent": bot or [],
        "unknown_terms": [], "exclude_ids": exclude or [],
    }  # fmt: skip
    got = tc.turn_context(store, make_cfg(), payload, moment, random.Random(0))[0]
    return got or {}


async def test_work_line_by_progress_and_topic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    work = _work(entries=[_entry(1), _entry(2)], last_ops=["星露谷物语：种田很治愈"])
    store.works.append(work)
    text = _chat(store, "你最近在干嘛")["life_lines"]
    assert (
        "你在慢慢整理的：《独立游戏片单》（片单，刚开了个头）；最近加了：星露谷物语：种田很治愈"
        in text
    )
    work["entries"] = [_entry(n) for n in range(1, 5)]
    assert "（片单，攒了一些）" in _chat(store, "最近在干嘛呀")["life_lines"]
    work["entries"] = [_entry(n) for n in range(1, 9)]
    assert "（片单，快弄完了）" in _chat(store, "最近在干嘛呀")["life_lines"]
    assert "整理的" not in (_chat(store, "你好吗") or {"life_lines": ""})["life_lines"]
    assert "《独立游戏片单》" in _chat(store, "聊聊独立游戏吧")["life_lines"]
    work.update(status="done", done_at=NOW - 2 * DAY)
    assert (
        "你之前整理完的：《独立游戏片单》（片单）" in _chat(store, "聊聊独立游戏吧")["life_lines"]
    )
    work["done_at"] = NOW - 40 * DAY
    assert "整理完的" not in (_chat(store, "聊聊独立游戏吧") or {"life_lines": ""})["life_lines"]


async def test_excerpt_block_when_she_is_asked_to_show_it(tmp_path: Path) -> None:
    store   = _store(tmp_path)
    entries = [_entry(n, "point", f"第{n}条内容写得挺认真的呢") for n in range(1, 9)]
    entries[2] = _entry(3, "todo", "还没核实的一条", check="词")
    store.works.append(_work(intro="我整理的独立游戏", entries=entries))
    text = _chat(store, "把你整理的片单发我看看")["life_lines"]
    assert "你自己整理的《独立游戏片单》（你写的东西，可以照着发，也可以挑几条说）：" in text
    assert "我整理的独立游戏" in text and "- （待核实）还没核实的一条" in text
    assert "第6条内容" in text and "第7条内容" not in text  # ≤6 条
    block = works.excerpt_block(store.works[0])
    assert len(block) <= 360
    long = _work(entries=[_entry(n, "point", "很长的条目" * 15) for n in range(1, 7)])
    assert len(works.excerpt_block(long)) <= 360
    for phrase in ("发我看看", "给我看看", "让我看看", "发来看看", "你写的东西呢", "片单发我"):
        assert tc._SHOW_RE.search(phrase), phrase
    assert (
        "你自己整理的" not in _chat(store, "今天吃啥")["life_lines"]
        if _chat(store, "今天吃啥")
        else True
    )
    store.works[0]["status"] = "shelved"
    assert (
        "你自己整理的"
        not in (_chat(store, "把你整理的片单发我看看") or {"life_lines": ""})["life_lines"]
    )


async def test_work_share_candidate_comes_after_episodes_and_is_capped_at_three(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.works.append(
        _work(last_ops=["星露谷物语：种田很治愈"], last_add_at=NOW - 3600, entries=[_entry(1)])
    )
    store.episodes += [
        _episode(n, share=True, ago=600.0 * n, title=f"分享{n}") for n in range(1, 4)
    ]
    got = _chat(store, "", mode="proactive")["share_candidates"]
    # 作品刚更新就留一个位置：经历最多 2 条 + 作品 1 条，总数仍 ≤3
    assert [c["episode_id"] for c in got] == ["e1", "e2", "work:w1:2026-11-05"]
    store.episodes.clear()
    got = _chat(store, "", mode="proactive")["share_candidates"]
    assert got == [
        {
            "episode_id": "work:w1:2026-11-05",
            "line"      : "你在整理的《独立游戏片单》刚更新了：星露谷物语：种田很治愈",
            "topics"    : ["独立游戏"],
        }
    ]
    store.episodes += [_episode(1, share=True, title="分享1")]
    mixed = _chat(store, "", mode="proactive")["share_candidates"]
    assert [c["episode_id"] for c in mixed] == ["e1", "work:w1:2026-11-05"]
    assert (
        _chat(store, "", mode="proactive", exclude=["work:w1:2026-11-05"])["share_candidates"][0][
            "episode_id"
        ]
        == "e1"
    )
    store.works[0]["last_add_at"] = NOW - 25 * 3600  # 24 小时前
    assert [c["episode_id"] for c in _chat(store, "", mode="proactive")["share_candidates"]] == [
        "e1"
    ]
    assert works.share_candidate([_work(last_add_at=NOW - 60, last_ops=[])], MOMENT) is None


# ───────────────────────────── 档案 ─────────────────────────────


def _thread(**kw: Any) -> dict[str, Any]:
    return {
        "id"        : "t1", "title": "星空为什么拖线", "topic": "天文摄影", "why": "拍星空总是拖线",
        "status"    : "open", "steps": 0, "notes": [], "takeaway": "", "created_at": NOW - 20 * DAY,
        "updated_at": NOW - 10 * DAY, "corrections": [], "park_reason": "", "drop_reason": "", **kw,
    }  # fmt: skip


async def test_sync_builds_a_dossier_from_threads_and_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.threads += [
        _thread(),
        _thread(
            id         = "t2",
            title      = "赤道仪怎么调",
            status     = "done",
            takeaway   = "先调水平",
            updated_at = NOW - 5 * DAY,
        ),
        _thread(id="t3", title="暗电流", status="parked", park_reason="资料不太够"),
        _thread(id="t4", title="没话题的", topic=""),
    ]
    dossier.sync(store, make_cfg(), NOW)
    entry = store.dossiers["天文摄影"]
    assert entry["started_at"] == NOW - 20 * DAY and entry["origin"] == "拍星空总是拖线"
    by_id = {q["thread_id"]: q for q in entry["questions"]}
    assert set(by_id) == {"t1", "t2", "t3"}
    assert by_id["t2"]["takeaway"] == "先调水平" and by_id["t3"]["reason"] == "资料不太够"
    assert entry["last_active"] == NOW - 5 * DAY
    snapshot = copy.deepcopy(store.dossiers)
    dossier.sync(store, make_cfg(), NOW)
    dossier.sync(store, make_cfg(), NOW + 1)
    assert store.dossiers == snapshot  # 幂等：重复同步不变
    # 状态变化会更新同一条，不新增
    store.threads[0].update(status="dropped", drop_reason="不查了", updated_at=NOW)
    dossier.sync(store, make_cfg(), NOW)
    again = {q["thread_id"]: q for q in store.dossiers["天文摄影"]["questions"]}
    assert (
        len(again) == 3 and again["t1"]["status"] == "dropped" and again["t1"]["reason"] == "不查了"
    )


async def test_dossier_keeps_its_own_snapshots_after_threads_and_episodes_are_pruned(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.threads.append(
        _thread(
            corrections = [{"before": "怕冷", "after": "安全感", "at": NOW - 3 * DAY}],
            status      = "done",
            takeaway    = "收获",
        )
    )
    store.episodes.append(_episode(1, title="拍星空教程", topics=["天文摄影"]))
    dossier.sync(store, make_cfg(), NOW)
    store.threads.clear()
    store.episodes.clear()
    dossier.sync(store, make_cfg(), NOW)
    entry = store.dossiers["天文摄影"]
    assert (
        entry["questions"][0]["takeaway"] == "收获" and entry["corrections"][0]["after"] == "安全感"
    )
    assert (
        entry["highlights"][0]["title"] == "拍星空教程"
        and entry["highlights"][0]["episode_id"] == "e1"
    )


async def test_highlights_keep_first_two_and_latest_four(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.threads.append(_thread())
    store.episodes += [
        _episode(n, title=f"星空{n}", topics=["天文摄影"], ago=(20 - n) * DAY * 0.5)
        for n in range(1, 11)
    ]
    store.episodes.append(_episode(11, title="不喜欢的", topics=["天文摄影"], feeling="dislike"))
    store.episodes.append(_episode(12, title="别的话题", topics=["做饭"]))
    store.episodes.append(_episode(13, title="自动的", topics=["天文摄影"], auto=True))
    dossier.sync(store, make_cfg(), NOW)
    ids = [h["episode_id"] for h in store.dossiers["天文摄影"]["highlights"]]
    assert ids == ["e1", "e2", "e7", "e8", "e9", "e10"]
    # 之后经历被清理，旧的 6 条仍在；新的喜欢经历按“最早 2 + 最近 4”滚动
    store.episodes.clear()
    store.episodes.append(_episode(20, title="新的", topics=["天文摄影"], ago=60.0))
    dossier.sync(store, make_cfg(), NOW)
    ids = [h["episode_id"] for h in store.dossiers["天文摄影"]["highlights"]]
    assert ids == ["e1", "e2", "e8", "e9", "e10", "e20"]
    # 没有档案的话题不建档案
    assert set(store.dossiers) == {"天文摄影"}


async def test_correction_dedup_cap_and_work_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fixes = [{"before": f"旧{n}", "after": f"新{n}", "at": NOW - (10 - n) * DAY} for n in range(8)]
    store.threads.append(_thread(corrections=fixes))
    store.works += [_work(id=f"w{n}", topic="天文摄影", title=f"作品{n}") for n in range(6)]
    dossier.sync(store, make_cfg(), NOW)
    entry = store.dossiers["天文摄影"]
    assert len(entry["corrections"]) == 6 and entry["corrections"][-1]["after"] == "新7"
    assert len(entry["works"]) == 4 and entry["works"][-1]["title"] == "作品5"
    entry["corrections"][-1]["last_suggested"] = NOW
    dossier.sync(store, make_cfg(), NOW)
    assert entry["corrections"][-1]["last_suggested"] == NOW  # 同步不覆盖冷却时间
    store.works[-1]["status"] = "done"
    dossier.sync(store, make_cfg(), NOW)
    assert entry["works"][-1]["status"] == "done"


def _topic(n: int) -> str:
    return f"{chr(0x4E00 + n * 37)}{chr(0x4E10 + n * 53)}{chr(0x5000 + n * 41)}"


async def test_dossier_retention_and_total_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.threads += [
        _thread(id=f"t{n}", topic=_topic(n), updated_at=NOW - n * 1000.0) for n in range(70)
    ]
    dossier.sync(store, make_cfg(), NOW)
    assert len(store.dossiers) == 60
    assert _topic(69) not in store.dossiers and _topic(0) in store.dossiers
    # 超过 dossier_keep_days 不活跃的删除
    store.threads.clear()
    dossier.sync(store, make_cfg(dossier_keep_days=30), NOW + 40 * DAY)
    assert store.dossiers == {}
    keep = _store(tmp_path / "k")
    keep.threads.append(_thread(updated_at=NOW - 100 * DAY, created_at=NOW - 110 * DAY))
    dossier.sync(keep, make_cfg(), NOW)
    assert "天文摄影" in keep.dossiers
    dossier.sync(keep, make_cfg(dossier_keep_days=60), NOW)
    assert keep.dossiers == {}


async def test_housekeeping_syncs_before_pruning(tmp_path: Path) -> None:
    from plugins.xiaoqing_life.session import _housekeeping

    store = _store(tmp_path)
    store.threads.append(_thread(status="done", takeaway="查明白了", updated_at=NOW - 40 * DAY))
    _housekeeping(store, make_cfg(), NOW)
    assert store.threads == []  # 30 天的保留期过了
    assert store.dossiers["天文摄影"]["questions"][0]["takeaway"] == "查明白了"


def _dossier_store(tmp_path: Path) -> LifeStore:
    store = _store(tmp_path)
    store.threads += [
        _thread(status="done", takeaway="加长曝光就能拍到", title="星空为什么拖线", updated_at=NOW - 2 * DAY,
                corrections=[{"before": "怕冷才拖线", "after": "地球自转", "at": NOW - 4 * DAY}]),
        _thread(id="t2", title="赤道仪怎么调", status="parked", park_reason="资料不太够", updated_at=NOW - 6 * DAY),
        _thread(id="t3", title="暗场怎么拍", status="dropped", drop_reason="没器材", updated_at=NOW - 7 * DAY),
        _thread(id="t4", title="叠加是什么", status="open", updated_at=NOW - 8 * DAY),
    ]  # fmt: skip
    store.works.append(
        _work(
            topic      = "天文摄影",
            title      = "星空片单",
            status     = "done",
            done_at    = NOW - DAY,
            updated_at = NOW - DAY,
        )
    )
    store.episodes.append(
        _episode(1, title="银河拍摄教程", topics=["天文摄影"], ago=20 * DAY, reaction="太美了")
    )
    dossier.sync(store, make_cfg(), NOW)
    return store


async def test_recall_lines_use_the_exact_formats(tmp_path: Path) -> None:
    store   = _dossier_store(tmp_path)
    started = tc.datetime.fromtimestamp(NOW - 20 * DAY, MOMENT.tzinfo)
    text    = _chat(store, "你上次查的天文摄影后来怎么样了")["life_lines"]
    assert (
        f"关于天文摄影（{started.month}月{started.day}日开始留意的，起因：拍星空总是拖线）："
        in text
    )
    assert "- 查过：星空为什么拖线（查明白了：加长曝光就能拍到）" in text
    assert "- 查过：赤道仪怎么调（查到一半资料不太够，先放着了）" in text
    assert "- 查过：暗场怎么拍（不查了：没器材）" in text
    assert "叠加是什么" not in text  # ≤3 条，最近的在前
    assert "- 后来改过想法：之前以为怕冷才拖线，后来发现地球自转" in text
    assert "- 整理的东西：《星空片单》（整理完了）" in text
    assert "- 印象深的：「银河拍摄教程」，当时觉得太美了" in text
    block = text.split("\n")
    assert sum(1 for line in block if line.startswith("- 查过")) == 3
    # 档案块出现时不再单独给同一话题的小课题行
    assert "你在查的小事" not in text and "你前几天查过" not in text and "你查过一阵" not in text


async def test_recall_picks_by_overlap_then_falls_back_to_the_latest(tmp_path: Path) -> None:
    store                = _dossier_store(tmp_path)
    store.dossiers["做饭"] = {
        "topic": "做饭", "started_at": NOW - 9 * DAY, "origin": "", "questions": [], "corrections": [],
        "works": [], "highlights": [], "last_active": NOW - 9 * DAY,
    }  # fmt: skip
    # 重合最高的：用最近 2 条和她最近的回复也算
    text = _chat(store, "那个问题后来怎么样了", recent=["聊聊做饭的事", "做饭挺有意思"])[
        "life_lines"
    ]
    assert "关于做饭（" in text and "关于天文摄影" not in text
    text = _chat(store, "那个问题后来呢", bot=["我之前研究过天文摄影来着"])["life_lines"]
    assert "关于天文摄影（" in text
    # 没有重合但明显在追问：取最近活跃的
    assert "关于天文摄影（" in _chat(store, "还在弄那个吗，进展怎么样")["life_lines"]
    # 没有召回词：不给档案块
    assert (
        "关于天文摄影（" not in (_chat(store, "今天天气不错") or {"life_lines": ""})["life_lines"]
    )
    assert dossier.find_recall({}, "x", fallback=True) is None


async def test_correction_line_on_topic_overlap_with_a_six_hour_cooldown(tmp_path: Path) -> None:
    store = _dossier_store(tmp_path)
    store.threads.clear()  # 让小课题行不干扰
    line  = "你在天文摄影上改过一个想法：之前以为怕冷才拖线，后来发现地球自转"
    first = _chat(store, "我最近在学天文摄影")["life_lines"]
    assert line in first
    assert store.dossiers["天文摄影"]["corrections"][0]["last_suggested"] == NOW
    assert line not in (_chat(store, "我最近在学天文摄影") or {"life_lines": ""})["life_lines"]
    store.dossiers["天文摄影"]["corrections"][0]["last_suggested"] = NOW - 7 * 3600
    assert line in _chat(store, "我最近在学天文摄影")["life_lines"]
    assert "改过一个想法" not in (_chat(store, "今天天气不错") or {"life_lines": ""})["life_lines"]
    # 召回时只给档案块，不重复给更正行
    store.dossiers["天文摄影"]["corrections"][0]["last_suggested"] = 0.0
    recalled = _chat(store, "天文摄影后来怎么样了")["life_lines"]
    assert "改过一个想法" not in recalled and "后来改过想法" in recalled


async def test_dossier_commands(tmp_path: Path) -> None:
    store = _dossier_store(tmp_path)
    assert "天文摄影：问题 4 个，作品 1 个" in commands.dossier_text(store, "", MOMENT)
    shown = commands.dossier_text(store, "天文摄影", MOMENT)
    assert shown.startswith("关于天文摄影（") and "印象深的" in shown
    assert commands.dossier_text(store, "不存在的话题", MOMENT) == "没有这个话题的档案。"
    assert commands.dossier_text(_store(tmp_path / "e"), "", MOMENT) == "还没有主题档案。"
    assert commands.works_text(_store(tmp_path / "w")) == "还没有作品。"
    store.works[0]["entries"] = [_entry(1, "todo", "待核实的"), _entry(2, "point", "确定的")]
    text                      = commands.works_text(store)
    assert (
        "[done] 《星空片单》（片单，天文摄影，整理 0 次，2 条）" in text
        and "（待核实）待核实的" in text
    )
    status = commands.status_text(store, make_cfg(), planner.Presence("free", "", None), MOMENT)
    assert "作品 1 个，档案 1 个" in status
    assert commands.route("works") == ("works", "") and commands.route("作品") == ("works", "")
    assert commands.route("dossier 天文") == ("dossier", "天文") and commands.route("档案") == (
        "dossier",
        "",
    )


async def test_manifest_lists_works_and_dossier_subcommands() -> None:
    manifest = json.loads(
        (Path(__file__).parents[3] / "plugins" / "xiaoqing_life" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    subs = {s["name"]: s for s in manifest["commands"][0]["subcommands"]}
    assert subs["works"]["aliases"] == ["作品"] and subs["dossier"]["aliases"] == ["档案"]
    assert all(subs[n]["permission"] == "bot_admin" for n in ("works", "dossier"))


async def test_works_and_dossiers_collections_persist(tmp_path: Path) -> None:
    store = _dossier_store(tmp_path)
    await store.flush()
    again = LifeStore(tmp_path / "life")
    again.load()
    assert again.works[0]["id"] == "w1" and "天文摄影" in again.dossiers


# ───────────────────────────── 780 字预算 ─────────────────────────────


def _crowded(tmp_path: Path) -> LifeStore:
    store = _dossier_store(tmp_path)
    store.works[0].update(
        status  = "drafting",
        entries = [_entry(n, "point", f"第{n}条内容写得挺认真的呢") for n in range(1, 7)],
        intro   = "我整理的东西",
    )
    for n in range(2, 6):
        store.episodes.append(
            _episode(
                n,
                title  = "一个很长很长的标题" * 3,
                topics = ["天文摄影"],
                ago    = 60.0 * n,
                gist   = "讲了很多很多内容" * 5,
            )
        )
    store.learnings.append(
        {
            "id"        : "l1",
            "topic"     : "天文摄影",
            "text"      : "曝光时间别太长否则星星拖线",
            "created_at": NOW - DAY,
            "last_used" : 0.0,
        }
    )
    store.state["diary"] = {
        "day" : "2026-11-05",
        "text": "今天大概看了不少天文摄影的东西，也写了点笔记。",
    }
    store.state["current"] = {
        "choice": "hot_scroll",
        "source": "bili_popular",
        "start" : NOW - 60,
        "end"   : NOW + 600,
    }
    return store


async def test_life_lines_stay_within_the_budget_and_keep_header_and_guide(tmp_path: Path) -> None:
    store = _crowded(tmp_path)
    for text in (
        "你最近在干嘛，天文摄影后来怎么样了",
        "把你整理的片单发我看看，你最近刷到啥了",
        "最近学到啥，你怎么知道的，等会干嘛",
    ):
        got = _chat(store, text)["life_lines"]
        assert len(got) <= tc.LIFE_BUDGET, (len(got), text)
        assert got.startswith(tc._LIFE_HEADER) and got.endswith(tc._GUIDE)


async def test_budget_drops_whole_lower_priority_parts_in_order(tmp_path: Path) -> None:
    store = _crowded(tmp_path)
    full  = _chat(store, "你最近在干嘛，天文摄影后来怎么样了")
    text  = full["life_lines"]
    # 高优先级的在：召回块 > 正在做 > 经历
    assert "关于天文摄影（" in text and "你这会儿正在刷B站热门" in text
    assert full["episode_ids"] == [] or "银河拍摄教程" in text or "一个很长很长" in text
    # 想看作品时，摘录排在最前面，且一整块都在
    shown = _chat(store, "把你整理的片单发我看看，你最近在干嘛")["life_lines"]
    assert "你自己整理的《星空片单》" in shown and "- 第1条内容写得挺认真的呢" in shown
    # 被丢弃的是整段而不是半段：任何一条被保留的经历行都是完整的
    for line in shown.split("\n"):
        if line.startswith("- ") and "点开" in line:
            assert (
                line.endswith(("。", "的", ")", "）")) or "你当时觉得" in line or "讲的是" in line
            )


async def test_budget_unit_priority_and_episode_ids() -> None:
    store = LifeStore(Path("."))
    store.episodes += [
        _episode(n, title="标题" * 20, gist="内容" * 30, ago=60.0 * n) for n in range(1, 4)
    ]
    store.state["diary"]   = {"day": "2026-11-05", "text": "日记" * 30}
    store.state["current"] = {
        "choice": "hot_scroll",
        "source": "bili_popular",
        "start" : NOW - 60,
        "end"   : NOW + 600,
    }
    payload = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : "你最近在干嘛",
        "recent"       : [],
        "bot_recent"   : [],
        "unknown_terms": [],
    }
    req = tc.clean_payload(payload)
    text, ids = tc.build_life_lines(store, req, MOMENT, make_cfg())
    assert (
        len(text) <= tc.LIFE_BUDGET
        and text.startswith(tc._LIFE_HEADER)
        and text.endswith(tc._GUIDE)
    )
    assert "正在刷B站热门" in text  # “正在做”优先于经历
    kept = sum(1 for line in text.split("\n") if line.startswith("- "))
    assert ids == (
        [e["id"] for e in sorted(store.episodes, key=lambda e: -e["at"])[:3]] if kept else []
    )
    assert tc.LIFE_BUDGET == 780


# ───────────────────────────── 复查补充 ─────────────────────────────


async def test_first_tidy_can_use_the_episodes_the_work_was_started_from() -> None:
    work = _work(created_at=NOW, last_op_at=0.0)
    mine = [_episode(1, ago=3 * 86400.0), _episode(2, ago=40 * 86400.0)]
    # 从没整理过：往前看 30 天（开作品的依据就是这些经历）
    assert [e["id"] for e in works.related_episodes(work, mine)] == ["e1"]
    # 已经引用过的经历不再算新材料
    work["entries"]                   = [_entry(1)]
    work["entries"][0]["episode_ids"] = ["e1"]
    assert works.related_episodes(work, mine) == []


async def test_a_tidy_without_changes_still_consumes_the_material(tmp_path: Path) -> None:
    work = _work(last_op_at=NOW - 7200, last_review_at=NOW - 60)
    mine = [_episode(1, ago=3600.0)]
    assert works.related_episodes(work, mine) == []  # 上次已经看过它、选择不加
    work["last_review_at"] = 0.0
    assert [e["id"] for e in works.related_episodes(work, mine)] == ["e1"]
    # 自动搁置只看真正的改动时间
    old = _work(last_op_at=NOW - 22 * 86400, last_review_at=NOW - 60)
    works.housekeep([old], NOW)
    assert old["status"] == "shelved"
