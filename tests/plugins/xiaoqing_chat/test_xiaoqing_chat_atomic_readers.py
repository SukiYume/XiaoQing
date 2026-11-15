# 验证聊天数据读取者只观察完整发布的文件快照。
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_corrupt_primary_with_backup(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    path.with_name(f"{path.name}.bak").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_memory_store_recovers_valid_atomic_backup(tmp_path: Path) -> None:
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    _write_corrupt_primary_with_backup(
        tmp_path / "chat-1.json",
        [{"role": "user", "name": "Alice", "content": "restored", "ts": 1.0}],
    )
    store = MemoryStore(tmp_path)

    history = await store.get_async("chat-1")

    assert [message.content for message in history] == ["restored"]


def test_vector_docs_recover_valid_atomic_backup(tmp_path: Path) -> None:
    from plugins.xiaoqing_chat.memory.vector_store import VectorStore

    _write_corrupt_primary_with_backup(
        tmp_path / "memory.docs.json",
        [{"doc_id": "d1", "text": "restored", "meta": {"type": "knowledge"}}],
    )
    store = VectorStore(dim=4)

    store.load(tmp_path, name="memory")

    assert [document.text for document in store.all_docs()] == ["restored"]


@pytest.mark.asyncio
async def test_action_history_recovers_valid_atomic_backup(tmp_path: Path) -> None:
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    _write_corrupt_primary_with_backup(
        tmp_path / "action_history" / "chat-1.json",
        [
            {
                "ts"          : 4.0,
                "local_target": "Alice",
                "action"      : "reply",
                "reasoning"   : "restored",
                "detail"      : {},
                "executed"    : True,
            }
        ],
    )
    store = ActionHistoryStore()
    store.bind(tmp_path)

    records = await store.get_recent_async("chat-1")

    assert [record.reasoning for record in records] == ["restored"]


def test_expression_reflector_recovers_valid_atomic_backup(tmp_path: Path) -> None:
    from plugins.xiaoqing_chat.expression.bw_expression_reflector import _load_state

    _write_corrupt_primary_with_backup(
        tmp_path / "bw_learner" / "reflector_state.json",
        {"last_sent_ts": 5.0},
    )

    assert _load_state(tmp_path) == {"last_sent_ts": 5.0}
