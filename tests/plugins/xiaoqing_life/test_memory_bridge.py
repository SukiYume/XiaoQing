"""网上生活发布到统一记忆：稳定 ID、只发游标之后的新条目、全部写成功才移游标、记忆不在时什么也不做。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.xiaoqing_life import memory_bridge
from plugins.xiaoqing_life.config import LifeConfig
from plugins.xiaoqing_life.stores import LifeStore
from plugins.xiaoqing_life.wind_down import validate_diary


class FakeMemory:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail                           = fail
        self.payloads: list[dict[str, Any]] = []

    async def record(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        if self.fail:
            return {"version": 1, "error": "life_global_only"}
        return {"version": 1, "ids": [], "rejected": []}


def _store(tmp_path) -> LifeStore:
    store = LifeStore(tmp_path)
    store.load()
    store.episodes.append(
        {
            "id"       : "e1",
            "at"       : 100.0,
            "day"      : "2026-11-10",
            "source"   : "bili_popular",
            "title"    : "猎户座星云怎么拍",
            "reaction" : "三脚架一定要稳",
            "feeling"  : "like",
            "topics"   : ["天文摄影"],
            "thread_id": "",
        }
    )
    # 和睡前整理写进去的一样（``wind_down`` 里是 ``{"kind": "diary", "at": now, **diary}``）。
    diary = validate_diary(
        {"date": "2026-11-10", "text": "今天学了拍星云。", "episode_ids": ["e1"]},
        "2026-11-10",
        {"e1"},
        (),
    )
    assert diary is not None
    store.journal.append({"kind": "diary", "at": 200.0, **diary})
    store.learnings.append(
        {
            "id"        : "l1",
            "topic"     : "星云",
            "text"      : "拍星云要长曝光",
            "created_at": 300.0,
            "confidence": "verified",
            "sources"   : 2,
        }
    )
    return store


@pytest.mark.asyncio
async def test_publish_sends_new_items_with_stable_keys_and_moves_the_cursor(tmp_path) -> None:
    store  = _store(tmp_path)
    memory = FakeMemory()
    context = SimpleNamespace(capabilities=SimpleNamespace(memory=memory))
    assert await memory_bridge.publish(context, store, LifeConfig(), now=400.0) == 3
    ops = memory.payloads[0]["ops"]
    assert memory.payloads[0]["scope"] == "global"
    assert [(op["kind"], op["key"]) for op in ops] == [
        ("episode", "e1"),
        ("self.diary", "diary:2026-11-10"),
        ("world.learning", "l1"),
    ]
    assert ops[0]["text"] == "在B站看了「猎户座星云怎么拍」：三脚架一定要稳"
    assert ops[2]["data"]["verified"] is True
    assert store.state[memory_bridge.CURSOR_KEY] == 400.0
    assert await memory_bridge.publish(context, store, LifeConfig(), now=500.0) == 0


@pytest.mark.asyncio
async def test_cursor_waits_while_a_session_is_running_so_late_entries_still_go_out(
    tmp_path,
) -> None:
    store  = _store(tmp_path)
    memory = FakeMemory()
    context = SimpleNamespace(capabilities=SimpleNamespace(memory=memory))
    # 睡前整理还在跑：已经写下的先发，游标不动。
    assert await memory_bridge.publish(context, store, LifeConfig(), 400.0, settled=False) == 3
    assert memory_bridge.CURSOR_KEY not in store.state
    # 它写下的日记带的是开始时的时间，早于刚才那次检查。
    diary = validate_diary(
        {"date": "2026-11-11", "text": "今天把三脚架调稳了。", "episode_ids": ["e1"]},
        "2026-11-11",
        {"e1"},
        (),
    )
    assert diary is not None
    store.journal.append({"kind": "diary", "at": 390.0, **diary})
    assert await memory_bridge.publish(context, store, LifeConfig(), 500.0) == 4
    assert "diary:2026-11-11" in [op["key"] for op in memory.payloads[-1]["ops"]]
    assert store.state[memory_bridge.CURSOR_KEY] == 500.0


@pytest.mark.asyncio
async def test_cursor_stays_when_memory_refuses_or_is_missing(tmp_path) -> None:
    store = _store(tmp_path)
    refused = SimpleNamespace(capabilities=SimpleNamespace(memory=FakeMemory(fail=True)))
    assert await memory_bridge.publish(refused, store, LifeConfig(), now=400.0) == 0
    assert memory_bridge.CURSOR_KEY not in store.state
    assert (
        await memory_bridge.publish(
            SimpleNamespace(capabilities=SimpleNamespace()), store, LifeConfig(), now=400.0
        )
        == 0
    )
