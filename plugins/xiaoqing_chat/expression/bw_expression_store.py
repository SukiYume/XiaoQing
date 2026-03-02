from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..store_base import StoreBase

@dataclass
class ExpressionRecord:
    expression_id: str
    chat_id: str
    situation: str
    style: str
    content_list: list[str] = field(default_factory=list)
    count: int = 1
    last_active_time: float = field(default_factory=lambda: time.time())
    checked: bool = False
    rejected: bool = False
    modified_by: str = "ai"

class ExpressionStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._cache: Optional[list[ExpressionRecord]] = None

    def _path(self) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "bw_learner" / "expressions.json"

    def load(self) -> list[ExpressionRecord]:
        if self._cache is not None:
            return list(self._cache)
        path = self._path()
        if not path or not path.exists():
            self._cache = []
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                self._cache = []
                return []
            out: list[ExpressionRecord] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                eid = str(item.get("expression_id", "") or "").strip()
                chat_id = str(item.get("chat_id", "") or "").strip()
                situation = str(item.get("situation", "") or "").strip()
                style = str(item.get("style", "") or "").strip()
                if not eid or not chat_id or not situation or not style:
                    continue
                content_list = item.get("content_list", [])
                if not isinstance(content_list, list):
                    content_list = []
                count = int(item.get("count", 1) or 1)
                last_active_time = float(item.get("last_active_time", 0.0) or 0.0)
                checked = bool(item.get("checked", False))
                rejected = bool(item.get("rejected", False))
                modified_by = str(item.get("modified_by", "ai") or "ai")
                out.append(
                    ExpressionRecord(
                        expression_id=eid,
                        chat_id=chat_id,
                        situation=situation,
                        style=style,
                        content_list=[str(x).strip() for x in content_list if isinstance(x, str) and str(x).strip()],
                        count=count,
                        last_active_time=last_active_time,
                        checked=checked,
                        rejected=rejected,
                        modified_by=modified_by,
                    )
                )
            self._cache = out
            return list(out)
        except Exception:
            self._cache = []
            return []

    def save(self, items: Sequence[ExpressionRecord]) -> None:
        path = self._path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(x) for x in items]
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return
        self._cache = list(items)

    def upsert_all(self, items: Sequence[ExpressionRecord]) -> None:
        self.save(items)
