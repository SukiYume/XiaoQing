"""命令回执与交付状态：多附件回执、未知结果、发送中取消与关闭收尾。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.delivery import DeliveryReceipt
from plugins.xiaoqing_agent import receipts
from plugins.xiaoqing_agent.delivery import SendReport
from plugins.xiaoqing_agent.runtime import AgentRuntime
from plugins.xiaoqing_agent.tasks import AgentTask, ChatRef, Outcome
from tests.helpers.xiaoqing_agent_test_support import ADMIN_ID, FakeAgentContext

CHAT = ChatRef(ADMIN_ID, None)


class _Receipt:
    def __init__(self) -> None:
        self.commit   = AsyncMock()
        self.rollback = AsyncMock()
        self.unknown  = AsyncMock()
        self.receipt  = DeliveryReceipt(
            expected_actions = 1,
            commit           = self.commit,
            rollback         = self.rollback,
            unknown          = self.unknown,
        )

    @property
    def calls(self) -> tuple[int, int, int]:
        return (self.commit.await_count, self.rollback.await_count, self.unknown.await_count)


def _files(tmp_path: Path, *names: str) -> list[Path]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def _task(task_id: str = "t1") -> AgentTask:
    return AgentTask(task_id, CHAT, "目标", "原话", "chat", admin=True)


def test_unknown_send_result_is_not_counted_as_delivered() -> None:
    assert SendReport(None).result is None
    assert SendReport(True, uploads=[(Path("a"), None)]).result is None
    assert SendReport(True, uploads=[(Path("a"), False)]).result is False
    assert SendReport(True, uploads=[(Path("a"), True)]).result is True


@pytest.mark.asyncio
async def test_receipt_with_several_files_waits_for_all_of_them(tmp_path: Path) -> None:
    first, second = _files(tmp_path, "a.csv", "b.csv")
    held: list[receipts.HeldReceipt] = []
    log                              = _Receipt()
    await receipts.hold(held, log.receipt, [first, second])

    await receipts.settle(held, [first], True)
    assert log.calls == (0, 0, 0)
    await receipts.settle(held, [second], False)
    # 一半送达一半失败：整份回执不能当成功，也不能当作完全没发。
    assert log.calls == (0, 0, 1)
    assert held == []

    both = _Receipt()
    await receipts.hold(held, both.receipt, [first, second])
    await receipts.settle(held, [first], True)
    await receipts.settle(held, [second], True)
    assert both.calls == (1, 0, 0)


@pytest.mark.asyncio
async def test_release_records_unsent_parts_of_a_kept_receipt(tmp_path: Path) -> None:
    image, table = _files(tmp_path, "a.png", "b.csv")
    held: list[receipts.HeldReceipt] = []
    log                              = _Receipt()
    await receipts.hold(held, log.receipt, [image, table])

    await receipts.release(held, keep=[image])
    assert log.calls == (0, 0, 0)
    await receipts.settle(held, [image], False)
    assert log.calls == (0, 1, 0)


@pytest.mark.asyncio
async def test_background_result_with_unknown_send_is_recorded_as_unknown(tmp_path: Path) -> None:
    context              = FakeAgentContext(tmp_path)
    context.send_results = [None]
    runtime              = AgentRuntime(tmp_path)
    task                 = _task()

    await runtime._deliver(context, task, Outcome("结果"))

    assert task.delivery == "unknown"
    records = await runtime.store.recent(CHAT.key, 5)
    assert records[-1]["delivery"] == "unknown"
    assert "是否送达未确认" in await runtime.history_text(CHAT)


@pytest.mark.asyncio
async def test_cancelling_a_background_send_settles_every_held_receipt(tmp_path: Path) -> None:
    image, table = _files(tmp_path, "a.png", "b.csv")
    context = FakeAgentContext(tmp_path)
    runtime = AgentRuntime(tmp_path)
    task    = _task()
    image_log, table_log = _Receipt(), _Receipt()
    await receipts.hold(task.held_receipts, image_log.receipt, [image])
    await receipts.hold(task.held_receipts, table_log.receipt, [table])
    entered = asyncio.Event()
    never   = asyncio.Event()

    async def blocking_send(action):
        entered.set()
        await never.wait()

    context.send_action = blocking_send  # type: ignore[method-assign]
    sending             = asyncio.create_task(
        runtime._deliver(context, task, Outcome("结果", [str(image), str(table)]))
    )
    await asyncio.wait_for(entered.wait(), 5)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending

    assert task.held_receipts == []
    # 正在发送的图片可能已经发出，记为未知；还没轮到的附件确定没有发出。
    assert image_log.calls == (0, 0, 1)
    assert table_log.calls == (0, 1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_remaining_upload", [False, True])
async def test_cancelled_inline_upload_persists_delivery_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_remaining_upload: bool
) -> None:
    paths   = _files(tmp_path, "first.csv", "second.csv")
    uploads = paths if has_remaining_upload else paths[:1]
    context = FakeAgentContext(tmp_path)
    runtime = AgentRuntime(tmp_path)
    task    = _task()
    logs    = [_Receipt() for _ in uploads]
    for path, log in zip(uploads, logs, strict=True):
        await receipts.hold(task.held_receipts, log.receipt, [path])
    entered = asyncio.Event()

    async def blocking_send(action):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(context, "send_action", blocking_send)
    monkeypatch.setattr("plugins.xiaoqing_agent.runtime._UPLOAD_AFTER_REPLY_SECONDS", 0)
    await runtime.inline_reply(context, task, Outcome("结果", [str(path) for path in uploads]))
    sending = next(iter(runtime._deliveries))
    await runtime.report_delivery(task.task_id, True)
    await asyncio.wait_for(entered.wait(), 5)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(sending, 5)

    expected = "failed" if has_remaining_upload else "unknown"
    assert task.delivery == expected
    records = await runtime.store.recent(CHAT.key, 5)
    assert records[-1]["delivery"] == expected
    assert task.held_receipts == []
    assert logs[0].calls == (0, 0, 1)
    if has_remaining_upload:
        assert logs[1].calls == (0, 1, 0)
    assert task.task_id not in runtime._awaiting_report


@pytest.mark.asyncio
async def test_shutdown_settles_inline_replies_still_waiting_for_a_report(tmp_path: Path) -> None:
    image   = _files(tmp_path, "a.png")[0]
    context = FakeAgentContext(tmp_path)
    runtime = AgentRuntime(tmp_path)
    task    = _task()
    log     = _Receipt()
    await receipts.hold(task.held_receipts, log.receipt, [image])

    await runtime.inline_reply(context, task, Outcome("结果", [str(image)]))
    assert len(runtime._awaiting_report) == 1
    await runtime.shutdown()

    assert runtime._awaiting_report == {}
    assert task.held_receipts == []
    assert log.calls == (0, 0, 1)
    assert task.delivery == "unknown"
