# 验证重置与正在进行的记忆落盘之间的代际隔离。
"""Regression tests for reset versus in-flight memory persistence."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from core.plugin_base import write_json as real_write_json
from plugins.xiaoqing_chat.memory.memory import MemoryStore


def _append(store: MemoryStore, chat_id: str, content: str) -> None:
    store.append(
        chat_id,
        role    = "user",
        name    = "Tester",
        user_id = 1,
        content = content,
    )


def test_reset_waits_for_old_snapshot_commit_then_removes_it(tmp_path, monkeypatch):
    store   = MemoryStore(tmp_path)
    chat_id = "g-reset-race"
    _append(store, chat_id, "old private history")
    writer_started      = threading.Event()
    allow_writer_commit = threading.Event()
    clear_started       = threading.Event()

    def blocking_write(path, payload):
        writer_started.set()
        assert allow_writer_commit.wait(timeout=5)
        real_write_json(path, payload)

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.memory.write_json",
        blocking_write,
    )

    def clear_memory():
        clear_started.set()
        store.clear(chat_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        persist_future = executor.submit(store.persist, chat_id)
        assert writer_started.wait(timeout=5)
        clear_future = executor.submit(clear_memory)
        assert clear_started.wait(timeout=5)
        assert not clear_future.done()
        allow_writer_commit.set()
        persist_future.result(timeout=5)
        clear_future.result(timeout=5)

    path = tmp_path / f"{chat_id}.json"
    assert not path.exists() or path.read_text(encoding="utf-8").strip() == "[]"
    assert MemoryStore(tmp_path).get(chat_id) == []


def test_consecutive_resets_keep_tombstone_and_history_empty(tmp_path):
    store   = MemoryStore(tmp_path)
    chat_id = "g-double-reset"
    _append(store, chat_id, "old history")
    store.persist(chat_id)

    store.clear(chat_id)
    store.clear(chat_id)
    store.persist(chat_id)

    assert store.get(chat_id) == []
    assert MemoryStore(tmp_path).get(chat_id) == []


def test_new_message_after_reset_starts_new_generation_without_old_history(tmp_path):
    store   = MemoryStore(tmp_path)
    chat_id = "g-reset-new-message"
    _append(store, chat_id, "old history")
    store.persist(chat_id)

    store.clear(chat_id)
    _append(store, chat_id, "new history")
    store.persist(chat_id)

    reloaded = MemoryStore(tmp_path).get(chat_id)
    assert [message.content for message in reloaded] == ["new history"]
