"""入口：服务回调的范围检查和关闭开关、定时检查只做门控、/mem 命令。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.xiaoqing_memory import main as memory_main
from tests.helpers.xiaoqing_memory_test_support import ALI, GROUP, bot_event, user_event


class FakeAI:
    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls   = 0

    async def complete(self, route: str, messages: list[dict[str, str]]) -> Any:
        self.calls += 1
        output = self.outputs.pop(0) if self.outputs else "{}"
        return SimpleNamespace(content=output if isinstance(output, str) else json.dumps(output))


def _context(
    tmp_path: Path, *, enabled: bool = True, ai: Any = None, routes: bool = True, **options: Any
) -> Any:
    plugin: dict[str, Any] = {"enabled": enabled, **options}
    if routes:
        plugin["ai"] = {"routes": {"reasoning": ["m"]}}
    sent: list[dict[str, Any]] = []

    async def send_action(action: dict[str, Any]) -> bool:
        sent.append(action)
        return True

    return SimpleNamespace(
        data_dir              = tmp_path / "memory",
        state                 = {},
        sent                  = sent,
        send_action           = send_action,
        get_settings_snapshot = lambda: SimpleNamespace(
            config={
                "bot_name": "小青",
                "timezone": "Asia/Shanghai",
                "plugins" : {"xiaoqing_memory": plugin},
            }
        ),
        capabilities=SimpleNamespace(ai=ai or FakeAI()),
    )


def _payload(principal: str, **extra: Any) -> dict[str, Any]:
    return {
        "caller"         : "xiaoqing_chat",
        "principal_kind" : "user",
        "principal_scope": principal,
        **extra,
    }


def _text(result: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("data", {}).get("text", "")) for item in result)


@pytest.mark.asyncio
async def test_services_enforce_scope_and_disabled_switch(temp_data_dir) -> None:
    context = _context(temp_data_dir)
    out     = await memory_main.record(
        _payload(GROUP, events=[user_event("在吗", ALI, "阿梨", "k1")]), context
    )
    assert out["seqs"] == [1]
    refused = await memory_main.recall(_payload(GROUP, scope="g999"), context)
    assert refused == {"version": 1, "error": "scope_mismatch"}
    recalled = await memory_main.recall(_payload(GROUP, text="在吗"), context)
    assert recalled["version"] == 1 and "blocks" in recalled
    off = _context(temp_data_dir / "off", enabled=False)
    assert await memory_main.recall(_payload(GROUP), off) is None
    assert await memory_main.record(_payload(GROUP), off) is None


@pytest.mark.asyncio
async def test_tick_only_gates_and_spawns_one_consolidation(temp_data_dir) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class GatedAI(FakeAI):
        """整理要调模型：卡在第一次调用上，直到测试放行。"""

        async def complete(self, route: str, messages: list[dict[str, str]]) -> Any:
            result = await super().complete(route, messages)
            started.set()
            await release.wait()
            return result

    ai = GatedAI()
    # 待整理的事件够多（不必等到夜间窗口），定时检查才会真的去整理这个群。
    context = _context(temp_data_dir, ai=ai, backlog_trigger_events=10)
    events = [user_event(f"第{n}句", ALI, "阿梨", f"k{n}") for n in range(10)]
    await memory_main.record(_payload(GROUP, events=events), context)
    runtime = memory_main.get_runtime(context)

    await memory_main.scheduled_tick(context)
    await asyncio.wait_for(started.wait(), 1)
    first = runtime.task
    assert first is not None and not first.done()
    await memory_main.scheduled_tick(context)
    assert runtime.task is first and ai.calls == 1, "整理是单飞的"
    release.set()
    await memory_main.shutdown(context)

    no_route_ai = FakeAI()
    no_route = _context(temp_data_dir / "noroute", ai=no_route_ai, routes=False)
    await memory_main.record(_payload(GROUP, events=events), no_route)
    await memory_main.scheduled_tick(no_route)
    assert memory_main.get_runtime(no_route).task is None and no_route_ai.calls == 0, (
        "没有配置模型时不整理"
    )


@pytest.mark.asyncio
async def test_mem_commands_status_consolidate_page_why_forget(temp_data_dir) -> None:
    ai = FakeAI(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "下周去杭州面试",
                    "source": "e1",
                    "quote" : "下周去杭州面试",
                }
            ]
        },
        {"names": ["阿梨"], "brief": "", "sections": {}},
    )
    context = _context(temp_data_dir, ai=ai)
    await memory_main.record(
        _payload(
            GROUP,
            events=[user_event("我下周去杭州面试", ALI, "阿梨", "k1"), bot_event("加油", "k2")],
        ),
        context,
    )
    event  = {"user_id": 42, "message_type": "private"}
    status = _text(await memory_main.handle("mem", "status", event, context))
    assert GROUP in status and "待整理 2" in status

    reply = _text(await memory_main.handle("mem", "consolidate", event, context))
    assert "开始整理" in reply
    runtime = memory_main.get_runtime(context)
    await runtime.task
    assert context.sent and "整理完了" in json.dumps(context.sent[-1], ensure_ascii=False)
    assert context.sent[-1]["params"]["user_id"] == 42

    page = _text(await memory_main.handle("mem", f"page {GROUP} {ALI}", event, context))
    assert "杭州" in page
    note_id = next(iter(runtime.store.notes(GROUP)))
    why     = _text(await memory_main.handle("mem", f"why {note_id}", event, context))
    assert "我下周去杭州面试" in why and "摘录" in why
    search = _text(await memory_main.handle("mem", f"search {GROUP} 杭州", event, context))
    assert "杭州" in search

    forgot = _text(await memory_main.handle("mem", f"forget {GROUP} {ALI}", event, context))
    assert "removed" in forgot and runtime.store.notes(GROUP) == {}
    _text(await memory_main.handle("mem", f"forget {GROUP}", event, context))
    assert runtime.store.events.events(GROUP) == []
    assert "用法" in _text(await memory_main.handle("mem", "forget", event, context))
    assert "/mem status" in _text(await memory_main.handle("mem", "help", event, context))
    await memory_main.shutdown(context)


@pytest.mark.asyncio
async def test_rebuild_rewrites_pages_from_notes(temp_data_dir) -> None:
    ai = FakeAI(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "养了只猫",
                    "source": "e1",
                    "quote" : "养了只猫",
                }
            ]
        },
    )
    context = _context(temp_data_dir, ai=ai)
    await memory_main.record(
        _payload(GROUP, events=[user_event("我养了只猫", ALI, "阿梨", "k1")]), context
    )
    event = {"user_id": 42}
    await memory_main.handle("mem", f"consolidate {GROUP}", event, context)
    runtime = memory_main.get_runtime(context)
    await runtime.task
    note_id = next(iter(runtime.store.notes(GROUP)))
    ai.outputs.append({"brief": "养猫的阿梨", "brief_notes": [note_id], "sections": {}})
    await memory_main.handle("mem", f"rebuild {GROUP}", event, context)
    await runtime.task
    assert runtime.store.page(GROUP, f"person:{ALI}").brief == "养猫的阿梨"
    assert "重写了 1/1" in json.dumps(context.sent[-1], ensure_ascii=False)
    await memory_main.shutdown(context)
