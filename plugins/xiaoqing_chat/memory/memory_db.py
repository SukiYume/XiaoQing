from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .vector_store import VectorDoc, VectorStore, write_vector_store_files

_logger = logging.getLogger("plugin.xiaoqing_chat")


@dataclass(frozen=True)
class RetrievedItem:
    doc_id: str
    text: str
    score: float
    meta: dict[str, Any]


class MemoryDB:
    def __init__(self) -> None:
        self._store = VectorStore(dim=2048)
        self._loaded_dir: Path | None = None
        self._dirty = False
        self._dirty_version = 0
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()

    def bind(self, data_dir: Path) -> None:
        with self._lock:
            if self._loaded_dir and self._loaded_dir == data_dir:
                return
            self._loaded_dir = data_dir
            self._dirty = False
            self._store = VectorStore(dim=2048)
            vdb_dir = data_dir / "vdb"
            if vdb_dir.exists():
                self._store.load(vdb_dir, name="memory")

    def save(self) -> None:
        with self._save_lock:
            with self._lock:
                loaded_dir = self._loaded_dir
                if not loaded_dir:
                    return
                self._store.build()
                docs = self._store.all_docs()
                dim = int(self._store.dim)
                mat = self._store._matrix
                if mat is None:
                    mat = np.zeros((0, dim), dtype=np.float32)
                else:
                    mat = mat.copy()
                save_version = self._dirty_version
            vdb_dir = loaded_dir / "vdb"
            try:
                write_vector_store_files(
                    dir_path=vdb_dir, name="memory", docs=docs, dim=dim, matrix=mat
                )
            except Exception as exc:
                _logger.warning("xiaoqing_chat memory_db save failed: %s", type(exc).__name__)
                return
            with self._lock:
                if self._dirty_version == save_version:
                    self._dirty = False

    def is_dirty(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    def get(self, doc_id: str) -> RetrievedItem | None:
        with self._lock:
            doc = self._store.get_doc(doc_id)
        if not doc:
            return None
        return RetrievedItem(doc_id=doc.doc_id, text=doc.text, score=1.0, meta=doc.meta)

    def upsert_text(self, *, doc_id: str, text: str, meta: dict[str, Any]) -> None:
        with self._lock:
            self._store.upsert(
                VectorDoc(doc_id=doc_id, text=text, meta={**meta, "updated_at": time.time()})
            )
            self._dirty = True
            self._dirty_version += 1

    def query(
        self,
        question: str,
        *,
        chat_id: str,
        top_k: int = 5,
        min_score: float = 0.12,
        type_filter: str | None = None,
        meta_filter: dict[str, Any] | None = None,
    ) -> list[RetrievedItem]:
        """Query tenant-scoped memories; a chat scope is never optional."""
        scoped_chat_id = str(chat_id or "").strip()
        if not scoped_chat_id:
            raise ValueError("chat_id is required for memory queries")

        def in_scope(doc: VectorDoc) -> bool:
            if str(doc.meta.get("chat_id", "") or "") != scoped_chat_id:
                return False
            if type_filter and str(doc.meta.get("type", "")) != type_filter:
                return False
            return not meta_filter or all(
                doc.meta.get(key) == value for key, value in meta_filter.items()
            )

        with self._lock:
            results = self._store.query(
                question, top_k=top_k, min_score=min_score, predicate=in_scope
            )
        out: list[RetrievedItem] = []
        for doc, score in results:
            out.append(RetrievedItem(doc_id=doc.doc_id, text=doc.text, score=score, meta=doc.meta))
        return out

    def query_global(
        self,
        question: str,
        *,
        top_k: int = 5,
        min_score: float = 0.12,
        type_filter: str,
    ) -> list[RetrievedItem]:
        """Query only explicit global, reviewed knowledge types."""
        if type_filter not in {"knowledge", "word_def"}:
            raise ValueError("global memory queries only allow reviewed knowledge types")

        def globally_visible(doc: VectorDoc) -> bool:
            return (
                str(doc.meta.get("type", "")) == type_filter
                and doc.meta.get("global_approved") is True
            )

        with self._lock:
            results = self._store.query(
                question, top_k=top_k, min_score=min_score, predicate=globally_visible
            )
        return [
            RetrievedItem(doc_id=doc.doc_id, text=doc.text, score=score, meta=doc.meta)
            for doc, score in results
        ]
