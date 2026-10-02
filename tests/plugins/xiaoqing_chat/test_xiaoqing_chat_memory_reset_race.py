# 验证重置与正在进行的记忆落盘之间的代际隔离。
"""Regression tests for reset versus in-flight memory persistence."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from core.plugin_base import write_json as real_write_json
from plugins.xiaoqing_chat.memory.knowledge_extract import PersonFact, _persist_person_facts
from plugins.xiaoqing_chat.memory.memory import MemoryStore
from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
from plugins.xiaoqing_chat.memory.person_profile import (
    clear_profiles_and_memory,
    get_profile_generation,
    load_profile,
    update_profile_and_index,
)


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


def test_profile_reset_clears_backups_and_rejects_old_generation(tmp_path):
    db = MemoryDB()
    db.bind(tmp_path)
    generation = get_profile_generation(tmp_path, "g1")
    update_profile_and_index(
        data_dir     = tmp_path,
        memory_db    = db,
        chat_id      = "g1",
        subject_id   = 1,
        subject_name = "u",
        new_facts    = ["old"],
    )
    update_profile_and_index(
        data_dir     = tmp_path,
        memory_db    = db,
        chat_id      = "g1",
        subject_id   = 1,
        subject_name = "u",
        new_facts    = ["older"],
    )
    clear_profiles_and_memory(tmp_path, "g1", db)
    _persist_person_facts(
        data_dir            = tmp_path,
        memory_db           = db,
        chat_id             = "g1",
        facts               = [PersonFact(1, "u", "stale", "stale")],
        expected_generation = generation,
    )
    assert load_profile(tmp_path, chat_id="g1", subject_id=1) is None
    assert not list((tmp_path / "person_profiles" / "g1").glob("*.json*"))
    update_profile_and_index(
        data_dir     = tmp_path,
        memory_db    = db,
        chat_id      = "g1",
        subject_id   = 1,
        subject_name = "u",
        new_facts    = ["new"],
    )
    assert load_profile(tmp_path, chat_id="g1", subject_id=1).facts == ["new"]
