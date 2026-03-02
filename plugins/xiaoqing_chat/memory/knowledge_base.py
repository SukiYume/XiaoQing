from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence

from .memory_db import MemoryDB

def _hash_id(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]

def _split_chunks(text: str, *, max_len: int = 800) -> list[str]:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in s.split("\n\n") if p.strip()]
    out: list[str] = []
    for p in parts:
        if len(p) <= max_len:
            out.append(p)
            continue
        start = 0
        while start < len(p):
            out.append(p[start : start + max_len].strip())
            start += max_len
    return [x for x in out if x]

def ensure_knowledge_index(
    *,
    memory_db: MemoryDB,
    data_dir: Path,
    plugin_dir: Path,
    files: Sequence[str],
) -> None:
    if not files:
        return
    memory_db.bind(data_dir)
    for f in files:
        p = Path(f)
        if not p.is_absolute():
            p = (plugin_dir / p).resolve()
        if not p.exists() or not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        chunks = _split_chunks(content)
        base = _hash_id(str(p))
        for i, c in enumerate(chunks[:200]):
            doc_id = f"kb:{base}:{i}"
            memory_db.upsert_text(
                doc_id=doc_id,
                text=c,
                meta={"type": "knowledge", "source": str(p), "chunk": i},
            )
    memory_db.save()
