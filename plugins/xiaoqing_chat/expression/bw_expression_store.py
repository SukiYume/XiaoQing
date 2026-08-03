from __future__ import annotations

import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.atomic_store import keyed_path_lock

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
        self._baseline: list[ExpressionRecord] = []

    def bind(self, data_dir: Path) -> None:
        if self._data_dir == data_dir:
            return
        super().bind(data_dir)
        self._cache = None
        self._baseline = []

    def _path(self) -> Path | None:
        return self._resolve_path("bw_learner", "expressions.json")

    def _read_records(self) -> list[ExpressionRecord]:
        try:
            raw = self._load_json_from_path_parts("bw_learner", "expressions.json", default=[])
            if not isinstance(raw, list):
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
            return out
        except Exception:
            return []

    def load(self) -> list[ExpressionRecord]:
        path = self._path()
        if path is None:
            return []
        # 使用 core 的规范路径锁，模块热重载前后的类实例也共享同一把锁。
        with keyed_path_lock(path):
            if self._cache is None:
                self._cache = self._read_records()
                self._baseline = deepcopy(self._cache)
            return deepcopy(self._cache)

    def clear(self, chat_id: str) -> None:
        """删除一个会话的表达学习记录，并保留其它会话的记录。"""

        target = str(chat_id or "").strip()
        path = self._path()
        if not target or path is None:
            return
        with keyed_path_lock(path):
            latest = self._read_records()
            remaining = [item for item in latest if item.chat_id != target]
            if len(remaining) != len(latest):
                payload = [asdict(item) for item in remaining]
                if not self._save_json_to_path_parts(
                    "bw_learner", "expressions.json", data=payload
                ):
                    return
            self._cache = deepcopy(remaining)
            self._baseline = deepcopy(remaining)

    @staticmethod
    def _merge_record(
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
            original = getattr(baseline, field_name) if baseline is not None else None
            desired_value = getattr(desired, field_name)
            if baseline is None or desired_value != original:
                setattr(merged, field_name, desired_value)
        return merged

    def save(self, items: Sequence[ExpressionRecord]) -> None:
        desired_items = deepcopy(list(items))
        path = self._path()
        if path is None:
            return
        with keyed_path_lock(path):
            latest_items = self._read_records()
            latest_by_id = {item.expression_id: item for item in latest_items}
            baseline_by_id = {item.expression_id: item for item in self._baseline}
            desired_by_id = {item.expression_id: item for item in desired_items}
            merged_by_id = dict(latest_by_id)
            for expression_id, desired in desired_by_id.items():
                merged_by_id[expression_id] = self._merge_record(
                    latest_by_id.get(expression_id),
                    baseline_by_id.get(expression_id),
                    desired,
                )
            for expression_id in set(baseline_by_id) - set(desired_by_id):
                merged_by_id.pop(expression_id, None)

            desired_order = [item.expression_id for item in desired_items]
            concurrent_order = [
                item.expression_id
                for item in latest_items
                if item.expression_id not in baseline_by_id
                and item.expression_id not in desired_by_id
            ]
            order = [*concurrent_order, *desired_order]
            merged_items = [merged_by_id[key] for key in order if key in merged_by_id]
            payload = [asdict(item) for item in merged_items]
            if not self._save_json_to_path_parts("bw_learner", "expressions.json", data=payload):
                return
            self._cache = deepcopy(merged_items)
            self._baseline = deepcopy(merged_items)
