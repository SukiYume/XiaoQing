"""14 天可控时钟模拟（DESIGN §9.5）：假来源让话题 A 总被喜欢、B 总被讨厌，某来源总失败，晚上 19 点收获高。

断言：偏好倒向 A 和高收获活动、失败来源降权后恢复、高收获时段形成习惯、frontier 从标签长出并被试过、
小课题收获变成 learnings 且后续问题加深、社交反馈把一个词变成 habit 而把另一个压下去。
用 ``pytest -s`` 可以看到“之前 → 之后”的数值。
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import lifecycle as lc
from plugins.xiaoqing_life import planner, reinforce, wind_down
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.session import SessionDeps, run_session
from plugins.xiaoqing_life.sources.base import SourceError
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.plugins.xiaoqing_life.support import (
    FREE,
    SHANGHAI,
    FakeFetcher,
    comments_payload,
    make_cfg,
)

START = datetime(2026, 11, 2, 0, 0, tzinfo=SHANGHAI)  # 周一
SLOTS = (12, 19, 22)
DAYS  = 14
DAY   = 86400.0
HOUR  = 3600.0


class World:
    """假世界：来源返回什么、模型怎么“喜欢”，都由时钟和话题决定。"""

    def __init__(self) -> None:
        self.now         = START
        self.serial      = 0
        self.zhihu_calls = 0

    def video(self, title: str, tags: list[str]) -> dict[str, Any]:
        self.serial += 1
        return {
            "id"         : self.serial,
            "bvid"       : f"BV{self.serial}",
            "aid"        : self.serial,
            "title"      : f"{title}{self.serial}",
            "desc"       : "简介",
            "description": "简介",
            "tag"        : ",".join(tags),
            "play"       : 5000,
            "stat"       : {"view": 5000},
            "owner"      : {"name": "up"},
            "tname"      : "科技",
        }

    def search(self, params: dict[str, str]) -> dict[str, Any]:
        keyword = params["keyword"]
        if "天文" in keyword or "赤道仪" in keyword:
            rows = [self.video("天文", ["赤道仪", "星空摄影"]) for _ in range(3)]
        elif "美食" in keyword:
            rows = [self.video("美食", ["小吃"]) for _ in range(3)]
        else:
            rows = [self.video("随机", ["杂谈"]) for _ in range(3)]
        return {"code": 0, "data": {"result": rows}}

    def popular(self, _params: dict[str, str] | None) -> dict[str, Any]:
        return {"code": 0, "data": {"list": [self.video("无聊", ["日常"]) for _ in range(3)]}}

    def zhihu(self, _params: dict[str, str] | None) -> dict[str, Any]:
        self.zhihu_calls += 1
        raise SourceError("zhihu_hot", "Timeout")

    def feeling(self, title: str) -> str:
        if self.now.hour == 19:  # 晚上 19 点看什么都开心：高收获时段
            return "like"
        if title.startswith("天文"):
            return "like"
        if title.startswith(("美食", "无聊")):
            return "dislike"
        return "meh"

    async def complete(self, route: str, messages: list[dict[str, str]]) -> str:
        items  = json.loads(messages[1]["content"])["untrusted_material"]["items"]
        looked = [
            {
                "item"    : row["id"],
                "reaction": "随手一看",
                "feeling" : self.feeling(row["title"]),
                "topics"  : [row["title"].rstrip("0123456789")],
            }
            for row in items[:2]
        ]
        return json.dumps({"looked_at": looked})


def make_fetch(world: World) -> FakeFetcher:
    return FakeFetcher(
        {
            "/search/type"       : world.search,
            "/popular"           : world.popular,
            "hot-lists"          : world.zhihu,
            "/reply/main"        : comments_payload(),
            "/web-interface/view": {"code": -404},
            "/tag/archive"       : {"code": -404},
        }
    )


async def run_days(tmp_path: Path) -> tuple[LifeStore, World, dict[str, Any]]:
    world = World()
    store = LifeStore(tmp_path / "life")
    store.load()
    cfg = make_cfg(
        seed_interests = ["天文摄影", "地方美食"],
        sources        = {
            "bili_popular" : True,
            "zhihu_hot"    : True,
            "bili_ranking" : False,
            "weibo_hot"    : False,
            "baidu_hot"    : False,
            "bili_comments": True,
            "baike_lookup" : False,
        },
        max_sessions_per_day    = 10,
        max_llm_calls_per_day   = 100,
        session_probability     = 1.0,
        min_session_gap_minutes = 0,
        activity_limit_minutes  = 1000,
        activity_window_minutes = 1440,
    )
    rng                   = random.Random(20261102)
    fetch                 = make_fetch(world)
    trace: dict[str, Any] = {
        "early"    : {"dive": 0, "hot": 0},
        "late"     : {"dive": 0, "hot": 0},
        "max_until": 0.0,
    }
    for day in range(DAYS):
        for hour in SLOTS:
            world.now = START + timedelta(days=day, hours=hour)
            moment   = world.now
            decision = planner.decide(
                cfg,
                store.state,
                store.episodes,
                store.threads,
                store.interests,
                moment,
                FREE,
                rng,
                manual   = True,
                frontier = store.frontier,
            )
            if decision.choice not in ("hot_scroll", "interest_dive", "thread_continue"):
                continue
            window = "early" if day < DAYS // 2 else "late"
            trace[window]["dive" if decision.choice == "interest_dive" else "hot"] += 1
            deps = SessionDeps(fetch=fetch, complete=world.complete, rng=rng, now=lambda: world.now)
            await run_session(store, cfg, decision, deps)
            trace.setdefault("sessions", []).append(
                (
                    decision.choice,
                    str(decision.params.get("query", "")),
                    store.state.get("last_reward"),
                    day,
                )
            )
            until = store.state.get("source_health", {}).get("zhihu_hot", {}).get("until", 0.0)
            trace["max_until"] = max(trace["max_until"], until)
    return store, world, trace


@pytest.mark.asyncio
async def test_fourteen_days_of_feedback_shape_choices_habits_and_curiosity(tmp_path: Path) -> None:
    store, world, trace = await run_days(tmp_path)
    state = store.state
    end = (START + timedelta(days=DAYS)).timestamp()
    summary: dict[str, Any] = {}

    # 1. 偏好倒向收获高的活动和话题 A
    sessions = trace["sessions"]
    mean = lambda rows: sum(r for _c, _q, r, _d in rows) / max(1, len(rows))  # noqa: E731
    dives = [x for x in sessions if x[0] == "interest_dive" and x[2] is not None]
    hots = [x for x in sessions if x[0] == "hot_scroll" and x[2] is not None]
    summary["mean reward dive / hot"] = (round(mean(dives), 2), round(mean(hots), 2))
    assert mean(dives) > mean(hots)
    prefs                         = state["preferences"]["activity"]
    summary["activity EMA (end)"] = {k: round(v, 2) for k, v in prefs.items()}
    assert {"interest_dive", "hot_scroll"} <= set(prefs) and all(
        -1.0 <= v <= 3.0 for v in prefs.values()
    )
    late_dives = [x for x in dives if x[3] >= DAYS // 2]
    a_count = sum("天文" in q for _c, q, _r, _d in late_dives)
    b_count = sum("美食" in q for _c, q, _r, _d in late_dives)
    a_score = lc.interest_score(store.interests["天文摄影"], end)
    b_score = lc.interest_score(store.interests.get("地方美食", {"base": 0, "evidence": []}), end)
    summary["late dives A vs B"] = (a_count, b_count)
    summary["interest score A vs B"] = (round(a_score, 2), round(b_score, 2))
    assert a_count > b_count and a_score > b_score
    # 同样的奖励反复进来，打分会把选择推向高收获的活动（之前 → 之后）
    tuned = {"preferences": {}}
    base_cfg = make_cfg(choice_temperature=8.0)

    def dive_share(state_: dict[str, Any]) -> float:
        picks = [
            planner.choose(
                base_cfg,
                {**default_state(), **state_},
                [],
                {},
                START + timedelta(hours=14),
                random.Random(n),
            ).choice
            for n in range(300)
        ]
        return picks.count("interest_dive") / 300

    before = dive_share(tuned)
    for _ in range(8):
        reinforce.update_preference(tuned, "activity", "interest_dive", 2.0)
        reinforce.update_preference(tuned, "activity", "hot_scroll", -1.0)
    after                                                    = dive_share(tuned)
    summary["interest_dive share of choices before → after"] = (round(before, 2), round(after, 2))
    assert after > before + 0.25

    # 2. 总失败的来源降权，过 6 小时自动恢复
    assert world.zhihu_calls >= 2
    until            = state["source_health"]["zhihu_hot"]["until"]
    summary["zhihu"] = {"calls": world.zhihu_calls, "max_cut_until_set": trace["max_until"] > 0}
    assert trace["max_until"] > 0
    assert reinforce.health_factor(state, "zhihu_hot", until - 1) == 0.2
    assert reinforce.health_factor(state, "zhihu_hot", until + 1) == 1.0
    assert reinforce.health_factor(state, "zhihu_hot", START.timestamp()) in (0.2, 1.0)
    cfg = make_cfg()
    cut = planner._enabled_hot_sources(cfg, state, until - 1)["zhihu_hot"]
    restored = planner._enabled_hot_sources(cfg, state, until + 1)["zhihu_hot"]
    summary["zhihu weight cut → restored"] = (round(cut, 3), round(restored, 3))
    assert cut < restored * 0.5

    # 3. 晚上 19 点收获高 → 形成习惯
    habits       = state["habits"]
    overall      = sum(b["sum"] for b in habits.values()) / sum(b["n"] for b in habits.values())
    evening_mean = habits["wd19"]["sum"] / habits["wd19"]["n"]
    multiplier = reinforce.habit_multiplier(state, START + timedelta(hours=19))
    summary["habit wd19 mean vs overall"] = (round(evening_mean, 2), round(overall, 2))
    summary["habit multiplier 1.0 → "]    = round(multiplier, 3)
    assert habits["wd19"]["n"] >= 3 and evening_mean > overall
    assert 1.0 < multiplier <= 1.3
    assert "19点" in reinforce.habit_text(state)
    summary["habit text"] = reinforce.habit_text(state)

    # 4. frontier 从视频标签长出，并被试过
    topics              = {f["topic"] for f in store.frontier}
    summary["frontier"] = sorted(topics)[:8]
    assert {"赤道仪", "星空摄影"} & topics
    assert len(store.frontier) <= 50
    summary["frontier tried during the run"] = sum(f["tried"] for f in store.frontier)
    target                                   = store.frontier[0]
    target["tried"]                          = False
    deps                                     = SessionDeps(
        fetch    = make_fetch(world),
        complete = world.complete,
        rng      = random.Random(1),
        now      = lambda: world.now,
    )
    world.now = START + timedelta(days=DAYS, hours=12)
    await run_session(
        store,
        make_cfg(),
        planner.Decision(
            "interest_dive",
            "s",
            {},
            {"query": target["topic"], "explored": True, "frontier": target["topic"]},
        ),
        deps,
    )
    assert next(f for f in store.frontier if f["topic"] == target["topic"])["tried"] is True
    summary["frontier tried"] = target["topic"]
    assert end > 0

    # 5. 小课题收获变成 learnings，后续问题逐层加深（最多 3 层）
    store.episodes.append(
        {
            "id"      : "ev0",
            "at"      : world.now.timestamp() - 600,
            "day"     : world.now.strftime("%Y-%m-%d"),
            "title"   : "视频",
            "reaction": "好",
            "feeling" : "like",
            "gist"    : "",
            "topics"  : [],
        }
    )
    parent = "t0"
    store.threads.append(
        {
            "id"            : "t0",
            "title"         : "课题0",
            "why"           : "",
            "next_query"    : "q0",
            "status"        : "reviewing",
            "steps"         : 1,
            "empty_streak"  : 0,
            "cooldown_until": 0.0,
            "notes"         : [{"episode_id": "ev0", "note": "n"}],
            "takeaway"      : "",
            "created_at"    : 0.0,
            "updated_at"    : world.now.timestamp(),
            "parent_id"     : "",
            "depth"         : 0,
        }
    )
    depths: list[int] = []
    for night in range(5):
        night_moment = world.now + timedelta(days=night, hours=11)
        day = night_moment.strftime("%Y-%m-%d")
        store.episodes.append(
            {
                "id"      : f"ev{night + 1}",
                "at"      : night_moment.timestamp() - 600,
                "day"     : day,
                "title"   : "视频",
                "reaction": "好",
                "feeling" : "like",
                "gist"    : "",
                "topics"  : [],
            }
        )
        child = {
            "parent_id" : parent,
            "title"     : f"后续{night + 1}",
            "why"       : "",
            "next_query": f"q{night + 1}",
        }
        reply = {
            "diary"  : {"date": day, "text": "今天。", "episode_ids": [f"ev{night + 1}"]},
            "threads": [
                {
                    "id"         : parent,
                    "action"     : "done",
                    "takeaway"   : f"查明白了第{night}件",
                    "episode_ids": [f"ev{0 if night == 0 else night}"],
                }
            ],
            "followup": child,
        }

        async def complete(route: str, messages: list[dict[str, str]], _reply=reply) -> str:
            return json.dumps(_reply)

        await wind_down.run_wind_down(
            store,
            make_cfg(),
            night_moment,
            complete,
            persona      = "p",
            moment_line  = "",
            reasoning_ok = True,
        )
        new = [t for t in store.threads if t.get("parent_id") == parent]
        if not new:
            break
        depths.append(new[0]["depth"])
        new[0]["status"], new[0]["notes"] = (
            "reviewing",
            [{"episode_id": f"ev{night + 1}", "note": "n"}],
        )
        parent = new[0]["id"]
    summary["learnings"]       = len(store.learnings)
    summary["followup depths"] = depths
    assert len(store.learnings) >= 3 and depths == [1, 2, 3]  # 第 4 层被拒绝
    for text in (row["text"] for row in store.learnings):
        assert text.startswith("查明白了")

    # 6. 社交反馈：一个词变成 habit，另一个被压下去（仍听得懂）
    now = (world.now + timedelta(days=9)).timestamp()
    for name in ("哈基米", "南北绿豆"):
        store.terms[name] = {
            "term"          : name,
            "meaning"       : "含义",
            "usage"         : "场合",
            "tone"          : "玩笑",
            "status"        : "known",
            "verified"      : False,
            "sources"       : ["a", "b"],
            "first_seen"    : now - 5 * DAY,
            "last_seen"     : now - HOUR,
            "last_suggested": now - 60.0,
        }
    moment = datetime.fromtimestamp(now, SHANGHAI)
    for round_ in range(2):
        for name, reactions in (("哈基米", ["哈哈哈确实"]), ("南北绿豆", ["啥意思"])):
            line                                = f"{name}第{round_}次出场"
            store.terms[name]["last_suggested"] = now - 60.0
            payload                             = {
                "version"      : 1,
                "mode"         : "reply",
                "text"         : "继续",
                "recent"       : ["随便聊", line, *reactions, "继续"],
                "bot_recent"   : [line],
                "unknown_terms": [],
            }
            tc.turn_context(store, make_cfg(), payload, moment, random.Random(0))
    good, bad = store.terms["哈基米"], store.terms["南北绿豆"]
    summary["social feedback"] = {
        "哈基米" : (good.get("use_ok", 0), good.get("use_confused", 0)),
        "南北绿豆": (bad.get("use_ok", 0), bad.get("use_confused", 0)),
    }
    assert reinforce.is_habit(good) and not reinforce.is_suppressed(good, now)
    assert reinforce.is_suppressed(bad, now + DAY) and not reinforce.is_habit(bad)
    understood = [
        k
        for k, _ in tc.understood_terms(
            store, tc.clean_payload({"text": "南北绿豆是啥", "recent": []})
        )
    ]
    assert "南北绿豆" in understood
    cfg_use = make_cfg(active_use_probability=1.0)
    store.terms["南北绿豆"]["meaning"] = store.terms["哈基米"]["meaning"] = (
        "看到猫图拍得好可爱时感叹"
    )
    store.terms["南北绿豆"]["last_suggested"] = store.terms["哈基米"]["last_suggested"] = 0.0
    picked = tc.pick_usable_term(
        store,
        {"text": "你家猫图拍得好可爱", "recent": [], "bot_recent": []},
        now + DAY,
        cfg_use,
        random.Random(0),
    )
    assert picked is not None and picked[0] == "哈基米"

    for key, value in summary.items():
        print(f"SIM {key}: {value}")
