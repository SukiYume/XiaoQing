from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

_RE_WS = re.compile(r"\s+")

@dataclass(frozen=True)
class VectorDoc:
    doc_id: str
    text: str
    meta: dict[str, Any]

class VectorStore:
    def __init__(self, *, dim: int = 2048) -> None:
        self._dim = dim
        self._docs: list[VectorDoc] = []
        self._matrix: Optional[np.ndarray] = None
        self._id_to_idx: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def dim(self) -> int:
        return self._dim

    def upsert(self, doc: VectorDoc) -> None:
        idx = self._id_to_idx.get(doc.doc_id)
        if idx is None:
            self._id_to_idx[doc.doc_id] = len(self._docs)
            self._docs.append(doc)
        else:
            self._docs[idx] = doc
        self._matrix = None

    def delete(self, doc_id: str) -> None:
        idx = self._id_to_idx.get(doc_id)
        if idx is None:
            return
        self._docs.pop(idx)
        self._reindex()
        self._matrix = None

    def all_docs(self) -> list[VectorDoc]:
        return list(self._docs)

    def get_doc(self, doc_id: str) -> Optional[VectorDoc]:
        idx = self._id_to_idx.get(doc_id)
        if idx is None:
            return None
        if idx < 0 or idx >= len(self._docs):
            return None
        return self._docs[idx]

    def build(self) -> None:
        if self._matrix is not None:
            return
        if not self._docs:
            self._matrix = np.zeros((0, self._dim), dtype=np.float32)
            return
        mat = np.zeros((len(self._docs), self._dim), dtype=np.float32)
        for i, d in enumerate(self._docs):
            mat[i, :] = _embed(d.text, dim=self._dim)
        mat = _l2_normalize(mat)
        self._matrix = mat

    def query(
        self,
        text: str,
        *,
        top_k: int = 5,
        min_score: float = 0.12,
    ) -> list[tuple[VectorDoc, float]]:
        if top_k <= 0:
            return []
        self.build()
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []
        q = _embed(text, dim=self._dim).astype(np.float32)
        q = _l2_normalize(q.reshape(1, -1))[0]
        scores = self._matrix @ q
        if scores.size == 0:
            return []
        idxs = np.argsort(scores)[::-1][:top_k]
        out: list[tuple[VectorDoc, float]] = []
        for idx in idxs.tolist():
            score = float(scores[idx])
            if score < min_score:
                continue
            out.append((self._docs[idx], score))
        return out

    def save(self, dir_path: Path, *, name: str) -> None:
        dir_path.mkdir(parents=True, exist_ok=True)
        docs_path = dir_path / f"{name}.docs.json"
        npz_path = dir_path / f"{name}.vecs.npz"

        docs_payload = [asdict(d) for d in self._docs]
        docs_path.write_text(json.dumps(docs_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        self.build()
        mat = self._matrix if self._matrix is not None else np.zeros((0, self._dim), dtype=np.float32)
        np.savez_compressed(npz_path, dim=np.int32(self._dim), matrix=mat)

    def load(self, dir_path: Path, *, name: str) -> None:
        docs_path = dir_path / f"{name}.docs.json"
        npz_path = dir_path / f"{name}.vecs.npz"
        self._docs = []
        self._id_to_idx = {}
        self._matrix = None

        if docs_path.exists():
            try:
                raw = json.loads(docs_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        doc_id = str(item.get("doc_id", "")).strip()
                        text = str(item.get("text", "")).strip()
                        meta = item.get("meta", {})
                        if doc_id and text:
                            if not isinstance(meta, dict):
                                meta = {}
                            self._id_to_idx[doc_id] = len(self._docs)
                            self._docs.append(VectorDoc(doc_id=doc_id, text=text, meta=meta))
            except Exception:
                # If docs fail to load, we have nothing.
                self._docs = []
                self._id_to_idx = {}
        
        # Only attempt to load vector cache if we have docs
        if self._docs and npz_path.exists():
            try:
                npz = np.load(npz_path, allow_pickle=False)
                dim = int(npz.get("dim", self._dim))
                matrix = npz.get("matrix")
                if isinstance(matrix, np.ndarray):
                    # Validate consistency: matrix rows must match doc count
                    if matrix.shape[0] == len(self._docs) and dim == self._dim:
                        self._dim = dim
                        self._matrix = matrix.astype(np.float32, copy=False)
                    else:
                        # Inconsistent cache, force rebuild
                        self._matrix = None
            except Exception:
                self._matrix = None

    def _reindex(self) -> None:
        self._id_to_idx = {d.doc_id: i for i, d in enumerate(self._docs)}

def _tokenize(text: str) -> list[str]:
    s = _RE_WS.sub(" ", (text or "").strip())
    if not s:
        return []
    tokens: list[str] = []
    buf: list[str] = []
    for ch in s:
        if "\u4e00" <= ch <= "\u9fff":
            if buf:
                tokens.extend("".join(buf).split())
                buf = []
            tokens.append(ch)
        else:
            buf.append(ch)
    if buf:
        tokens.extend("".join(buf).split())
    out: list[str] = []
    for t in tokens:
        t = t.strip().lower()
        if not t:
            continue
        out.append(t)
    return out

def _char_ngrams(tokens: Sequence[str]) -> list[str]:
    out: list[str] = []
    for t in tokens:
        if len(t) == 1:
            out.append(t)
            continue
        if all("\u4e00" <= c <= "\u9fff" for c in t) and len(t) >= 2:
            for i in range(len(t) - 1):
                out.append(t[i : i + 2])
        else:
            out.append(t)
    return out

def _hash32(s: str) -> int:
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h

def _embed(text: str, *, dim: int) -> np.ndarray:
    tokens = _char_ngrams(_tokenize(text))
    vec = np.zeros((dim,), dtype=np.float32)
    if not tokens:
        return vec
    for t in tokens:
        idx = _hash32(t) % dim
        vec[idx] += 1.0
    vec = np.log1p(vec)
    return vec

def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    if mat.size == 0:
        return mat.astype(np.float32, copy=False)
    if mat.ndim == 1:
        denom = float(np.linalg.norm(mat)) or 1.0
        return (mat / denom).astype(np.float32, copy=False)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32, copy=False)
