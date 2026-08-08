"""提供本地哈希向量索引，以及文档与矩阵的一致性缓存。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeAlias, cast

import numpy as np
from numpy.typing import NDArray

from core.atomic_store import atomic_write_bytes
from core.plugin_base import load_json, write_json

_RE_WS = re.compile(r"\s+")
FloatArray: TypeAlias = NDArray[np.float32]


@dataclass(frozen=True)
class VectorDoc:
    """向量索引中的稳定文档记录。"""

    doc_id: str
    text: str
    meta: dict[str, Any]


def _docs_content_digest(docs: Sequence[VectorDoc]) -> str:
    payload = json.dumps(
        [asdict(doc) for doc in docs],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_cached_matrix(
    matrix: object,
    *,
    expected_rows: int,
    expected_dim: int,
) -> FloatArray | None:
    if not isinstance(matrix, np.ndarray):
        return None
    if matrix.ndim != 2 or matrix.shape != (expected_rows, expected_dim):
        return None
    if not np.issubdtype(matrix.dtype, np.number):
        return None
    if np.issubdtype(matrix.dtype, np.complexfloating):
        return None
    with np.errstate(over="ignore", invalid="ignore"):
        normalized = cast(FloatArray, matrix.astype(np.float32, copy=False))
    if not np.isfinite(normalized).all():
        return None
    return normalized


class VectorStore:
    """维护唯一文档 ID，并在首次查询时惰性构建归一化矩阵。"""

    def __init__(self, *, dim: int = 2048) -> None:
        self._dim = dim
        self._docs: list[VectorDoc] = []
        self._matrix: FloatArray | None = None
        self._id_to_idx: dict[str, int] = {}

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

    def get_doc(self, doc_id: str) -> VectorDoc | None:
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
        mat: FloatArray = np.zeros((len(self._docs), self._dim), dtype=np.float32)
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
        predicate: Callable[[VectorDoc], bool] | None = None,
    ) -> list[tuple[VectorDoc, float]]:
        if top_k <= 0:
            return []
        self.build()
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []
        q: FloatArray = _embed(text, dim=self._dim)
        q = _l2_normalize(q.reshape(1, -1))[0]
        scores = self._matrix @ q
        if scores.size == 0:
            return []
        candidate_idxs = np.asarray(
            [index for index, doc in enumerate(self._docs) if predicate is None or predicate(doc)],
            dtype=np.int64,
        )
        if candidate_idxs.size == 0:
            return []
        candidate_scores = scores[candidate_idxs]
        ranked = np.argsort(candidate_scores)[::-1][:top_k]
        idxs = candidate_idxs[ranked]
        out: list[tuple[VectorDoc, float]] = []
        for idx in idxs.tolist():
            score = float(scores[idx])
            if score < min_score:
                continue
            out.append((self._docs[idx], score))
        return out

    def save(self, dir_path: Path, *, name: str) -> None:
        self.build()
        mat: FloatArray = (
            self._matrix if self._matrix is not None else np.zeros((0, self._dim), dtype=np.float32)
        )
        write_vector_store_files(
            dir_path=dir_path, name=name, docs=self._docs, dim=self._dim, matrix=mat
        )

    def load(self, dir_path: Path, *, name: str) -> None:
        docs_path = dir_path / f"{name}.docs.json"
        npz_path = dir_path / f"{name}.vecs.npz"
        self._docs = []
        self._id_to_idx = {}
        self._matrix = None

        if docs_path.exists():
            try:
                raw: object = load_json(docs_path, default=[])
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
                            # 加载也必须遵守运行时的唯一 ID 语义；重复项以后出现者为准。
                            self.upsert(
                                VectorDoc(
                                    doc_id=doc_id,
                                    text=text,
                                    meta={str(key): value for key, value in meta.items()},
                                )
                            )
            except OSError:
                # 文档加载失败时没有可用数据。
                self._docs = []
                self._id_to_idx = {}

        # 仅在已有文档时尝试加载向量缓存。
        if self._docs and npz_path.exists():
            try:
                with np.load(npz_path, allow_pickle=False) as npz:
                    if not {"dim", "matrix", "docs_digest"} <= set(npz.files):
                        return
                    dim_value = np.asarray(npz["dim"])
                    if dim_value.ndim != 0:
                        return
                    dim = int(dim_value)
                    if dim != self._dim:
                        return
                    digest_value = np.asarray(npz["docs_digest"])
                    if digest_value.ndim != 0 or str(digest_value.item()) != _docs_content_digest(
                        self._docs
                    ):
                        return
                    self._matrix = _validate_cached_matrix(
                        npz["matrix"],
                        expected_rows=len(self._docs),
                        expected_dim=self._dim,
                    )
            except (
                OSError,
                EOFError,
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
                zipfile.BadZipFile,
            ):
                # 向量矩阵只是可再生成缓存；缓存损坏时让后续查询按文档重建。
                self._matrix = None

    def _reindex(self) -> None:
        self._id_to_idx = {d.doc_id: i for i, d in enumerate(self._docs)}


def write_vector_store_files(
    *,
    dir_path: Path,
    name: str,
    docs: Sequence[VectorDoc],
    dim: int,
    matrix: FloatArray,
) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    docs_path = dir_path / f"{name}.docs.json"
    npz_path = dir_path / f"{name}.vecs.npz"
    docs_payload = [asdict(d) for d in docs]
    write_json(docs_path, docs_payload)
    buffer = io.BytesIO()
    np.savez_compressed(
        buffer,
        dim=np.int32(dim),
        matrix=matrix,
        docs_digest=np.asarray(_docs_content_digest(docs)),
    )
    atomic_write_bytes(npz_path, buffer.getvalue())


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
            out.extend(t[i : i + 2] for i in range(len(t) - 1))
        else:
            out.append(t)
    return out


def _hash32(s: str) -> int:
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _embed(text: str, *, dim: int) -> FloatArray:
    tokens = _char_ngrams(_tokenize(text))
    vec: FloatArray = np.zeros((dim,), dtype=np.float32)
    if not tokens:
        return vec
    for t in tokens:
        idx = _hash32(t) % dim
        vec[idx] += 1.0
    return cast(FloatArray, np.log1p(vec))


def _l2_normalize(mat: FloatArray) -> FloatArray:
    if mat.size == 0:
        return cast(FloatArray, mat.astype(np.float32, copy=False))
    if mat.ndim == 1:
        denom = float(np.linalg.norm(mat)) or 1.0
        return cast(FloatArray, (mat / denom).astype(np.float32, copy=False))
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return cast(FloatArray, (mat / norms).astype(np.float32, copy=False))
