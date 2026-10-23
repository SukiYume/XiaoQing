"""结果投递：图片内嵌、文件上传、缺失与超限文件的说明，以及发送失败不抛出。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.interfaces import ACTION_BYPASS_SINK_KEY
from plugins.xiaoqing_agent import delivery
from plugins.xiaoqing_agent.delivery import (
    classify_files,
    outcome_message,
    send_message,
    upload_file,
)
from plugins.xiaoqing_agent.tasks import ChatRef, Outcome
from tests.helpers.xiaoqing_agent_test_support import ADMIN_ID, GROUP_ID, FakeAgentContext


def test_classify_files_splits_images_uploads_and_problems(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(delivery, "MAX_IMAGES", 2)
    monkeypatch.setattr(delivery, "MAX_UPLOADS", 1)
    monkeypatch.setattr(delivery, "MAX_FILE_BYTES", 10)
    for name in ("a.png", "b.JPG", "c.webp"):
        (tmp_path / name).write_bytes(b"img")
    (tmp_path / "data.csv").write_text("1,2", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "huge.bin").write_bytes(b"0" * 11)

    images, uploads, problems = classify_files(
        [
            str(tmp_path / "a.png"),
            str(tmp_path / "a.png"),
            str(tmp_path / "b.JPG"),
            str(tmp_path / "c.webp"),
            str(tmp_path / "data.csv"),
            str(tmp_path / "notes.txt"),
            str(tmp_path / "huge.bin"),
            str(tmp_path / "missing.png"),
        ]
    )

    assert [path.name for path in images] == ["a.png", "b.JPG"]
    assert [path.name for path in uploads] == ["data.csv"]
    assert problems == [
        "图片太多，c.webp 没有发出",
        f"文件太多，notes.txt 留在 {(tmp_path / 'notes.txt').resolve()}",
        f"huge.bin 超过 100MB，留在 {(tmp_path / 'huge.bin').resolve()}",
        "找不到文件 missing.png",
    ]


def test_outcome_message_notes_problems_and_defaults_empty_text(tmp_path: Path) -> None:
    message, uploads = outcome_message(Outcome("  ", [str(tmp_path / "gone.png")]))
    assert uploads == []
    assert message == [
        {
            "type": "text",
            "data": {"text": "做完了，但没有可以说明的结果。\n（找不到文件 gone.png）"},
        }
    ]


@pytest.mark.asyncio
async def test_send_and_upload_target_the_chat_and_swallow_transport_errors(tmp_path: Path) -> None:
    context = FakeAgentContext(tmp_path)
    group   = ChatRef(ADMIN_ID, GROUP_ID)
    private = ChatRef(ADMIN_ID, None)

    assert await send_message(context, group, [{"type": "text", "data": {"text": "hi"}}]) is True
    assert context.actions[-1]["action"] == "send_group_msg"
    assert context.actions[-1][ACTION_BYPASS_SINK_KEY] is True
    assert await send_message(context, group, []) is False

    context.send_results = [None]
    assert await upload_file(context, private, tmp_path / "x.csv") is None
    assert context.actions[-1]["action"] == "upload_private_file"

    async def broken(_action):
        raise RuntimeError("transport down")

    context.send_action = broken  # type: ignore[method-assign]
    assert await send_message(context, private, [{"type": "text", "data": {"text": "hi"}}]) is False
    assert await upload_file(context, group, tmp_path / "x.csv") is False


@pytest.mark.asyncio
async def test_audio_and_video_are_sent_as_their_own_messages(tmp_path: Path) -> None:
    from plugins.xiaoqing_agent.delivery import deliver_outcome

    for name in ("say.mp3", "clip.mp4", "table.csv", "plot.png"):
        (tmp_path / name).write_bytes(b"data")
    context = FakeAgentContext(tmp_path)
    files   = [str(tmp_path / name) for name in ("say.mp3", "clip.mp4", "table.csv", "plot.png")]

    await deliver_outcome(context, ChatRef(ADMIN_ID, None), Outcome("都在这", files))

    kinds = [
        (action["action"], [seg["type"] for seg in action["params"].get("message", [])])
        for action in context.actions
    ]
    assert kinds == [
        ("send_private_msg", ["text", "image"]),
        ("send_private_msg", ["record"]),
        ("send_private_msg", ["video"]),
        ("upload_private_file", []),
    ]
