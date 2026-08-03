from __future__ import annotations

import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from core.atomic_store import keyed_path_lock

from ..store_base import StoreBase


@dataclass
class JargonRecord:
    content: str
    scope_chat_id: str = ""
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
        self._cache: dict[str, JargonRecord] | None = None
        self._baseline: dict[str, JargonRecord] = {}

    def bind(self, data_dir: Path) -> None:
        if self._data_dir == data_dir:
            return
        super().bind(data_dir)
        self._cache = None
        self._baseline = {}

    def _path(self) -> Path | None:
        return cast(Path | None, self._resolve_path("bw_learner", "jargon.json"))

    @staticmethod
    def key_for(content: str, chat_id: str = "") -> str:
        term = str(content or "").strip()
        scope = str(chat_id or "").strip()
        return f"{scope}\x1f{term}" if scope else term

    def _read_records(self) -> dict[str, JargonRecord]:
        try:
            raw = self._load_json_from_path_parts("bw_learner", "jargon.json", default=[])
            if not isinstance(raw, list):
                return {}
            out: dict[str, JargonRecord] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content", "") or "").strip()
                if not content:
                    continue
                chat_counts_raw = item.get("chat_id_counts")
                chat_id_counts = cast(
                    list[list[Any]],
                    chat_counts_raw if isinstance(chat_counts_raw, list) else [],
                )
                rec = JargonRecord(
                    content=content,
                    scope_chat_id=str(item.get("scope_chat_id", "") or "").strip(),
                    meaning=str(item.get("meaning", "") or "").strip(),
                    raw_content=[
                        str(x).strip()
                        for x in (item.get("raw_content", []) or [])
                        if isinstance(x, str) and str(x).strip()
                    ],
                    chat_id_counts=chat_id_counts,
                    is_global=bool(item.get("is_global", False)),
                    count=int(item.get("count", 0) or 0),
                    is_jargon=bool(item.get("is_jargon", True)),
                    is_complete=bool(item.get("is_complete", False)),
                    last_inference_count=int(item.get("last_inference_count", 0) or 0),
                    updated_at=float(item.get("updated_at", time.time()) or time.time()),
                )
                out[self.key_for(content, "" if rec.is_global else rec.scope_chat_id)] = rec
            return out
        except Exception:
            return {}

    def load(self) -> dict[str, JargonRecord]:
        path = self._path()
        if path is None:
            return {}
        with keyed_path_lock(path):
            if self._cache is None:
                self._cache = self._read_records()
                self._baseline = deepcopy(self._cache)
            return deepcopy(self._cache)

    def clear(self, chat_id: str) -> None:
        """删除一个会话的黑话记录及其全局统计，保留全局定义与其它会话。"""

        target = str(chat_id or "").strip()
        path = self._path()
        if not target or path is None:
            return
        with keyed_path_lock(path):
            latest = self._read_records()
            remaining: dict[str, JargonRecord] = {}
            for record in latest.values():
                if not record.is_global and record.scope_chat_id == target:
                    continue
                kept = deepcopy(record)
                if kept.is_global:
                    kept.chat_id_counts = [
                        item
                        for item in kept.chat_id_counts
                        if not item or str(item[0] or "").strip() != target
                    ]
                remaining[self.key_for(kept.content, "" if kept.is_global else kept.scope_chat_id)] = kept
            if remaining != latest:
                payload = [asdict(item) for item in remaining.values()]
                if not self._save_json_to_path_parts(
                    "bw_learner", "jargon.json", data=payload
                ):
                    return
            self._cache = deepcopy(remaining)
            self._baseline = deepcopy(remaining)

    @staticmethod
    def _count_map(values: Sequence[Sequence[Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in values:
            if len(item) < 2:
                continue
            chat_id = str(item[0] or "").strip()
            if not chat_id:
                continue
            try:
                count = int(item[1] or 0)
            except (TypeError, ValueError):
                continue
            counts[chat_id] = max(0, count)
        return counts

    @classmethod
    def _merge_record(
        cls,
        latest: JargonRecord | None,
        baseline: JargonRecord | None,
        desired: JargonRecord,
    ) -> JargonRecord:
        if latest is None:
            return deepcopy(desired)

        merged = deepcopy(latest)
        baseline_count = baseline.count if baseline is not None else 0
        merged.count = max(0, latest.count + (desired.count - baseline_count))

        baseline_raw = baseline.raw_content if baseline is not None else []
        removed_raw = set(baseline_raw) - set(desired.raw_content)
        additions = [item for item in desired.raw_content if item not in baseline_raw]
        merged.raw_content = [item for item in latest.raw_content if item not in removed_raw]
        merged.raw_content.extend(item for item in additions if item not in merged.raw_content)
        merged.raw_content = merged.raw_content[-20:]

        latest_counts = cls._count_map(latest.chat_id_counts)
        baseline_counts = cls._count_map(baseline.chat_id_counts if baseline is not None else [])
        desired_counts = cls._count_map(desired.chat_id_counts)
        for chat_id, desired_count in desired_counts.items():
            delta = desired_count - baseline_counts.get(chat_id, 0)
            latest_counts[chat_id] = max(0, latest_counts.get(chat_id, 0) + delta)
        merged.chat_id_counts = [
            [chat_id, count]
            for chat_id, count in sorted(
                latest_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if count > 0
        ][:30]

        baseline_meaning = baseline.meaning if baseline is not None else ""
        if desired.meaning != baseline_meaning and latest.meaning == baseline_meaning:
            merged.meaning = desired.meaning
        for field_name in ("content", "scope_chat_id", "is_global", "is_jargon"):
            baseline_value = getattr(baseline, field_name) if baseline is not None else None
            desired_value = getattr(desired, field_name)
            latest_value = getattr(latest, field_name)
            if desired_value != baseline_value and latest_value == baseline_value:
                setattr(merged, field_name, desired_value)
        merged.is_complete = latest.is_complete or desired.is_complete
        merged.last_inference_count = max(
            latest.last_inference_count,
            desired.last_inference_count,
        )
        merged.updated_at = max(latest.updated_at, desired.updated_at)
        return merged

    def save(self, items: Sequence[JargonRecord]) -> None:
        desired_items = deepcopy(list(items))
        path = self._path()
        if path is None:
            return
        with keyed_path_lock(path):
            latest = self._read_records()
            desired = {
                self.key_for(item.content, "" if item.is_global else item.scope_chat_id): item
                for item in desired_items
            }
            merged = dict(latest)
            for key, desired_record in desired.items():
                merged[key] = self._merge_record(
                    latest.get(key),
                    self._baseline.get(key),
                    desired_record,
                )
            for key in set(self._baseline) - set(desired):
                merged.pop(key, None)

            concurrent_keys = [
                key for key in latest if key not in self._baseline and key not in desired
            ]
            desired_keys = list(desired)
            order = [*concurrent_keys, *desired_keys]
            merged_items = [merged[key] for key in order if key in merged]
            payload = [asdict(item) for item in merged_items]
            if not self._save_json_to_path_parts("bw_learner", "jargon.json", data=payload):
                return
            self._cache = deepcopy(merged)
            self._baseline = deepcopy(merged)
