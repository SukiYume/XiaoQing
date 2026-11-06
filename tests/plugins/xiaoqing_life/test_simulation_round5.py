"""第五轮可控时钟模拟（DESIGN §10.10）。

假来源 + 脚本化的假模型，一条时间线上验证：

1. 一个话题越刷越喜欢 → 睡前整理开一个片单作品 → 几次 ``work_on`` 后有 ≥4 条条目，并作为分享候选被分享一次；
2. 一个小课题研究两轮后 ``stuck`` 搁置，3 天后被再拿起并答出；
3. 其中一条 ``revises`` 成为更正，出现在主题档案的召回里；
4. 小课题 done 时提炼一条方法，下一个相近的小课题用上它，done 后 ``wins`` 增加；
5. 一个话题前一期喜欢、后一期不喜欢 → ``cooling`` 快照。

兴趣走向以 14 天为一期（前一期 [now−28d, now−14d)），“mixed” 看近 21 天，所以第 5 条的“后一期”要放到第 22 天之后：
时间线前 10 天是第 1～4 条，第 22～23 天是第 5 条。用 ``pytest -s`` 可以看到关键数值。
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import dossier, planner, threads, wind_down, works
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.planner import THREAD, WORK_ON, Decision
from plugins.xiaoqing_life.session import SessionDeps, run_session
from plugins.xiaoqing_life.stores import LifeStore
from tests.plugins.xiaoqing_life.support import (
    FREE,
    SHANGHAI,
    FakeFetcher,
    comments_payload,
    make_cfg,
)

START = datetime(2026, 11, 2, 0, 0, tzinfo=SHANGHAI)
GAMES = ("星露谷物语", "空洞骑士", "茶杯头", "蔚蓝", "哈迪斯", "死亡细胞", "泰拉瑞亚", "动物森友会")
DAY   = 86400.0


class World:
    """假世界：来源按关键词出视频；模型按提示种类分发，研究回复由时间线脚本给出。"""

    def __init__(self) -> None:
        self.now                                  = START
        self.serial                               = 0
        self.game_cursor                          = 0
        self.research_script: list[Any]           = []
        self.research_calls: list[dict[str, Any]] = []
        self.dislike_baking                       = False
        self.want_method                          = False

    # ——— 来源 ———
    def video(self, title: str) -> dict[str, Any]:
        self.serial += 1
        return {
            "id"   : self.serial, "bvid": f"BV{self.serial}", "aid": self.serial,
            "title": f"{title}{self.serial}", "desc": "简介", "description": "简介", "tag": "",
            "play" : 5000, "stat": {"view": 5000}, "owner": {"name": "up"}, "tname": "游戏",
        }  # fmt: skip

    def search(self, params: dict[str, str]) -> dict[str, Any]:
        keyword = params["keyword"]
        if "独立游戏" in keyword:
            rows = []
            for _ in range(2):
                game = GAMES[self.game_cursor % len(GAMES)]
                self.game_cursor += 1
                rows.append(self.video(f"{game}独立游戏评测"))
        elif "烘焙" in keyword:
            rows = [self.video("烘焙教程") for _ in range(2)]
        else:
            rows = [self.video("星空拍摄") for _ in range(2)]
        return {"code": 0, "data": {"result": rows}}

    # ——— 模型 ———
    @staticmethod
    def topic_of(title: str) -> str:
        if any(game in title for game in GAMES):
            return "独立游戏"
        if title.startswith("烘焙"):
            return "烘焙"
        return "天文摄影" if title.startswith("星空") else "杂谈"

    def feeling(self, topic: str) -> str:
        if topic == "烘焙":
            return "dislike" if self.dislike_baking else "like"
        return "like" if topic in ("独立游戏", "天文摄影") else "meh"

    async def complete(self, route: str, messages: list[dict[str, str]]) -> str:
        system, user = messages[0]["content"], json.loads(messages[1]["content"])
        if "你在弄明白一个自己好奇的问题" in system:
            self.research_calls.append(user)
            return json.dumps(self.research_script.pop(0)(user), ensure_ascii=False)
        if "你在慢慢整理一件自己的小作品" in system:
            return json.dumps(self.work(user), ensure_ascii=False)
        if "快要睡了" in system:
            return json.dumps(self.wind(user["untrusted_record"]), ensure_ascii=False)
        items  = user["untrusted_material"]["items"]
        looked = [
            {
                "item"   : row["id"], "reaction": "随手一看", "topics": [self.topic_of(row["title"])],
                "feeling": self.feeling(self.topic_of(row["title"])),
            }
            for row in items[:2]
        ]  # fmt: skip
        return json.dumps({"looked_at": looked}, ensure_ascii=False)

    def work(self, user: dict[str, Any]) -> dict[str, Any]:
        have                      = " ".join(e["text"] for e in user["work"]["entries"])
        ops: list[dict[str, Any]] = []
        for episode in user["untrusted_record"]["episodes"]:
            game = next((g for g in GAMES if g in episode["title"]), None)
            if game and game not in have and len(ops) < 3:
                ops.append(
                    {
                        "op"         : "add",
                        "kind"       : "item",
                        "text"       : f"{game}：上手很快",
                        "episode_ids": [episode["id"]],
                    }
                )
                have += game
        total = len(user["work"]["entries"]) + len(ops)
        return {
            "ops": ops, "intro": "我最近迷上的独立游戏", "status": "done" if total >= 6 else "drafting",
        }  # fmt: skip

    def wind(self, record: dict[str, Any]) -> dict[str, Any]:
        episodes              = record["episodes"]
        reply: dict[str, Any] = {
            "diary": {"date": record["date"], "text": "今天刷了点东西。", "episode_ids": [episodes[0]["id"]]},
            "threads": [], "methods": [],
        }  # fmt: skip
        for thread in record["threads"]:
            if thread["status"] != "reviewing":
                continue
            note = thread["notes"][0]["episode_id"]
            reply["threads"].append(
                {
                    "id"         : thread["id"],
                    "action"     : "done",
                    "takeaway"   : f"收获：{thread['title']}",
                    "episode_ids": [note],
                }
            )
            useful = [a["index"] for a in thread["actions"] if a["useful"]]
            if self.want_method and useful:
                reply["methods"].append(
                    {
                        "thread_id" : thread["id"],
                        "text"      : "先搜现象再对照原理",
                        "applies_to": "天文摄影",
                        "actions"   : useful[:2],
                    }
                )
                self.want_method = False
        if "独立游戏" in record.get("work_candidates", []):
            ids           = [e["id"] for e in episodes if any(g in e["title"] for g in GAMES)][:2]
            reply["work"] = {
                "action": "start", "title": "独立游戏片单", "form": "list", "topic": "独立游戏",
                "why"   : "最近越刷越上头", "episode_ids": ids,
            }  # fmt: skip
        return reply


def _look(topic: str = "天文摄影") -> list[dict[str, Any]]:
    return [{"item": "i1", "reaction": "有意思", "feeling": "like", "topics": [topic]}]


def _finding(user: dict[str, Any], text: str, revises: str = "") -> dict[str, Any]:
    item = user["untrusted_material"]["items"][0]
    return {"text": text, "quote": item["title"], "refs": [item["id"]], "revises": revises}


def _fetch(world: World) -> FakeFetcher:
    return FakeFetcher(
        {
            "/search/type"       : world.search, "/reply/main": comments_payload(),
            "/web-interface/view": {"code": -404}, "/tag/archive": {"code": -404},
        }
    )  # fmt: skip


@pytest.mark.asyncio
async def test_round5_timeline_works_research_methods_dossier_and_tone(tmp_path: Path) -> None:
    world = World()
    store = LifeStore(tmp_path / "life")
    store.load()
    cfg   = make_cfg(
        max_sessions_per_day=20, max_llm_calls_per_day=100, session_probability=1.0,
        min_session_gap_minutes=0, activity_limit_minutes=1000, activity_window_minutes=1440,
    )  # fmt: skip
    rng                     = random.Random(20261102)
    fetch                   = _fetch(world)
    summary: dict[str, Any] = {}

    def when(day: int, hour: int) -> datetime:
        world.now = START + timedelta(days=day, hours=hour)
        return world.now

    def deps() -> SessionDeps:
        return SessionDeps(fetch=fetch, complete=world.complete, rng=rng, now=lambda: world.now)

    async def dive(day: int, hour: int, query: str) -> None:
        when(day, hour)
        await run_session(store, cfg, Decision("interest_dive", "s", {}, {"query": query}), deps())

    async def wind(day: int) -> Any:
        moment = when(day, 23)
        return await wind_down.run_wind_down(
            store, cfg, moment, world.complete, persona="p", moment_line="", reasoning_ok=True
        )

    async def research(day: int, hour: int, thread_id: str) -> None:
        when(day, hour)
        decision = Decision(THREAD, "s", {}, {"thread_id": thread_id, "query": "q"})
        await run_session(store, cfg, decision, deps())

    async def work_on(day: int, hour: int) -> None:
        moment   = when(day, hour)
        decision = planner.decide(
            cfg, store.state, store.episodes, store.threads, store.interests, moment, FREE, rng,
            manual=True, frontier=store.frontier, works=store.works,
        )  # fmt: skip
        assert WORK_ON in decision.scores  # 有新材料时规划器给出这个候选
        pick = Decision(WORK_ON, "s", decision.scores, {"work_id": store.works[0]["id"]})
        await run_session(store, cfg, pick, deps())

    # ── 第 0～1 天：独立游戏越刷越喜欢；烘焙在前一期被喜欢 ──
    await dive(0, 11, "烘焙")
    await dive(0, 14, "独立游戏")
    await dive(1, 14, "独立游戏")
    assert (await wind(1)).status == "ok"
    assert len(store.works) == 1 and store.works[0]["topic"] == "独立游戏"
    work_id = store.works[0]["id"]
    assert (
        "独立游戏" in store.dossiers
        and store.dossiers["独立游戏"]["works"][0]["title"] == "独立游戏片单"
    )
    summary["work started"] = store.works[0]["title"]

    # ── 第 2 天：开一个小课题，研究两轮后 stuck 搁置 ──
    await dive(2, 11, "独立游戏")
    when(2, 12)
    t1 = threads.open_thread(
        store.threads, cfg, title="星空为什么拖线", why="拍星空总是拖线", next_query="星空 拖线",
        now=world.now.timestamp(), topic="天文摄影",
    )  # fmt: skip
    assert t1 is not None
    world.research_script = [
        lambda u: {
            "looked_at": _look(), "findings": [_finding(u, "星星拖线是因为相机抖动")],
            "status"   : "progress", "next": {"action": "search", "arg": "星空 曝光"},
        },
        lambda u: {
            "looked_at"    : [], "findings": [], "status": "stuck", "stuck_reason": "资料不够",
            "open_question": "到底是抖动还是自转", "next": {"action": "stop"},
        },
    ]  # fmt: skip
    await research(2, 14, t1["id"])
    assert len(world.research_calls) == 2
    assert t1["status"] == "parked" and t1["park_count"] == 1 and t1["park_reason"] == "资料不够"
    assert t1["parked_until"] == (START + timedelta(days=5, hours=14)).timestamp()
    assert [a["action"] for a in t1["actions"]] == ["search", "search"]
    summary["parked until"] = t1["parked_until"]
    await work_on(2, 15)
    # 第一次整理就能用上开作品时依据的那些经历（往前看 30 天）
    assert len(store.works[0]["entries"]) >= 2

    # ── 第 3 天：搁置中的小课题还不是候选；作品攒到 4 条并被分享一次 ──
    await dive(3, 11, "独立游戏")
    state  = {**store.state}
    moment = when(3, 13)
    scores = planner.choose(
        cfg,
        state,
        store.threads,
        store.interests,
        moment,
        rng,
        works    = store.works,
        episodes = store.episodes,
    ).scores
    assert THREAD not in scores
    await work_on(3, 15)
    assert len(store.works[0]["entries"]) >= 4
    payload = {
        "version"      : 1,
        "mode"         : "proactive",
        "text"         : "",
        "recent"       : [],
        "bot_recent"   : [],
        "unknown_terms": [],
        "exclude_ids"  : [],
    }
    moment     = when(3, 16)
    first      = tc.turn_context(store, cfg, payload, moment, rng)[0]
    work_cards = [c for c in first["share_candidates"] if c["episode_id"].startswith("work:")]
    assert len(work_cards) == 1 and work_cards[0]["episode_id"] == f"work:{work_id}:2026-11-05"
    assert work_cards[0]["line"].startswith("你在整理的《独立游戏片单》刚更新了：")
    # 聊天侧记下已分享的 ID：同一天不会再给
    payload["exclude_ids"] = [work_cards[0]["episode_id"]]
    again                  = tc.turn_context(store, cfg, payload, moment, rng)[0]
    assert again is None or not any(
        c["episode_id"].startswith("work:") for c in again["share_candidates"]
    )
    summary["work entries day 3"] = len(store.works[0]["entries"])

    # ── 第 4 天：作品攒够了，done ──
    await dive(4, 11, "独立游戏")
    await work_on(4, 15)
    assert store.works[0]["status"] == "done" and len(store.works[0]["entries"]) >= 4

    # ── 第 5 天：搁置 3 天后被再拿起，用 revises 改了一个旧想法并答出 ──
    await dive(5, 11, "星空")
    moment    = when(5, 14)
    open_copy = {**t1, "status": "open"}
    scored    = planner.choose(cfg, store.state, [t1], store.interests, moment, rng).scores
    plain     = planner.choose(cfg, store.state, [open_copy], store.interests, moment, rng).scores
    assert THREAD in scored and scored[THREAD] == pytest.approx(plain[THREAD] - 8.0)
    world.research_script = [
        lambda u: {
            "looked_at": _look(), "status": "answered", "next": {"action": "stop"},
            "findings" : [_finding(u, "其实是地球自转，需要赤道仪跟踪", revises="n1")],
        }
    ]  # fmt: skip
    await research(5, 14, t1["id"])
    assert t1["status"] == "reviewing" and t1["park_count"] == 1
    assert (
        len(t1["corrections"]) == 1 and t1["corrections"][0]["before"] == "星星拖线是因为相机抖动"
    )
    world.want_method = True
    assert (await wind(5)).done == 1
    assert t1["status"] == "done" and len(store.methods) == 1
    method = store.methods[0]
    assert method["text"] == "先搜现象再对照原理" and method["uses"] == 0 and method["wins"] == 0
    summary["method"] = method["text"]

    # ── 第 6 天：档案召回里有“后来改过想法” ──
    payload = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : "你之前研究的天文摄影那个问题后来怎么样了",
        "recent"       : [],
        "bot_recent"   : [],
        "unknown_terms": [],
    }
    reply = tc.turn_context(store, cfg, payload, when(6, 12), rng)[0]
    lines = reply["life_lines"]
    assert "关于天文摄影（" in lines
    assert (
        "- 后来改过想法：之前以为星星拖线是因为相机抖动，后来发现其实是地球自转，需要赤道仪跟踪"
        in lines
    )
    assert "- 查过：星空为什么拖线（查明白了：收获：星空为什么拖线）" in lines
    assert len(lines) <= tc.LIFE_BUDGET
    summary["dossier recall"] = [x for x in lines.split("\n") if x.startswith("- 后来")]

    # ── 第 8 天：相近的小课题用上方法，done 后 wins 增加 ──
    when(8, 12)
    t2 = threads.open_thread(
        store.threads, cfg, title="银河怎么拍", why="想拍银河", next_query="银河 拍摄",
        now=world.now.timestamp(), topic="天文摄影",
    )  # fmt: skip
    assert t2 is not None
    world.research_script = [
        lambda u: {
            "looked_at": _look(), "status": "answered", "next": {"action": "stop"},
            "findings" : [_finding(u, "银河要用高感光度加广角")],
        }
    ]  # fmt: skip
    await research(8, 14, t2["id"])
    assert world.research_calls[-1]["methods"] == ["先搜现象再对照原理"]
    assert method["uses"] == 1 and t2["methods_used"] == [method["id"]]
    assert (await wind(8)).done == 1
    assert method["wins"] == 1 and method["status"] == "active"
    summary["method uses/wins"] = (method["uses"], method["wins"])
    assert {q["status"] for q in store.dossiers["天文摄影"]["questions"]} == {"done"}

    # ── 第 22～23 天：烘焙前一期被喜欢，后一期不喜欢 → cooling ──
    assert not [s for s in store.snapshots if s.get("topic") == "烘焙" and s.get("kind") == "tone"]
    world.dislike_baking = True
    await dive(22, 14, "烘焙")
    await dive(23, 14, "烘焙")
    tone = [s for s in store.snapshots if s.get("topic") == "烘焙" and s.get("kind") == "tone"]
    assert [s["tone"] for s in tone] == ["cooling"]
    assert tone[0]["text"] == "前阵子挺爱看烘焙的，这两周没那么上头了"
    summary["tone"] = tone[0]["text"]
    payload         = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : "你最近还爱看烘焙吗",
        "recent"       : [],
        "bot_recent"   : [],
        "unknown_terms": [],
    }
    moment = when(23, 20)
    assert (
        "前阵子挺爱看烘焙的" in tc.turn_context(store, cfg, payload, moment, rng)[0]["life_lines"]
    )

    # 档案与作品跨越了这么久还在（经历和小课题的保留期是它们自己的）
    assert "独立游戏" in store.dossiers and "天文摄影" in store.dossiers
    assert dossier.recall_lines(store.dossiers["天文摄影"], moment, cfg.episode_detail_days)
    assert works.get_work(store.works, work_id)["status"] == "done"
    print("\n[simulation round5]")  # noqa: T201
    for key, value in summary.items():
        print(f"  {key}: {value}")  # noqa: T201
