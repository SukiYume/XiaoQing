from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
        self._cache: list[ExpressionRecord] | None = None

    def _path(self) -> Path | None:
        return self._resolve_path("bw_learner", "expressions.json")

    def load(self) -> list[ExpressionRecord]:
        if self._cache is not None:
            return list(self._cache)
        try:
            raw = self._load_json_from_path_parts("bw_learner", "expressions.json", default=[])
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
                        content_list=[
                            str(x).strip()
                            for x in content_list
                            if isinstance(x, str) and str(x).strip()
                        ],
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
        payload = [asdict(x) for x in items]
        if not self._save_json_to_path_parts("bw_learner", "expressions.json", data=payload):
            return
        self._cache = list(items)

    def upsert_all(self, items: Sequence[ExpressionRecord]) -> None:
        self.save(items)
