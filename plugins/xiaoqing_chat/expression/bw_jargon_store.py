from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..store_base import StoreBase

@dataclass
class JargonRecord:
    content: str
    meaning: str = ""
    raw_content: list[str] = field(default_factory=list)
    chat_id_counts: list[list[Any]] = field(default_factory=list)
    is_global: bool = False
    count: int = 0
    is_jargon: bool = True
    is_complete: bool = False
    last_inference_count: int = 0
    updated_at: float = field(default_factory=lambda: time.time())

class JargonStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._cache: Optional[dict[str, JargonRecord]] = None

    def _path(self) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "bw_learner" / "jargon.json"

    def load(self) -> dict[str, JargonRecord]:
        if self._cache is not None:
            return dict(self._cache)
        path = self._path()
        if not path or not path.exists():
            self._cache = {}
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                self._cache = {}
                return {}
            out: dict[str, JargonRecord] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content", "") or "").strip()
                if not content:
                    continue
                rec = JargonRecord(
                    content=content,
                    meaning=str(item.get("meaning", "") or "").strip(),
                    raw_content=[str(x).strip() for x in (item.get("raw_content", []) or []) if isinstance(x, str) and str(x).strip()],
                    chat_id_counts=item.get("chat_id_counts") if isinstance(item.get("chat_id_counts"), list) else [],
                    is_global=bool(item.get("is_global", False)),
                    count=int(item.get("count", 0) or 0),
                    is_jargon=bool(item.get("is_jargon", True)),
                    is_complete=bool(item.get("is_complete", False)),
                    last_inference_count=int(item.get("last_inference_count", 0) or 0),
                    updated_at=float(item.get("updated_at", time.time()) or time.time()),
                )
                out[content] = rec
            self._cache = out
            return dict(out)
        except Exception:
            self._cache = {}
            return {}

    def save(self, items: Sequence[JargonRecord]) -> None:
        path = self._path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(x) for x in items]
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return
        self._cache = {x.content: x for x in items}
