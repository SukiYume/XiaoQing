"""原子持久化已学习表达，并合并多实例的增量更新。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..store_base import coerce_finite_float, coerce_int, coerce_json_bool
from .baseline_store import BaselineMergeStore


@dataclass
class ExpressionRecord:
    expression_id   : str
    chat_id         : str
    situation       : str
    style           : str
    content_list    : list[str] = field(default_factory=list)
    count           : int = 1
    last_active_time: float = field(default_factory=lambda: time.time())
    checked         : bool = False
    rejected        : bool = False
    modified_by     : str  = "ai"


class ExpressionSnapshot(list[ExpressionRecord]):
    """随数据一起跨任务或线程传递基线，保存时只提交这次快照的增量。"""

    def __init__(self, records: Sequence[ExpressionRecord]) -> None:
        super().__init__(deepcopy(list(records)))
        self.baseline = deepcopy(list(records))


class ExpressionStore(BaselineMergeStore[ExpressionRecord]):
    _FILE = "expressions.json"

    @staticmethod
    def _key(record: ExpressionRecord) -> str:
        return record.expression_id

    @staticmethod
    def _parse(item: dict[str, Any]) -> ExpressionRecord | None:
        eid       = str(item.get("expression_id", "") or "").strip()
        chat_id   = str(item.get("chat_id", "") or "").strip()
        situation = str(item.get("situation", "") or "").strip()
        style     = str(item.get("style", "") or "").strip()
        if not eid or not chat_id or not situation or not style:
            return None
        content_list = item.get("content_list", [])
        if not isinstance(content_list, list):
            content_list = []
        return ExpressionRecord(
            expression_id = eid,
            chat_id       = chat_id,
            situation     = situation,
            style         = style,
            content_list  = [
                str(value).strip()
                for value in content_list
                if isinstance(value, str) and value.strip()
            ],
            count=coerce_int(item.get("count"), default=1, minimum=0),
            last_active_time=coerce_finite_float(
                item.get("last_active_time"), default=0.0, minimum=0.0
            ),
            checked=coerce_json_bool(item.get("checked"), default=False),
            rejected=coerce_json_bool(item.get("rejected"), default=False),
            modified_by=str(item.get("modified_by", "ai") or "ai"),
        )

    def _read_records(self) -> dict[str, ExpressionRecord]:
        try:
            return super()._read_records()
        except OSError:
            return {}

    def load(self) -> list[ExpressionRecord]:
        return ExpressionSnapshot(list(self._load_records().values()))

    def clear(self, chat_id: str) -> None:
        """删除一个会话的表达学习记录，并保留其它会话的记录。"""

        target = str(chat_id or "").strip()
        if not target:
            return
        self._rewrite(
            lambda latest: {
                key: record for key, record in latest.items() if record.chat_id != target
            }
        )

    @classmethod
    def _merge_record(
        cls,
        latest: ExpressionRecord | None,
        baseline: ExpressionRecord | None,
        desired: ExpressionRecord,
    ) -> ExpressionRecord:
        if latest is None:
            return deepcopy(desired)
        merged = deepcopy(latest)
        original_count = baseline.count if baseline is not None else 0
        merged.count = max(0, latest.count + (desired.count - original_count))
        original_content = baseline.content_list if baseline is not None else []
        removed = set(original_content) - set(desired.content_list)
        additions = [item for item in desired.content_list if item not in original_content]
        merged.content_list = [item for item in latest.content_list if item not in removed]
        merged.content_list.extend(item for item in additions if item not in merged.content_list)
        preserve_rejection = (
            latest.rejected and not desired.rejected and desired.modified_by != "user"
        )
        for field_name in (
            "chat_id",
            "situation",
            "style",
            "last_active_time",
            "checked",
            "rejected",
            "modified_by",
        ):
            if preserve_rejection and field_name in {"checked", "rejected", "modified_by"}:
                continue
            original      = getattr(baseline, field_name) if baseline is not None else None
            desired_value = getattr(desired, field_name)
            if baseline is None or desired_value != original:
                setattr(merged, field_name, desired_value)
        return merged

    def save(self, items: Sequence[ExpressionRecord]) -> None:
        desired_items = deepcopy(list(items))
        baseline      = (
            {record.expression_id: record for record in items.baseline}
            if isinstance(items, ExpressionSnapshot)
            else self._baseline
        )
        desired = {record.expression_id: record for record in desired_items}
        if self._merge_and_save(desired, baseline) and isinstance(items, ExpressionSnapshot):
            items.baseline = deepcopy(desired_items)
