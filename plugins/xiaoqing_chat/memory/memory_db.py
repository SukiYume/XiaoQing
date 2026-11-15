"""管理员知识库的向量检索索引（``kb:`` 文档）。

已有部署的索引里可能还有旧版本写入的会话记忆文档（人物事实、话题摘要）。聊天不读写这些文档：
统一记忆在每个会话第一次推送时直接读文件导入（``legacy.py``），重置会话时按会话删除。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..store_base import LockedDirtyStateMixin
from .vector_store import VectorDoc, VectorStore, write_vector_store_files

_logger                      = logging.getLogger("plugin.xiaoqing_chat")
_CONFIGURED_KNOWLEDGE_PREFIX = "kb:"


def normalize_memory_chat_id(chat_id: object) -> str:
    scoped_chat_id = str(chat_id or "").strip()
    if not scoped_chat_id or len(scoped_chat_id) > 256:
        raise ValueError("invalid memory chat scope")
    return scoped_chat_id


@dataclass(frozen=True)
class RetrievedItem:
    doc_id: str
    text  : str
    score : float
    meta  : dict[str, Any]


class MemoryDB(LockedDirtyStateMixin):
    def __init__(self) -> None:
        self._store                   = VectorStore()
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
            self._store      = VectorStore()
            vdb_dir          = data_dir / "vdb"
            if vdb_dir.exists():
                self._store.load(vdb_dir, name="memory")

    def save(self) -> None:
        with self._save_lock:
            with self._lock:
                loaded_dir = self._loaded_dir
                if not loaded_dir:
                    return
                docs, mat = self._store.snapshot()
                dim          = int(self._store.dim)
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

    def delete_chat(self, chat_id: object) -> int:
        """删除一个会话作用域内的全部文档（旧版本写入的会话记忆），不影响其他会话或全局知识。"""

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

        调用方进入本方法前必须完成文件读取、解码、分块和预算检查。替代存储在存储锁内
        完整构造并发布，构建失败时保留原知识集合。
        """
        requested    : list[VectorDoc] = []
        requested_ids: set[str]        = set()
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

    def query_knowledge(
        self,
        question: str,
        *,
        top_k: int       = 5,
        min_score: float = 0.12,
    ) -> list[RetrievedItem]:
        """只查询管理员批准的知识文档。"""

        def globally_visible(doc: VectorDoc) -> bool:
            return (
                str(doc.meta.get("type", "")) == "knowledge"
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
