"""真实服务验收发现的人称泄漏与共享关闭预算回归。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.xiaoqing_chat import main
from plugins.xiaoqing_chat.memory.memory import MemoryStore
from plugins.xiaoqing_chat.reply_generator import _concise_public_identity


@pytest.mark.parametrize(
    "identity,expected",
    [
        (
            "你是理工科学生。你对天文有兴趣，你也喜欢猫。",
            "我是理工科学生。我对天文有兴趣，我也喜欢猫",
        ),
        ("二十岁左右，是住校学生。你对天文有兴趣。", "二十岁左右，是住校学生。我对天文有兴趣"),
        ("我的昵称叫迷你青。朋友说你很活泼。", "我的昵称叫迷你青。朋友说你很活泼"),
    ],
)
def test_public_identity_renders_configured_subject_as_first_person(identity, expected):
    assert _concise_public_identity(identity) == expected


@pytest.mark.asyncio
async def test_shutdown_cancels_sleeping_work_and_flushes_within_shared_budget(
    tmp_path, monkeypatch
):
    memory = MemoryStore(tmp_path)
    memory.append("g1", role="user", name="user", content="before shutdown")
    started = asyncio.Event()

    async def delayed_work():
        try:
            started.set()
            await asyncio.sleep(30)
        finally:
            # 取消收尾仍可更新状态，最终刷盘需要保存这一轮变更。
            memory.append("g1", role="assistant", name="bot", content="cleanup complete")

    task = asyncio.create_task(delayed_work())
    await started.wait()
    state = SimpleNamespace(
        stop_accepting_background_tasks = Mock(),
        background_tasks                = lambda: {task},
        memory_store                    = memory,
        pfc_state_store=SimpleNamespace(flush=Mock()),
        action_history=SimpleNamespace(flush=Mock()),
        media_store=SimpleNamespace(flush=Mock()),
        memory_db=SimpleNamespace(is_dirty=lambda: False),
    )
    monkeypatch.setattr(main, "_state", lambda: state)
    try:
        # 其他插件已经消耗部分默认五秒预算，聊天需要在剩余预算内收敛。
        await asyncio.wait_for(main.shutdown(SimpleNamespace(logger=Mock())), timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert [message.content for message in MemoryStore(tmp_path).get("g1")] == [
        "before shutdown",
        "cleanup complete",
    ]
