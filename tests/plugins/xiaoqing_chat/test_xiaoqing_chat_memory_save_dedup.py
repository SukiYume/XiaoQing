from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from plugins.xiaoqing_chat.memory import knowledge_base as knowledge_base_module
from plugins.xiaoqing_chat.memory.knowledge_base import (
    KnowledgeIndexError,
    ensure_knowledge_index,
)
from plugins.xiaoqing_chat.memory.memory_db import MemoryDB
from plugins.xiaoqing_chat.memory.vector_store import VectorDoc, VectorStore


def _knowledge_snapshot(db: MemoryDB) -> list[tuple[str, str, dict]]:
    return sorted(
        (item.doc_id, item.text, item.meta)
        for item in db.query_global(
            "configured knowledge",
            top_k=2048,
            min_score=-1.0,
            type_filter="knowledge",
        )
    )


def test_vector_store_save_uses_shared_write_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plugins.xiaoqing_chat.memory import vector_store as vector_store_module
    from plugins.xiaoqing_chat.memory.vector_store import VectorDoc, VectorStore

    calls: list[tuple[Path, str, Sequence[VectorDoc], int, tuple[int, ...]]] = []

    def fake_write(
        *, dir_path: Path, name: str, docs: Sequence[VectorDoc], dim: int, matrix: np.ndarray
    ) -> None:
        calls.append((dir_path, name, docs, dim, tuple(matrix.shape)))

    monkeypatch.setattr(vector_store_module, "write_vector_store_files", fake_write, raising=False)

    store = VectorStore(dim=8)
    store.upsert(VectorDoc(doc_id="d1", text="hello", meta={"type": "knowledge"}))

    store.save(tmp_path, name="memory")

    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert calls[0][1] == "memory"
    assert calls[0][3] == 8
    assert calls[0][2][0].doc_id == "d1"


def test_memory_db_save_uses_shared_write_path_and_clears_dirty_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plugins.xiaoqing_chat.memory import memory_db as memory_db_module
    from plugins.xiaoqing_chat.memory import vector_store as vector_store_module
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    calls: list[tuple[Path, str, Sequence[VectorDoc], int, tuple[int, ...]]] = []

    def fake_write(
        *, dir_path: Path, name: str, docs: Sequence[VectorDoc], dim: int, matrix: np.ndarray
    ) -> None:
        calls.append((dir_path, name, docs, dim, tuple(matrix.shape)))

    monkeypatch.setattr(vector_store_module, "write_vector_store_files", fake_write, raising=False)
    monkeypatch.setattr(memory_db_module, "write_vector_store_files", fake_write, raising=False)

    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    assert db.is_dirty() is True

    db.save()

    assert len(calls) == 1
    assert calls[0][0] == (tmp_path / "vdb")
    assert calls[0][1] == "memory"
    assert calls[0][2][0].doc_id == "doc-1"
    assert db.is_dirty() is False


def test_memory_db_save_keeps_dirty_when_shared_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plugins.xiaoqing_chat.memory import memory_db as memory_db_module
    from plugins.xiaoqing_chat.memory import vector_store as vector_store_module
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    logger = MagicMock()

    def fake_write(
        *,
        dir_path: Path,
        name: str,
        docs: Sequence[VectorDoc],
        dim: int,
        matrix: np.ndarray,
    ) -> None:
        _ = (dir_path, name, docs, dim, matrix)
        raise OSError("disk full")

    monkeypatch.setattr(memory_db_module, "_logger", logger, raising=False)
    monkeypatch.setattr(vector_store_module, "write_vector_store_files", fake_write, raising=False)
    monkeypatch.setattr(memory_db_module, "write_vector_store_files", fake_write, raising=False)

    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    db.save()

    assert db.is_dirty() is True
    logger.warning.assert_called_once()


def test_memory_db_save_keeps_dirty_when_new_write_happens_during_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plugins.xiaoqing_chat.memory import memory_db as memory_db_module
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(doc_id="doc-1", text="hello", meta={"type": "knowledge"})

    did_race_write = False

    def fake_write(
        *,
        dir_path: Path,
        name: str,
        docs: Sequence[VectorDoc],
        dim: int,
        matrix: np.ndarray,
    ) -> None:
        nonlocal did_race_write
        _ = (dir_path, name, docs, dim, matrix)
        if not did_race_write:
            did_race_write = True
            db.upsert_text(doc_id="doc-2", text="new info", meta={"type": "knowledge"})

    monkeypatch.setattr(memory_db_module, "write_vector_store_files", fake_write, raising=False)

    db.save()

    assert did_race_write is True
    assert db.is_dirty() is True


def test_memory_db_delete_chat_removes_only_target_scope_and_persists(
    tmp_path: Path,
) -> None:
    db = MemoryDB()
    db.bind(tmp_path)
    db.upsert_text(
        doc_id="topic-target",
        text="目标群旧话题",
        meta={"type": "topic_summary", "chat_id": "g-target"},
    )
    db.upsert_text(
        doc_id="person-target",
        text="目标群人物信息",
        meta={"type": "person_info", "chat_id": "g-target"},
    )
    db.upsert_text(
        doc_id="topic-other",
        text="其他群旧话题",
        meta={"type": "topic_summary", "chat_id": "g-other"},
    )
    db.upsert_text(
        doc_id="global-knowledge",
        text="审核后的全局知识",
        meta={"type": "knowledge", "global_approved": True},
    )
    db.save()

    assert db.delete_chat("g-target") == 2
    assert db.delete_chat("g-target") == 0
    db.save()

    reloaded = MemoryDB()
    reloaded.bind(tmp_path)
    assert reloaded.query("旧话题", chat_id="g-target", min_score=-1.0) == []
    assert [
        item.doc_id for item in reloaded.query("旧话题", chat_id="g-other", min_score=-1.0)
    ] == ["topic-other"]
    assert [
        item.doc_id
        for item in reloaded.query_global(
            "全局知识",
            min_score=-1.0,
            type_filter="knowledge",
        )
    ] == ["global-knowledge"]


def test_configured_knowledge_refresh_removes_shortened_removed_and_deleted_sources(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    data_dir = tmp_path / "data"
    plugin_dir.mkdir()
    first = plugin_dir / "first.txt"
    second = plugin_dir / "second.txt"
    first.write_text("alpha\n\nbeta", encoding="utf-8")
    second.write_text("gamma", encoding="utf-8")

    db = MemoryDB()
    db.bind(data_dir)
    db.upsert_text(
        doc_id="reviewed-public",
        text="public reference",
        meta={"type": "knowledge", "global_approved": True},
    )

    assert (
        ensure_knowledge_index(
            memory_db=db,
            data_dir=data_dir,
            plugin_dir=plugin_dir,
            files=["first.txt", "second.txt"],
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
            memory_db=db,
            data_dir=data_dir,
            plugin_dir=plugin_dir,
            files=["first.txt", "second.txt"],
        )
        is False
    )

    first.write_text("alpha revised", encoding="utf-8")
    assert (
        ensure_knowledge_index(
            memory_db=db,
            data_dir=data_dir,
            plugin_dir=plugin_dir,
            files=["first.txt"],
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
            memory_db=db,
            data_dir=data_dir,
            plugin_dir=plugin_dir,
            files=["first.txt"],
        )
        is True
    )
    assert [text for _doc_id, text, _meta in _knowledge_snapshot(db)] == ["public reference"]
    assert (data_dir / "vdb" / "memory.docs.json").is_file()


def test_configured_knowledge_metadata_never_stores_absolute_source_paths(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
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
        memory_db=db,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_dir,
        files=["docs/guide.txt", str(external.resolve())],
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    data_dir = tmp_path / "data"
    plugin_dir.mkdir()
    baseline_file = plugin_dir / "baseline.txt"
    baseline_file.write_text("last complete snapshot", encoding="utf-8")
    db = MemoryDB()
    assert (
        ensure_knowledge_index(
            memory_db=db,
            data_dir=data_dir,
            plugin_dir=plugin_dir,
            files=["baseline.txt"],
        )
        is True
    )
    baseline = _knowledge_snapshot(db)

    first = plugin_dir / "first.txt"
    second = plugin_dir / "second.txt"
    oversized = plugin_dir / "oversized.txt"
    chunked = plugin_dir / "chunked.txt"
    invalid_utf8 = plugin_dir / "invalid.txt"
    first.write_text("aaa", encoding="utf-8")
    second.write_text("bbb", encoding="utf-8")
    oversized.write_text("abcd", encoding="utf-8")
    chunked.write_text("one\n\ntwo", encoding="utf-8")
    invalid_utf8.write_bytes(b"\xff")

    def reject(files: list[str], **limits: int) -> None:
        with monkeypatch.context() as scoped:
            for name, value in limits.items():
                scoped.setattr(knowledge_base_module, name, value)
            with pytest.raises(KnowledgeIndexError):
                ensure_knowledge_index(
                    memory_db=db,
                    data_dir=data_dir,
                    plugin_dir=plugin_dir,
                    files=files,
                )
        assert _knowledge_snapshot(db) == baseline

    reject(["first.txt", "second.txt"], MAX_KNOWLEDGE_FILES=1)
    reject(["oversized.txt"], MAX_KNOWLEDGE_FILE_BYTES=3)
    reject(
        ["first.txt", "second.txt"],
        MAX_KNOWLEDGE_FILE_BYTES=10,
        MAX_TOTAL_KNOWLEDGE_BYTES=5,
    )
    reject(["chunked.txt"], MAX_KNOWLEDGE_CHUNKS=1)
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


def test_runtime_refresh_publishes_an_empty_snapshot_when_knowledge_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from plugins.xiaoqing_chat import helper_utils

    plugin_dir = tmp_path / "plugin"
    data_dir = tmp_path / "data"
    plugin_dir.mkdir()
    memory_db = MagicMock()
    state = SimpleNamespace(
        memory_db=memory_db,
        get_runtime=MagicMock(return_value=None),
        get_runtime_mtime=MagicMock(return_value=None),
        get_runtime_revision=MagicMock(return_value=None),
        set_runtime=MagicMock(),
    )
    config = SimpleNamespace(
        ban_regex=[],
        knowledge=SimpleNamespace(enable_knowledge=False, files=["stale.txt"]),
    )
    ensure = MagicMock(return_value=True)
    monkeypatch.setattr(helper_utils, "_state", lambda: state)
    monkeypatch.setattr(
        helper_utils,
        "load_xiaoqing_chat_config",
        lambda **_kwargs: config,
    )
    monkeypatch.setattr(knowledge_base_module, "ensure_knowledge_index", ensure)

    from tests.helpers.settings_snapshot import with_settings_reader

    helper_utils._load_runtime(
        with_settings_reader(SimpleNamespace(plugin_dir=plugin_dir, data_dir=data_dir, config={}))
    )

    ensure.assert_called_once_with(
        memory_db=memory_db,
        data_dir=data_dir,
        plugin_dir=plugin_dir,
        files=(),
    )


def test_runtime_refreshes_when_settings_revision_changes_without_file_mtime_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from plugins.xiaoqing_chat import helper_utils
    from tests.helpers.settings_snapshot import settings_snapshot

    plugin_dir = tmp_path / "plugin"
    data_dir = tmp_path / "data"
    plugin_dir.mkdir()
    cached_runtime = object()
    state = SimpleNamespace(
        memory_db=MagicMock(),
        get_runtime=MagicMock(return_value=cached_runtime),
        get_runtime_mtime=MagicMock(return_value=-1),
        get_runtime_revision=MagicMock(return_value=1),
        set_runtime=MagicMock(),
    )
    config = SimpleNamespace(
        ban_regex=[],
        knowledge=SimpleNamespace(enable_knowledge=False, files=[]),
    )
    load_config = MagicMock(return_value=config)
    ensure = MagicMock(return_value=True)
    settings = settings_snapshot(
        config={"plugins": {"xiaoqing_chat": {"changed": True}}},
        revision=2,
    )
    context = SimpleNamespace(
        plugin_dir=plugin_dir,
        data_dir=data_dir,
        get_settings_snapshot=lambda: settings,
    )
    monkeypatch.setattr(helper_utils, "_state", lambda: state)
    monkeypatch.setattr(helper_utils, "load_xiaoqing_chat_config", load_config)
    monkeypatch.setattr(knowledge_base_module, "ensure_knowledge_index", ensure)

    refreshed = helper_utils._load_runtime(context)

    assert refreshed is not cached_runtime
    load_config.assert_called_once_with(context_config=settings.config, plugin_dir=plugin_dir)
    state.set_runtime.assert_called_once_with(str(plugin_dir), refreshed, -1, 2)
