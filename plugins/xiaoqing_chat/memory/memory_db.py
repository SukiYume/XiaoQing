from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .vector_store import VectorDoc, VectorStore, _embed, _l2_normalize

@dataclass(frozen=True)
class RetrievedItem:
    doc_id: str
    text: str
    score: float
    meta: dict[str, Any]

class MemoryDB:
    def __init__(self) -> None:
        self._store = VectorStore(dim=2048)
        self._loaded_dir: Optional[Path] = None
        self._dirty = False
        self._lock = threading.RLock()

    def bind(self, data_dir: Path) -> None:
        with self._lock:
            if self._loaded_dir and self._loaded_dir == data_dir:
                return
            self._loaded_dir = data_dir
            self._dirty = False
            vdb_dir = data_dir / "vdb"
            if vdb_dir.exists():
                self._store.load(vdb_dir, name="memory")

    def save(self) -> None:
        with self._lock:
            loaded_dir = self._loaded_dir
            if not loaded_dir:
                return
            # Ensure vectors are up to date before saving
            self._store.build()
            docs = self._store.all_docs()
            dim = int(self._store.dim)
            # Reuse the already-computed and normalized matrix
            mat = self._store._matrix
            if mat is None:
                mat = np.zeros((0, dim), dtype=np.float32)
        # Write files outside the lock
        vdb_dir = loaded_dir / "vdb"
        vdb_dir.mkdir(parents=True, exist_ok=True)
        docs_path = vdb_dir / "memory.docs.json"
        npz_path = vdb_dir / "memory.vecs.npz"

        docs_payload = [{"doc_id": d.doc_id, "text": d.text, "meta": d.meta} for d in docs]
        try:
            docs_path.write_text(json.dumps(docs_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            np.savez_compressed(npz_path, dim=np.int32(dim), matrix=mat)
        except Exception:
            # I/O failed — keep dirty flag so next schedule retries
            return
        # Only clear dirty flag after BOTH writes succeed
        with self._lock:
            self._dirty = False

    def is_dirty(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    def get(self, doc_id: str) -> Optional[RetrievedItem]:
        with self._lock:
            doc = self._store.get_doc(doc_id)
        if not doc:
            return None
        return RetrievedItem(doc_id=doc.doc_id, text=doc.text, score=1.0, meta=doc.meta)

    def upsert_text(self, *, doc_id: str, text: str, meta: dict[str, Any]) -> None:
        with self._lock:
            self._store.upsert(VectorDoc(doc_id=doc_id, text=text, meta={**meta, "updated_at": time.time()}))
            self._dirty = True

    def query(
        self,
        question: str,
        *,
        top_k: int = 5,
        min_score: float = 0.12,
        type_filter: Optional[str] = None,
        meta_filter: Optional[dict[str, Any]] = None,
    ) -> list[RetrievedItem]:
        with self._lock:
            results = self._store.query(question, top_k=top_k, min_score=min_score)
        out: list[RetrievedItem] = []
        for doc, score in results:
            t = str(doc.meta.get("type", ""))
            if type_filter and t != type_filter:
                continue
            if meta_filter:
                ok = True
                for k, v in meta_filter.items():
                    if doc.meta.get(k) != v:
                        ok = False
                        break
                if not ok:
                    continue
            out.append(RetrievedItem(doc_id=doc.doc_id, text=doc.text, score=score, meta=doc.meta))
        return out
