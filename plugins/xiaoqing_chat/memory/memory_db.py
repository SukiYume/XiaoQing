"""维护长期记忆文档、人物资料和对应的向量检索索引。"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..store_base import LockedDirtyStateMixin
from .vector_store import VectorDoc, VectorStore, write_vector_store_files

_logger                      = logging.getLogger("plugin.xiaoqing_chat")
_LEGACY_PERSON_ID_RE         = re.compile(r"^person:\d+:\d+$")
_CONFIGURED_KNOWLEDGE_PREFIX = "kb:"


def normalize_memory_chat_id(chat_id: object) -> str:
    scoped_chat_id = str(chat_id or "").strip()
    if not scoped_chat_id or len(scoped_chat_id) > 256:
        raise ValueError("invalid memory chat scope")
    return scoped_chat_id


def person_fact_doc_id(
    *,
    chat_id: object,
    subject_id: int,
    subject_name: str,
    fact: str,
) -> str:
    scoped_chat_id  = normalize_memory_chat_id(chat_id)
    normalized_fact = str(fact or "").strip()
    if subject_id <= 0 or not normalized_fact:
        raise ValueError("invalid person fact identity")
    normalized_name = str(subject_name or "").strip()
    scope_hash      = hashlib.sha256(scoped_chat_id.encode("utf-8")).hexdigest()[:16]
    material        = f"{scoped_chat_id}\0{subject_id}\0{normalized_name}\0{normalized_fact}"
    fact_hash       = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"person:v2:{scope_hash}:{subject_id}:{fact_hash}"


@dataclass(frozen=True)
class RetrievedItem:
    doc_id: str
    text: str
    score: float
    meta: dict[str, Any]


class MemoryDB(LockedDirtyStateMixin):
    def __init__(self) -> None:
        self._store = VectorStore(dim=2048)
        self._loaded_dir: Path | None = None
        self._dirty                   = False
        self._dirty_version           = 0
        self._lock                    = threading.RLock()
        self._save_lock               = threading.Lock()

    def bind(self, data_dir: Path) -> None:
        with self._lock:
            if self._loaded_dir and self._loaded_dir == data_dir:
                return
            self._loaded_dir = data_dir
            self._dirty      = False
            self._store = VectorStore(dim=2048)
            vdb_dir = data_dir / "vdb"
            if vdb_dir.exists():
                self._store.load(vdb_dir, name="memory")
            self._migrate_legacy_person_info_locked()

    @staticmethod
    def _legacy_person_fact_text(text: str) -> str:
        value = str(text or "").strip()
        if "：" in value:
            value = value.split("：", 1)[1]
        if "\n证据：" in value:
            value = value.split("\n证据：", 1)[0]
        return value.strip()

    def _migrate_legacy_person_info_locked(self) -> None:
        """重建旧范围事实的键，并隔离缺少有效范围的记录。"""
        changed = False
        for doc in list(self._store.all_docs()):
            if not _LEGACY_PERSON_ID_RE.fullmatch(doc.doc_id):
                continue
            if str(doc.meta.get("type", "")) != "person_info":
                continue
            meta = dict(doc.meta)
            try:
                chat_id      = normalize_memory_chat_id(meta.get("chat_id"))
                subject_id   = int(meta.get("subject_id", 0) or 0)
                subject_name = str(meta.get("subject_name", "") or "").strip()
                fact         = self._legacy_person_fact_text(doc.text)
                new_doc_id   = person_fact_doc_id(
                    chat_id      = chat_id,
                    subject_id   = subject_id,
                    subject_name = subject_name,
                    fact         = fact,
                )
            except (TypeError, ValueError):
                meta["type"]              = "quarantined_person_info"
                meta["quarantine_reason"] = "invalid_scope_or_identity"
                self._store.upsert(VectorDoc(doc_id=doc.doc_id, text=doc.text, meta=meta))
                changed = True
                continue

            meta.update(
                {
                    "type": "person_info",
                    "chat_id": chat_id,
                    "subject_id": subject_id,
                    "schema_version": 2,
                }
            )
            self._store.upsert(VectorDoc(doc_id=new_doc_id, text=doc.text, meta=meta))
            if new_doc_id != doc.doc_id:
                self._store.delete(doc.doc_id)
            changed = True
        if changed:
            self._dirty = True
            self._dirty_version += 1

    def save(self) -> None:
        with self._save_lock:
            with self._lock:
                loaded_dir = self._loaded_dir
                if not loaded_dir:
                    return
                self._store.build()
                docs = self._store.all_docs()
                dim  = int(self._store.dim)
                mat  = self._store._matrix
                mat = np.zeros((0, dim), dtype=np.float32) if mat is None else mat.copy()
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

    def get(self, doc_id: str) -> RetrievedItem | None:
        with self._lock:
            doc = self._store.get_doc(doc_id)
        if not doc:
            return None
        return RetrievedItem(doc_id=doc.doc_id, text=doc.text, score=1.0, meta=doc.meta)

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            if self._store.get_doc(doc_id) is None:
                return False
            self._store.delete(doc_id)
            self._dirty = True
            self._dirty_version += 1
            return True

    def delete_chat(self, chat_id: object) -> int:
        """删除一个会话作用域内的全部向量记忆，不影响其他会话或全局知识。"""

        scoped_chat_id = normalize_memory_chat_id(chat_id)
        with self._lock:
            doc_ids = [
                doc.doc_id
                for doc in self._store.all_docs()
                if str(doc.meta.get("chat_id", "") or "") == scoped_chat_id
            ]
            if not doc_ids:
                return 0
            for doc_id in doc_ids:
                self._store.delete(doc_id)
            self._dirty = True
            self._dirty_version += 1
            return len(doc_ids)

    def upsert_text(self, *, doc_id: str, text: str, meta: dict[str, Any]) -> None:
        with self._lock:
            self._store.upsert(
                VectorDoc(doc_id=doc_id, text=text, meta={**meta, "updated_at": time.time()})
            )
            self._dirty = True
            self._dirty_version += 1

    def replace_configured_knowledge(self, documents: Sequence[VectorDoc]) -> bool:
        """原子替换完整的 ``kb:`` 文档命名空间。

        调用方进入本方法前必须完成文件读取、解码、分块和预算检查。替代存储先在锁外
        完整构造，再于同一把锁下发布，因此校验或构建失败不会暴露只刷新了一部分的
        知识集合。
        """
        requested: list[VectorDoc] = []
        requested_ids: set[str]    = set()
        for document in documents:
            if not document.doc_id.startswith(_CONFIGURED_KNOWLEDGE_PREFIX):
                raise ValueError("configured knowledge document has an invalid namespace")
            if not document.text.strip():
                raise ValueError("configured knowledge document is empty")
            if document.doc_id in requested_ids:
                raise ValueError("configured knowledge document IDs must be unique")
            meta = dict(document.meta)
            meta.pop("updated_at", None)
            if meta.get("type") != "knowledge" or meta.get("global_approved") is not True:
                raise ValueError("configured knowledge document is not globally approved")
            requested_ids.add(document.doc_id)
            requested.append(VectorDoc(doc_id=document.doc_id, text=document.text, meta=meta))

        with self._lock:
            current = self._store.all_docs()
            managed = {
                document.doc_id: document
                for document in current
                if document.doc_id.startswith(_CONFIGURED_KNOWLEDGE_PREFIX)
                and document.meta.get("type") == "knowledge"
            }
            retained = [document for document in current if document.doc_id not in managed]
            if requested_ids.intersection(document.doc_id for document in retained):
                raise ValueError("configured knowledge ID conflicts with another memory type")

            published_at              = time.time()
            resolved: list[VectorDoc] = []
            for document in requested:
                previous      = managed.get(document.doc_id)
                previous_meta = (
                    {key: value for key, value in previous.meta.items() if key != "updated_at"}
                    if previous is not None
                    else None
                )
                if (
                    previous is not None
                    and previous.text == document.text
                    and previous_meta == document.meta
                ):
                    resolved.append(previous)
                else:
                    resolved.append(
                        VectorDoc(
                            doc_id = document.doc_id,
                            text   = document.text,
                            meta   = {**document.meta, "updated_at": published_at},
                        )
                    )

            replacement_docs = [*retained, *resolved]
            if replacement_docs == current:
                return False

            replacement = VectorStore(dim=self._store.dim)
            for document in replacement_docs:
                replacement.upsert(document)
            self._store = replacement
            self._dirty = True
            self._dirty_version += 1
            return True

    def query(
        self,
        question: str,
        *,
        chat_id: str,
        top_k: int                         = 5,
        min_score: float                   = 0.12,
        type_filter: str | None            = None,
        meta_filter: dict[str, Any] | None = None,
    ) -> list[RetrievedItem]:
        """查询租户范围内的记忆；会话范围始终必填。"""
        scoped_chat_id = str(chat_id or "").strip()
        if not scoped_chat_id:
            raise ValueError("chat_id is required for memory queries")

        def in_scope(doc: VectorDoc) -> bool:
            # 隔离状态只供维护读取，所有面向聊天的检索统一排除。
            if str(doc.meta.get("type", "")).startswith("quarantined_"):
                return False
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
        top_k: int       = 5,
        min_score: float = 0.12,
        type_filter: str,
    ) -> list[RetrievedItem]:
        """只查询显式全局且已经审核的知识类型。"""
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
