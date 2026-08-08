import json
from pathlib import Path

import numpy as np
import pytest

from plugins.xiaoqing_chat.memory.vector_store import (
    VectorDoc,
    VectorStore,
    _docs_content_digest,
    write_vector_store_files,
)


def _write_vector_cache(
    root: Path,
    *,
    matrix: np.ndarray,
    dim: np.ndarray | np.generic = np.int32(4),
) -> None:
    docs = [{"doc_id": "d1", "text": "hello", "meta": {"type": "knowledge"}}]
    (root / "memory.docs.json").write_text(
        json.dumps(docs, ensure_ascii=False),
        encoding="utf-8",
    )
    digest = _docs_content_digest(
        [VectorDoc(doc_id="d1", text="hello", meta={"type": "knowledge"})]
    )
    np.savez_compressed(
        root / "memory.vecs.npz",
        dim=dim,
        matrix=matrix,
        docs_digest=np.asarray(digest),
    )


@pytest.mark.parametrize(
    "matrix",
    [
        np.array([1.0], dtype=np.float32),
        np.ones((1, 3), dtype=np.float32),
        np.array([["1", "0", "0", "0"]]),
        np.array([[1.0, 0.0, np.nan, 0.0]], dtype=np.float32),
        np.array([[1.0, 0.0, np.inf, 0.0]], dtype=np.float32),
        np.array([[1.0 + 1.0j, 0.0, 0.0, 0.0]], dtype=np.complex64),
    ],
    ids=[
        "one-dimensional",
        "wrong-column-count",
        "non-numeric-dtype",
        "nan",
        "infinity",
        "complex",
    ],
)
def test_invalid_vector_cache_is_discarded_and_safely_rebuilt(tmp_path, matrix):
    _write_vector_cache(tmp_path, matrix=matrix)
    store = VectorStore(dim=4)

    store.load(tmp_path, name="memory")

    assert store._matrix is None
    results = store.query("hello", top_k=1, min_score=-1.0)
    assert [document.doc_id for document, _score in results] == ["d1"]
    assert store._matrix is not None
    assert store._matrix.shape == (1, 4)
    assert np.isfinite(store._matrix).all()


@pytest.mark.parametrize(
    "dim",
    [
        np.array([4, 4], dtype=np.int32),
        np.array(3, dtype=np.int32),
    ],
    ids=["non-scalar-dimension", "dimension-mismatch"],
)
def test_invalid_dimension_metadata_discards_vector_cache(tmp_path, dim):
    _write_vector_cache(
        tmp_path,
        matrix=np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32),
        dim=dim,
    )
    store = VectorStore(dim=4)

    store.load(tmp_path, name="memory")

    assert store._matrix is None


def test_valid_vector_cache_loads_as_float32(tmp_path):
    _write_vector_cache(
        tmp_path,
        matrix=np.array([[1, 0, 0, 0]], dtype=np.int16),
    )
    store = VectorStore(dim=4)

    store.load(tmp_path, name="memory")

    assert store._matrix is not None
    assert store._matrix.shape == (1, 4)
    assert store._matrix.dtype == np.float32


def test_same_shape_vector_cache_with_wrong_document_digest_is_discarded(tmp_path):
    _write_vector_cache(
        tmp_path,
        matrix=np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32),
    )
    with np.load(tmp_path / "memory.vecs.npz", allow_pickle=False) as cache:
        dim = cache["dim"]
        matrix = cache["matrix"]
    np.savez_compressed(
        tmp_path / "memory.vecs.npz",
        dim=dim,
        matrix=matrix,
        docs_digest=np.asarray("0" * 64),
    )
    store = VectorStore(dim=4)

    store.load(tmp_path, name="memory")

    assert store._matrix is None


def test_vector_store_write_persists_matching_digest(tmp_path):
    docs = [VectorDoc(doc_id="d1", text="hello", meta={"type": "knowledge"})]
    matrix = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)

    write_vector_store_files(
        dir_path=tmp_path,
        name="memory",
        docs=docs,
        dim=4,
        matrix=matrix,
    )

    with np.load(tmp_path / "memory.vecs.npz", allow_pickle=False) as cache:
        assert str(np.asarray(cache["docs_digest"]).item()) == _docs_content_digest(docs)


def test_vector_store_load_keeps_unique_document_ids(tmp_path):
    (tmp_path / "memory.docs.json").write_text(
        json.dumps(
            [
                {"doc_id": "same", "text": "old", "meta": {"version": 1}},
                {"doc_id": "same", "text": "new", "meta": {"version": 2}},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = VectorStore(dim=4)

    store.load(tmp_path, name="memory")

    assert store.all_docs() == [VectorDoc(doc_id="same", text="new", meta={"version": 2})]
