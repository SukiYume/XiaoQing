"""入口：定时检查只做门控并登记后台任务、单飞、关闭时取消；服务回调与 /life 命令。"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

import pytest

from plugins.xiaoqing_life import main as life_main
from plugins.xiaoqing_life.persona_bridge import Presence
from tests.plugins.xiaoqing_life.support import (
    FREE,
    FakeAI,
    FakeFetcher,
    at,
    bili_reply,
    bili_video,
    comments_payload,
    fixed_clock,
    make_context,
    popular_payload,
)

MOMENT = at(14, 0)
DIGEST = {
    "looked_at": [{"item": "i1", "reaction": "猫猫好可爱", "feeling": "like", "topics": ["猫"]}],
    "terms"    : [
        {"term": "哈基米", "meaning": "对猫咪的爱称", "quote": "哈基米这也太可爱", "refs": ["c1.1"]}
    ],
}


class Gate:
    """让取材卡住，直到测试放行；用来验证单飞和关闭时取消。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls   = 0

    async def get_json(self, url: str, *, params=None, referer: str = "") -> Any:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return popular_payload(bili_video(1, "猫猫的一天"))


def _fetch() -> FakeFetcher:
    return FakeFetcher(
        {
            "/popular"   : popular_payload(bili_video(1, "猫猫的一天")),
            "/reply/main": comments_payload(bili_reply("哈基米这也太可爱了吧", sub=("哈基米！",))),
        }
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    state = {"presence": FREE, "fetch": _fetch()}
    monkeypatch.setattr(life_main.persona_bridge, "availability", lambda *_a: state["presence"])
    monkeypatch.setattr(life_main.persona_bridge, "persona_facts", lambda _c: "你是小青。")
    monkeypatch.setattr(life_main, "BoundedFetcher", lambda _session, *_a, **_k: state["fetch"])
    return state


def _context(tmp_path: Path, ai: FakeAI | None = None, **life: Any):
    context = make_context(tmp_path, life={"session_probability": 1.0, **life}, ai=ai)
    runtime       = life_main.get_runtime(context)
    runtime.clock = fixed_clock(MOMENT)
    runtime.rng   = random.Random(3)
    return context, runtime


@pytest.mark.asyncio
async def test_tick_returns_immediately_and_the_session_runs_in_a_tracked_task(
    tmp_path: Path, patched
) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST))
    gate             = Gate()
    patched["fetch"] = gate
    await asyncio.wait_for(life_main.scheduled_tick(context), timeout=1)  # 取材卡住也不挡定时回调
    assert runtime.running and runtime.task is not None
    await asyncio.wait_for(gate.entered.wait(), timeout=1)
    gate.release.set()
    await asyncio.wait_for(runtime.task, timeout=2)
    assert not runtime.running and len(runtime.store.episodes) == 1
    assert runtime.store.state["sessions_today"] == 1


@pytest.mark.asyncio
async def test_only_one_session_is_in_flight(tmp_path: Path, patched) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST), min_session_gap_minutes=0)
    gate             = Gate()
    patched["fetch"] = gate
    await life_main.scheduled_tick(context)
    await asyncio.wait_for(gate.entered.wait(), timeout=1)
    first = runtime.task
    await life_main.scheduled_tick(context)
    await life_main.scheduled_tick(context)
    assert runtime.task is first and gate.calls == 1
    gate.release.set()
    await asyncio.wait_for(first, timeout=2)
    assert runtime.store.state["sessions_today"] == 1


@pytest.mark.asyncio
async def test_disabled_or_asleep_ticks_start_nothing_and_write_no_records(
    tmp_path: Path, patched
) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST), enabled=False)
    await life_main.scheduled_tick(context)
    assert runtime.task is None and runtime.store.state["last_decision"] == {}

    context, runtime = _context(tmp_path / "b", FakeAI(DIGEST))
    patched["presence"] = Presence("asleep", "睡觉", 0)
    await life_main.scheduled_tick(context)
    assert runtime.task is None and runtime.store.episodes == []
    decision = runtime.store.state["last_decision"]
    assert (decision["choice"], decision["reason"]) == ("rest", "asleep")
    assert (
        runtime.store.state["sessions_today"] == 0 and runtime.store.state["llm_calls_today"] == 0
    )


@pytest.mark.asyncio
async def test_wind_down_window_runs_the_tidy_up_as_a_tracked_task_and_browses_nothing(
    tmp_path: Path, patched
) -> None:
    diary = {"diary": {"date": "2026-11-05", "text": "今天刷了猫猫视频。", "episode_ids": ["e1"]}}
    context, runtime = _context(tmp_path, FakeAI(diary))
    runtime.store.episodes.append(
        {
            "id"      : "e1",
            "day"     : "2026-11-05",
            "at"      : MOMENT.timestamp() - 600,
            "title"   : "猫猫的一天",
            "reaction": "好可爱",
            "feeling" : "like",
            "gist"    : "",
        }
    )
    patched["presence"] = Presence("free", "", 20)
    await life_main.scheduled_tick(context)
    assert runtime.task is not None
    await asyncio.wait_for(runtime.task, timeout=2)
    state = runtime.store.state
    assert state["done_on"]["wind_down"] == "2026-11-05" and state["diary"]["episode_ids"] == ["e1"]
    assert state["sessions_today"] == 0 and state["llm_calls_today"] == 1
    # 已整理过：窗口内再检查只是休息
    await life_main.scheduled_tick(context)
    assert runtime.store.state["last_decision"]["reason"] == "winding_down"


@pytest.mark.asyncio
async def test_disabled_tick_does_not_touch_any_state(tmp_path: Path, patched) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST), enabled=False)
    before = (dict(runtime.store.state), dict(runtime.store.interests))
    await life_main.scheduled_tick(context)
    assert (runtime.store.state, runtime.store.interests) == before


@pytest.mark.asyncio
async def test_session_crash_is_contained_and_recorded_as_a_failure(
    tmp_path: Path, patched
) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST))

    class Exploding:
        async def get_json(self, *_a: Any, **_k: Any) -> Any:
            raise ZeroDivisionError

    patched["fetch"] = Exploding()
    await life_main.scheduled_tick(context)
    assert runtime.task is not None
    await asyncio.wait_for(runtime.task, timeout=2)  # 异常不会传出任务
    assert runtime.store.state["last_failure_at"] == MOMENT.timestamp()
    assert runtime.store.episodes == []


@pytest.mark.asyncio
async def test_shutdown_cancels_the_running_session_and_flushes(tmp_path: Path, patched) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST))
    gate             = Gate()
    patched["fetch"] = gate
    await life_main.scheduled_tick(context)
    await asyncio.wait_for(gate.entered.wait(), timeout=1)
    task = runtime.task
    await asyncio.wait_for(life_main.shutdown(context), timeout=2)
    assert task is not None and task.cancelled()
    assert runtime.closing and runtime.spawn(asyncio.sleep(0)) is None  # 关闭后不再接新任务
    await life_main.scheduled_tick(context)
    assert runtime.task is task


@pytest.mark.asyncio
async def test_turn_context_service_is_none_when_disabled_and_never_raises(
    tmp_path: Path, patched
) -> None:
    context, runtime = _context(tmp_path / "off", enabled=False)
    assert await life_main.turn_context({"text": "最近刷到啥"}, context) is None

    context, runtime = _context(tmp_path / "on")
    runtime.store.episodes.append(
        {
            "id"      : "e1",
            "at"      : MOMENT.timestamp() - 3600,
            "day"     : "2026-11-05",
            "source"  : "bili_popular",
            "title"   : "猫猫的一天",
            "reaction": "好可爱",
            "topics"  : [],
            "share"   : False,
        }
    )
    response = await life_main.turn_context(
        {
            "version"      : 1,
            "mode"         : "reply",
            "text"         : "最近刷到啥好玩的",
            "recent"       : [],
            "bot_recent"   : [],
            "unknown_terms": ["绝绝子"],
        },
        context,
    )
    assert response and "猫猫的一天" in response["life_lines"] and response["version"] == 1
    for task in list(runtime._bg):  # 入队的待查词被登记的后台任务落盘
        await task
    assert runtime.store.state["lookup_queue"] == ["绝绝子"]

    runtime.store.episodes[0]["at"] = "坏数据"
    assert await life_main.turn_context({"text": "最近刷到啥好玩的"}, context) is None


@pytest.mark.asyncio
async def test_export_memes_returns_an_empty_page_for_now(tmp_path: Path, patched) -> None:
    context, _ = _context(tmp_path)
    assert await life_main.export_memes({"since": 12.5, "limit": 5}, context) == {
        "memes"     : [],
        "revoked"   : [],
        "next_since": 12.5,
    }
    assert (await life_main.export_memes({"since": "x"}, context))["next_since"] == 0.0
    off, _ = _context(tmp_path / "off", enabled=False)
    assert await life_main.export_memes({"since": 1}, off) is None


async def _life(context: Any, args: str) -> str:
    result = await life_main.handle("life", args, {}, context)
    return "".join(seg["data"]["text"] for seg in result)


@pytest.mark.asyncio
async def test_life_commands(tmp_path: Path, patched) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST))
    status = await _life(context, "")
    assert "网上生活：开启" in status and "今日上网 0/5 次" in status and "此刻空闲" in status
    assert await _life(context, "recent") == "还没有上网经历。"
    assert await _life(context, "slang") == "还没有学到词条。"
    assert await _life(context, "threads") == "没有小课题。"
    assert "梗图" in await _life(context, "memes")
    assert "/life run" in await _life(context, "help")
    assert "未知子命令" in await _life(context, "dance")

    assert "已安排一次上网" in await _life(context, "run")
    assert runtime.task is not None
    await asyncio.wait_for(runtime.task, timeout=2)
    assert "猫猫好可爱" in await _life(context, "recent")
    assert "哈基米" in await _life(context, "slang")
    status = await _life(context, "status")
    assert "今日上网 1/5 次，模型调用 1/30 次" in status and "刷热榜" in status
    assert "已删除词条" in await _life(context, "forget 哈基米")
    assert "没有这个词条" in await _life(context, "forget 哈基米")
    assert "用法" in await _life(context, "forget")


@pytest.mark.asyncio
async def test_life_run_skips_dice_and_gap_but_not_the_switch_or_sleep(
    tmp_path: Path, patched
) -> None:
    context, runtime = _context(tmp_path, FakeAI(DIGEST), session_probability=0.0)
    runtime.store.state["last_session_at"] = MOMENT.timestamp() - 60
    assert "已安排一次上网" in await _life(context, "run")
    await asyncio.wait_for(runtime.task, timeout=2)
    assert "已安排一次上网" in await _life(context, "run")  # 间隔内再来一次也行
    await asyncio.wait_for(runtime.task, timeout=2)
    assert runtime.store.state["sessions_today"] == 2
    patched["presence"] = Presence("asleep", "睡觉", 0)
    assert "不上网：asleep" in await _life(context, "run")
    off, _ = _context(tmp_path / "off", enabled=False)
    assert "没有开启" in await _life(off, "run")
