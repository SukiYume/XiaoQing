"""活动比例：14 天、每 15 分钟一次决策、一直有一个能续做的小课题时，一天的上网不会被它包下。

生产里 5 次上网有 3 次在续做同一个小课题；现在同一个小课题一天只查一次。这里不跑真的上网，
按会话开始和结束时的状态写法（``session._start`` / ``activity.remember_activity``）推进时钟。
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from plugins.xiaoqing_life import planner
from plugins.xiaoqing_life.days import day_key
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.stores import default_state, roll_day
from tests.plugins.xiaoqing_life.support import SHANGHAI, make_cfg

START = datetime(2026, 11, 2, 10, 30, tzinfo=SHANGHAI)
DAYS = 14


def _presence(moment: datetime) -> Presence:
    hour = moment.hour + moment.minute / 60
    if 1.5 <= hour < 10.5:
        return Presence("asleep", "睡觉", 0)
    until = (25.5 - hour) * 60 if hour >= 10.5 else (1.5 - hour) * 60
    return Presence("free", "", int(until))


def _thread() -> dict[str, Any]:
    return {
        "id"        : "t1",
        "title"     : "猫为什么爱纸箱",
        "topic"     : "猫",
        "kind"      : "fact",
        "next_query": "猫 纸箱",
        "status"    : "open",
        "steps"     : 0,
        "notes"     : [],
        "created_at": START.timestamp(),
        "updated_at": START.timestamp(),
    }


def test_two_weeks_with_an_open_thread_keep_a_varied_day() -> None:
    cfg                              = make_cfg()
    state                            = default_state()
    thread                           = _thread()
    rng                              = random.Random(7)
    moment                           = START
    picks  : Counter[str]            = Counter()
    per_day: dict[str, Counter[str]] = {}
    while moment < START + timedelta(days=DAYS):
        day = day_key(moment)
        roll_day(state, day)
        decision = planner.decide(cfg, state, [], [thread], {}, moment, _presence(moment), rng)
        if decision.choice not in (planner.REST, planner.WIND_DOWN):
            low, high = cfg.session_minutes.get(decision.choice, (10, 10))
            start, end = moment.timestamp(), moment.timestamp() + rng.randint(low, high) * 60.0
            state["sessions_today"] = int(state.get("sessions_today", 0)) + 1
            state["last_session_at"], state["last_session_end"] = start, end
            state["activity_log"].append({"start": start, "end": end})
            state["recent"] = [
                *state["recent"],
                {"choice": decision.choice, **decision.params, "at": end},
            ][-12:]
            if decision.choice == planner.THREAD:
                thread["last_session_day"] = day
                thread["updated_at"]       = end  # 一直有进展，但不答完、不卡住
            picks[decision.choice] += 1
            per_day.setdefault(day, Counter())[decision.choice] += 1
        moment += timedelta(minutes=15)
    total = sum(picks.values())
    assert all(c[planner.THREAD] <= 1 for c in per_day.values())  # 同一个小课题一天最多一次
    assert picks[planner.THREAD] / total <= 0.4
    assert picks[planner.HOT_SCROLL] and picks[planner.INTEREST]  # 其余时间照常刷、照常按兴趣搜
    assert total >= DAYS * 2  # 一天至少上两三次网
