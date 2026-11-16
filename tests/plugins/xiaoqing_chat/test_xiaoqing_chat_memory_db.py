"""聊天的 MemoryDB：保存与脏标记、按会话删除、切换目录、配置知识库的刷新与预算、运行时刷新、本地编号。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugins.xiaoqing_chat import chat_runtime
from plugins.xiaoqing_chat.memory.knowledge_base import (
    MAX_KNOWLEDGE_CHUNKS,
    MAX_KNOWLEDGE_FILE_BYTES,
    MAX_KNOWLEDGE_FILES,
    MAX_TOTAL_KNOWLEDGE_BYTES,
    KnowledgeIndexError,
    ensure_knowledge_index,
)
from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
from plugins.xiaoqing_chat.memory.vector_store import VectorDoc, VectorStore
from plugins.xiaoqing_chat.persona import BUNDLED_PERSONA_FILE
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState, get_state
from tests.helpers.settings_snapshot import settings_snapshot


def _knowledge_snapshot(db: MemoryDB) -> list[tuple[str, str, dict]]:
    return sorted(
        (item.doc_id, item.text, item.meta)
        for item in db.query_knowledge("configured knowledge", top_k=2048, min_score=-1.0)
    )


def test_memory_db_save_persists_documents_and_clears_dirty(tmp_path: Path) -> None:
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    assert db.is_dirty() is True

    db.save()

    assert db.is_dirty() is False
    reloaded = MemoryDB()
    reloaded.bind(tmp_path)
    saved = reloaded.get("doc-1")
    assert saved is not None and saved.text == "hello"


def test_memory_db_save_keeps_dirty_when_shared_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    with monkeypatch.context() as scoped, caplog.at_level(logging.WARNING):
        scoped.setattr(os, "replace", disk_full)
        db.save()

    assert db.is_dirty() is True
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_memory_db_save_keeps_dirty_when_new_write_happens_during_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    did_race_write = False
    real_replace   = os.replace

    def replace_after_racing_write(*args: Any, **kwargs: Any) -> None:
        # 落盘的第一次原子替换之前，另一个线程写入了新文档：这次保存的快照里没有它。
        nonlocal did_race_write
        if not did_race_write:
            did_race_write = True
            db.upsert_text(doc_id="doc-2", text="new info", meta={"type": "knowledge"})
        real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_after_racing_write)

    db.save()

    assert did_race_write is True
    assert db.is_dirty() is True


def test_memory_db_delete_chat_removes_only_target_scope_and_persists(
    tmp_path: Path,
) -> None:
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id = "topic-target",
        text   = "目标群旧话题",
        meta   = {"type": "topic_summary", "chat_id": "g-target"},
    )
    db.upsert_text(
        doc_id = "person-target",
        text   = "目标群人物信息",
        meta   = {"type": "person_info", "chat_id": "g-target"},
    )
    db.upsert_text(
        doc_id = "topic-other",
        text   = "其他群旧话题",
        meta   = {"type": "topic_summary", "chat_id": "g-other"},
    )
    db.upsert_text(
        doc_id = "global-knowledge",
        text   = "审核后的全局知识",
        meta   = {"type": "knowledge", "global_approved": True},
    )
    db.save()

    assert db.delete_chat("g-target") == 2
    assert db.delete_chat("g-target") == 0
    db.save()

    reloaded = MemoryDB()
    reloaded.bind(tmp_path)
    assert reloaded.get("topic-target") is None
    assert reloaded.get("person-target") is None
    assert reloaded.get("topic-other") is not None
    assert [item.doc_id for item in reloaded.query_knowledge("全局知识", min_score=-1.0)] == [
        "global-knowledge"
    ]


def test_configured_knowledge_refresh_removes_shortened_removed_and_deleted_sources(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    data_dir   = tmp_path / "data"
    plugin_dir.mkdir()
    first  = plugin_dir / "first.txt"
    second = plugin_dir / "second.txt"
    first.write_text("alpha\n\nbeta", encoding="utf-8")
    second.write_text("gamma", encoding="utf-8")

    db = MemoryDB()
    db.bind(data_dir)
    db.upsert_text(
        doc_id = "reviewed-public",
        text   = "public reference",
        meta   = {"type": "knowledge", "global_approved": True},
    )

    assert (
        ensure_knowledge_index(
            memory_db  = db,
            data_dir   = data_dir,
            plugin_dir = plugin_dir,
            files      = ["first.txt", "second.txt"],
        )
        is True
    )
    assert sorted(text for _doc_id, text, _meta in _knowledge_snapshot(db)) == [
        "alpha",
        "beta",
        "gamma",
        "public reference",
    ]
    assert (
        ensure_knowledge_index(
            memory_db  = db,
            data_dir   = data_dir,
            plugin_dir = plugin_dir,
            files      = ["first.txt", "second.txt"],
        )
        is False
    )

    first.write_text("alpha revised", encoding="utf-8")
    assert (
        ensure_knowledge_index(
            memory_db  = db,
            data_dir   = data_dir,
            plugin_dir = plugin_dir,
            files      = ["first.txt"],
        )
        is True
    )
    assert sorted(text for _doc_id, text, _meta in _knowledge_snapshot(db)) == [
        "alpha revised",
        "public reference",
    ]

    first.unlink()
    assert (
        ensure_knowledge_index(
            memory_db  = db,
            data_dir   = data_dir,
            plugin_dir = plugin_dir,
            files      = ["first.txt"],
        )
        is True
    )
    assert [text for _doc_id, text, _meta in _knowledge_snapshot(db)] == ["public reference"]
    assert (data_dir / "vdb" / "memory.docs.json").is_file()


def test_configured_knowledge_metadata_never_stores_absolute_source_paths(
    tmp_path: Path,
) -> None:
    plugin_dir   = tmp_path / "plugin"
    internal_dir = plugin_dir / "docs"
    external_dir = tmp_path / "private" / "operator"
    internal_dir.mkdir(parents=True)
    external_dir.mkdir(parents=True)
    internal = internal_dir / "guide.txt"
    external = external_dir / "private-notes.txt"
    internal.write_text("internal knowledge", encoding="utf-8")
    external.write_text("external knowledge", encoding="utf-8")
    db = MemoryDB()

    ensure_knowledge_index(
        memory_db  = db,
        data_dir   = tmp_path / "data",
        plugin_dir = plugin_dir,
        files      = ["docs/guide.txt", str(external.resolve())],
    )

    configured = [
        (doc_id, meta)
        for doc_id, _text, meta in _knowledge_snapshot(db)
        if doc_id.startswith("kb:")
    ]
    encoded_meta = json.dumps([meta for _doc_id, meta in configured], ensure_ascii=False)
    assert str(plugin_dir.resolve()) not in encoded_meta
    assert str(external.resolve()) not in encoded_meta
    sources = {str(meta["source"]) for _doc_id, meta in configured}
    assert "docs/guide.txt" in sources
    assert len(sources) == 2
    assert len([source for source in sources if source.startswith("external:")]) == 1


def test_knowledge_budget_and_decode_failures_preserve_last_complete_snapshot(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    data_dir   = tmp_path / "data"
    plugin_dir.mkdir()
    baseline_file = plugin_dir / "baseline.txt"
    baseline_file.write_text("last complete snapshot", encoding="utf-8")
    db = MemoryDB()
    assert (
        ensure_knowledge_index(
            memory_db  = db,
            data_dir   = data_dir,
            plugin_dir = plugin_dir,
            files      = ["baseline.txt"],
        )
        is True
    )
    baseline = _knowledge_snapshot(db)

    many_names = []
    for index in range(MAX_KNOWLEDGE_FILES + 1):
        (plugin_dir / f"many-{index}.txt").write_text("x", encoding="utf-8")
        many_names.append(f"many-{index}.txt")
    (plugin_dir / "oversized.txt").write_bytes(b"a" * (MAX_KNOWLEDGE_FILE_BYTES + 1))
    # 每个文件都不超限，合计超过总预算：最后一个文件让合计多出 1 字节。
    full_files  = MAX_TOTAL_KNOWLEDGE_BYTES // MAX_KNOWLEDGE_FILE_BYTES
    total_names = []
    for index in range(full_files):
        (plugin_dir / f"full-{index}.txt").write_bytes(b"b" * MAX_KNOWLEDGE_FILE_BYTES)
        total_names.append(f"full-{index}.txt")
    (plugin_dir / "extra.txt").write_bytes(b"c")
    total_names.append("extra.txt")
    (plugin_dir / "chunked.txt").write_text(
        "\n\n".join(f"chunk {index}" for index in range(MAX_KNOWLEDGE_CHUNKS + 1)),
        encoding="utf-8",
    )
    (plugin_dir / "invalid.txt").write_bytes(b"\xff")

    def reject(files: list[str]) -> None:
        with pytest.raises(KnowledgeIndexError):
            ensure_knowledge_index(
                memory_db  = db,
                data_dir   = data_dir,
                plugin_dir = plugin_dir,
                files      = files,
            )
        assert _knowledge_snapshot(db) == baseline

    reject(many_names)
    reject(["oversized.txt"])
    reject(total_names)
    reject(["chunked.txt"])
    reject(["invalid.txt"])


def test_memory_db_configured_replacement_build_failure_is_not_partially_visible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = MemoryDB()
    db.bind(tmp_path)
    approved = {"type": "knowledge", "global_approved": True}
    db.upsert_text(doc_id="kb:old:0", text="old", meta=approved)
    db.upsert_text(doc_id="reviewed-public", text="public", meta=approved)
    original_upsert = VectorStore.upsert

    def fail_second_document(store: VectorStore, document: VectorDoc) -> None:
        if document.doc_id == "kb:new:1":
            raise MemoryError("injected replacement failure")
        original_upsert(store, document)

    monkeypatch.setattr(VectorStore, "upsert", fail_second_document)
    with pytest.raises(MemoryError, match="replacement failure"):
        db.replace_configured_knowledge(
            [
                VectorDoc(doc_id="kb:new:0", text="new zero", meta=approved),
                VectorDoc(doc_id="kb:new:1", text="new one", meta=approved),
            ]
        )

    assert db.get("kb:old:0") is not None
    assert db.get("kb:new:0") is None
    assert db.get("kb:new:1") is None
    assert db.get("reviewed-public") is not None


def _runtime_context(plugin_dir: Path, data_dir: Path, config: dict, revision: int):
    return SimpleNamespace(
        plugin_dir = plugin_dir,
        data_dir   = data_dir,
        get_settings_snapshot=lambda: settings_snapshot(config=config, revision=revision),
        logger=MagicMock(),
    )


def _knowledge_config(*, enabled: bool, files: list[str]) -> dict:
    return {
        "plugins": {"xiaoqing_chat": {"knowledge": {"enable_knowledge": enabled, "files": files}}}
    }


def test_runtime_refresh_publishes_an_empty_snapshot_when_knowledge_is_disabled(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    data_dir   = tmp_path / "data"
    plugin_dir.mkdir()
    (plugin_dir / "stale.txt").write_text("stale reference", encoding="utf-8")
    state = get_state()

    chat_runtime.load_runtime(
        _runtime_context(
            plugin_dir, data_dir, _knowledge_config(enabled=True, files=["stale.txt"]), 1
        )
    )
    assert [text for _doc_id, text, _meta in _knowledge_snapshot(state.memory_db)] == [
        "stale reference"
    ]

    chat_runtime.load_runtime(
        _runtime_context(
            plugin_dir, data_dir, _knowledge_config(enabled=False, files=["stale.txt"]), 2
        )
    )

    assert _knowledge_snapshot(state.memory_db) == []


def test_runtime_refreshes_when_settings_revision_changes_without_file_mtime_change(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    data_dir   = tmp_path / "data"
    plugin_dir.mkdir()
    state = get_state()

    cached = chat_runtime.load_runtime(
        _runtime_context(plugin_dir, data_dir, {"plugins": {"xiaoqing_chat": {}}}, 1)
    )
    changed = {"plugins": {"xiaoqing_chat": {"planner": {"enable_planner": False}}}}
    assert cached.cfg.planner.enable_planner is True
    same = chat_runtime.load_runtime(_runtime_context(plugin_dir, data_dir, changed, 1))
    assert same is cached, "修订号没变就沿用缓存，即使部署配置的内容不同"

    refreshed = chat_runtime.load_runtime(_runtime_context(plugin_dir, data_dir, changed, 2))

    assert refreshed is not cached
    assert refreshed.cfg.planner.enable_planner is False
    # 缓存戳是配置文件和人设卡（这里用插件自带的）各自的修改时间。
    persona_mtime = BUNDLED_PERSONA_FILE.stat().st_mtime_ns
    key           = str(plugin_dir)
    assert state.get_runtime(key) is refreshed
    assert state.get_runtime_mtime(key) == (-1, persona_mtime)
    assert state.get_runtime_revision(key) == 2


def test_local_ids_survive_restarts_and_idle_reset(tmp_path):
    for index in range(3):
        state = ChatRuntimeState()
        state.memory_store.bind_data_dir(tmp_path)
        number = state.fetch_and_increment_local_id("g1")
        assert number == index + 1
        state.memory_store.append(
            "g1", role="user", name="u", content=str(index), local_id=f"m{number}"
        )
        state.memory_store.persist("g1")
        state.clear_transient_chat_state("g1")
        assert state.fetch_and_increment_local_id("g1") == index + 2
    assert len(state.memory_store.get("g1")) == 3


def test_memory_db_bind_clears_previous_store_when_switching_dirs(tmp_path):
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    first_dir  = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()

    db = MemoryDB()
    db.bind(first_dir)
    db.upsert_text(
        doc_id = "doc1",
        text   = "旧目录里的记忆",
        meta   = {"type": "knowledge", "global_approved": True},
    )
    assert db.query_knowledge("旧目录", top_k=5)

    db.bind(second_dir)

    assert db.query_knowledge("旧目录", top_k=5) == []
