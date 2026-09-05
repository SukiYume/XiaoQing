# 验证聊天回想流程的上下文选择与规划结果。
from __future__ import annotations

import json
import threading
from pathlib import Path

from plugins.xiaoqing_chat.memory import thinking_back


def test_compaction_and_append_share_one_path_lock(monkeypatch, tmp_path: Path) -> None:
    entered_atomic_write          = threading.Event()
    release_atomic_write          = threading.Event()
    second_finished               = threading.Event()
    failures: list[BaseException] = []
    real_atomic_write             = thinking_back.atomic_write_text

    def blocked_atomic_write(path: Path, payload: str) -> None:
        entered_atomic_write.set()
        assert release_atomic_write.wait(timeout=2.0)
        real_atomic_write(path, payload)

    monkeypatch.setattr(thinking_back, "atomic_write_text", blocked_atomic_write)

    def append(question: str, answer: str, *, finished: threading.Event | None = None) -> None:
        try:
            thinking_back.append_record(
                data_dir    = tmp_path,
                chat_id     = "chat-1",
                question    = question,
                answer      = answer,
                max_entries = 10,
                max_bytes   = 1,
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
        finally:
            if finished is not None:
                finished.set()

    first = threading.Thread(target=append, args=("q1", "a1"))
    first.start()
    assert entered_atomic_write.wait(timeout=2.0)

    second = threading.Thread(
        target=append, args=("q2", "a2"), kwargs={"finished": second_finished}
    )
    second.start()
    assert not second_finished.wait(timeout=0.05)

    release_atomic_write.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    rows = [
        json.loads(line)
        for line in (tmp_path / "thinking_back" / "chat-1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["question"], row["answer"]) for row in rows] == [("q1", "a1"), ("q2", "a2")]
