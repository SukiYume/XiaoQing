"""任务记录存储：取消写盘时的串行边界与损坏文件保留。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from plugins.xiaoqing_agent.tasks import AgentTask, ChatRef, TaskStore

CHAT = ChatRef(10001, None)


def _task(task_id: str) -> AgentTask:
    return AgentTask(task_id=task_id, chat=CHAT, goal=task_id, request_text=task_id, source="test")


def _ids(path: Path) -> list[str]:
    return [item["task_id"] for item in json.loads(path.read_text(encoding="utf-8"))["tasks"]]


@pytest.mark.asyncio
async def test_cancelled_save_still_finishes_before_the_next_save(
    tmp_path: Path, monkeypatch
) -> None:
    store    = TaskStore(tmp_path)
    started  = threading.Event()
    release  = threading.Event()
    original = TaskStore._write

    def slow_write(path: Path, records: list[dict]) -> None:
        if any(item["task_id"] == "first" for item in records) and not release.is_set():
            started.set()
            assert release.wait(5)
        original(path, records)

    monkeypatch.setattr(TaskStore, "_write", staticmethod(slow_write))
    first = asyncio.create_task(store.save(_task("first")))
    assert await asyncio.to_thread(started.wait, 5)
    first.cancel()
    second = asyncio.create_task(store.save(_task("second")))
    await asyncio.sleep(0.05)
    assert not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, 5)
    await asyncio.wait_for(second, 5)

    assert _ids(store._path(CHAT.key)) == ["first", "second"]
    assert await store.update_delivery(CHAT.key, "second", "sent")


@pytest.mark.asyncio
async def test_corrupt_records_are_kept_aside_instead_of_overwritten(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    path  = store._path(CHAT.key)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    await store.save(_task("new"))

    assert _ids(path) == ["new"]
    kept = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert [item.read_text(encoding="utf-8") for item in kept] == ["{broken"]


@pytest.mark.asyncio
async def test_unreadable_records_fail_the_save_and_leave_the_file(
    tmp_path: Path, monkeypatch
) -> None:
    store = TaskStore(tmp_path)
    path  = store._path(CHAT.key)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "tasks": [{"task_id": "old"}]}), encoding="utf-8")
    original = Path.read_text

    def failing_read(self: Path, *args, **kwargs):
        if self == path:
            raise PermissionError("locked")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read)
    with pytest.raises(PermissionError):
        await store.save(_task("new"))
    monkeypatch.undo()

    assert _ids(path) == ["old"]
