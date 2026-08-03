"""对管理员批准的知识文件执行有界、全有或全无的索引更新。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .memory_db import MemoryDB
from .vector_store import VectorDoc

MAX_KNOWLEDGE_FILES = 32
MAX_KNOWLEDGE_FILE_BYTES = 1024 * 1024
MAX_TOTAL_KNOWLEDGE_BYTES = 4 * 1024 * 1024
MAX_KNOWLEDGE_CHUNKS = 512
KNOWLEDGE_CHUNK_CHARS = 800


class KnowledgeIndexError(ValueError):
    """配置的知识快照不安全时，在发布前抛出。"""


@dataclass(frozen=True)
class _KnowledgeSource:
    path: Path
    label: str
    identity: tuple[int, int, int, int]
    size: int


def _hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _split_chunks(text: str, *, max_len: int = KNOWLEDGE_CHUNK_CHARS) -> list[str]:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in normalized.split("\n\n") if part.strip()]
    chunks: list[str] = []
    for part in parts:
        if len(part) <= max_len:
            chunks.append(part)
            continue
        for start in range(0, len(part), max_len):
            chunk = part[start : start + max_len].strip()
            if chunk:
                chunks.append(chunk)
    return chunks


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))


def _logical_source_label(*, path: Path, plugin_root: Path) -> str:
    """生成可交给模型的稳定来源名，绝不包含插件根目录外的本地路径。"""
    try:
        return path.relative_to(plugin_root).as_posix()
    except ValueError:
        suffix = path.suffix.lower()
        if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
            suffix = ""
        return f"external:{_hash_id(str(path))}{suffix}"


def _resolve_sources(*, files: Sequence[str], plugin_dir: Path) -> list[_KnowledgeSource]:
    if isinstance(files, (str, bytes)):
        raise KnowledgeIndexError("knowledge files must be a sequence of paths")
    if len(files) > MAX_KNOWLEDGE_FILES:
        raise KnowledgeIndexError(
            f"knowledge file count exceeds the limit of {MAX_KNOWLEDGE_FILES}"
        )

    try:
        plugin_root = plugin_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise KnowledgeIndexError("plugin directory could not be resolved") from exc

    sources: list[_KnowledgeSource] = []
    seen: set[Path] = set()
    declared_bytes = 0
    for raw_path in files:
        if type(raw_path) is not str or not raw_path.strip():
            raise KnowledgeIndexError("knowledge file paths must be non-empty strings")
        path = Path(raw_path.strip())
        if not path.is_absolute():
            path = plugin_dir / path
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise KnowledgeIndexError("knowledge file path could not be resolved") from exc
        if path in seen:
            continue
        seen.add(path)

        try:
            info = path.stat()
        except FileNotFoundError:
            # 配置来源被删除时，应同步移除其旧分块。
            continue
        except OSError as exc:
            raise KnowledgeIndexError("knowledge file metadata could not be read") from exc
        if not stat.S_ISREG(info.st_mode):
            raise KnowledgeIndexError("knowledge source is not a regular file")
        size = int(info.st_size)
        if size > MAX_KNOWLEDGE_FILE_BYTES:
            raise KnowledgeIndexError(
                f"knowledge file exceeds the {MAX_KNOWLEDGE_FILE_BYTES}-byte limit"
            )
        declared_bytes += size
        if declared_bytes > MAX_TOTAL_KNOWLEDGE_BYTES:
            raise KnowledgeIndexError(
                f"knowledge files exceed the {MAX_TOTAL_KNOWLEDGE_BYTES}-byte total limit"
            )
        sources.append(
            _KnowledgeSource(
                path=path,
                label=_logical_source_label(path=path, plugin_root=plugin_root),
                identity=_stat_identity(info),
                size=size,
            )
        )
    return sources


def _read_source(source: _KnowledgeSource) -> bytes:
    try:
        with source.path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != source.identity:
                raise KnowledgeIndexError("knowledge file changed during preflight")
            payload = handle.read(MAX_KNOWLEDGE_FILE_BYTES + 1)
            finished = os.fstat(handle.fileno())
    except KnowledgeIndexError:
        raise
    except OSError as exc:
        raise KnowledgeIndexError("knowledge file could not be read") from exc

    if _stat_identity(finished) != _stat_identity(opened):
        raise KnowledgeIndexError("knowledge file changed while it was being read")
    if len(payload) > MAX_KNOWLEDGE_FILE_BYTES:
        raise KnowledgeIndexError(
            f"knowledge file exceeds the {MAX_KNOWLEDGE_FILE_BYTES}-byte limit"
        )
    return payload


def _prepare_documents(*, files: Sequence[str], plugin_dir: Path) -> list[VectorDoc]:
    sources = _resolve_sources(files=files, plugin_dir=plugin_dir)
    documents: list[VectorDoc] = []
    read_bytes = 0
    for source in sources:
        payload = _read_source(source)
        read_bytes += len(payload)
        if read_bytes > MAX_TOTAL_KNOWLEDGE_BYTES:
            raise KnowledgeIndexError(
                f"knowledge files exceed the {MAX_TOTAL_KNOWLEDGE_BYTES}-byte total limit"
            )
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise KnowledgeIndexError("knowledge files must contain valid UTF-8") from exc

        chunks = _split_chunks(content)
        if len(documents) + len(chunks) > MAX_KNOWLEDGE_CHUNKS:
            raise KnowledgeIndexError(
                f"knowledge snapshot exceeds the {MAX_KNOWLEDGE_CHUNKS}-chunk limit"
            )
        base = _hash_id(source.label)
        documents.extend(
            VectorDoc(
                doc_id=f"kb:{base}:{index}",
                text=chunk,
                meta={
                    "type": "knowledge",
                    "source": source.label,
                    "chunk": index,
                    "knowledge_index": "configured_files",
                    "global_approved": True,
                },
            )
            for index, chunk in enumerate(chunks)
        )
    return documents


def ensure_knowledge_index(
    *,
    memory_db: MemoryDB,
    data_dir: Path,
    plugin_dir: Path,
    files: Sequence[str],
) -> bool:
    """发布完整的配置文件快照，并在内容变化时持久化。"""
    documents = _prepare_documents(files=files, plugin_dir=plugin_dir)
    memory_db.bind(data_dir)
    changed = memory_db.replace_configured_knowledge(documents)
    if changed:
        memory_db.save()
    return changed
