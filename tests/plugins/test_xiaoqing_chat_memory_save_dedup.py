from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from plugins.xiaoqing_chat.memory.vector_store import VectorDoc


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
