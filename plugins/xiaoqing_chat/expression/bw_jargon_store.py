"""按会话持久化黑话记录，并合并并发增量更新。

没有 ``scope_chat_id`` 的旧记录对任何会话都不可见；读写时原样带着，不删也不改。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..store_base import coerce_finite_float, coerce_int, coerce_json_bool
from .baseline_store import BaselineMergeStore


def _string_list(value: Any) -> list[str]:
    """只接收真正的 JSON 数组，避免字符串被拆成单字符列表。"""

    if not isinstance(value, list):
        return []
    return [text for item in value if isinstance(item, str) and (text := item.strip())]


# 一个词条最多记多少个会话的出现次数（按次数从多到少留）。
MAX_COUNTED_CHATS = 30


def _chat_count_list(value: Any) -> list[list[Any]]:
    """规范化 ``[chat_id, count]`` 对，忽略空会话和畸形成员。"""

    if not isinstance(value, list):
        return []
    normalized: list[list[Any]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        chat_id = str(item[0] or "").strip()
        if not chat_id:
            continue
        normalized.append([chat_id, coerce_int(item[1], default=0, minimum=0)])
    return normalized


@dataclass
class JargonRecord:
    """一个黑话条目及其按会话累计的观察统计。"""

    content       : str
    scope_chat_id : str = ""
    meaning       : str = ""
    raw_content   : list[str] = field(default_factory=list)
    chat_id_counts: list[list[Any]] = field(default_factory=list)
    count         : int = 0
    # 释义有依据（原句解释过，或多处上下文推断出）；否则只是候选，不当作本群共识。
    confirmed           : bool = False
    last_inference_count: int  = 0
    updated_at          : float = field(default_factory=lambda: time.time())
    usage               : str = ""
    tone                : str = ""
    source              : str = ""

    def visible_to(self, chat_id: str) -> bool:
        """这条词条在这个会话里可用：只有按这个会话记下的词条。"""

        return self.scope_chat_id == chat_id


class JargonSnapshot(dict[str, JargonRecord]):
    """黑话集合与其原始版本一起传递，保持异步推断前后的增量语义。"""

    def __init__(self, records: dict[str, JargonRecord]) -> None:
        super().__init__(deepcopy(records))
        self.baseline = deepcopy(records)


class JargonStore(BaselineMergeStore[JargonRecord]):
    """在原子 JSON 文件上维护可并发合并的黑话快照。"""

    _FILE = "jargon.json"

    @staticmethod
    def key_for(content: str, chat_id: str = "") -> str:
        term  = str(content or "").strip()
        scope = str(chat_id or "").strip()
        return f"{scope}\x1f{term}" if scope else term

    @staticmethod
    def _key(record: JargonRecord) -> str:
        return JargonStore.key_for(record.content, record.scope_chat_id)

    @staticmethod
    def _parse(item: dict[str, Any]) -> JargonRecord | None:
        content = str(item.get("content", "") or "").strip()
        if not content:
            return None
        return JargonRecord(
            content        = content,
            scope_chat_id  = str(item.get("scope_chat_id", "") or "").strip(),
            meaning        = str(item.get("meaning", "") or "").strip(),
            usage          = str(item.get("usage", "") or "").strip(),
            tone           = str(item.get("tone", "") or "").strip(),
            source         = str(item.get("source", "") or "").strip(),
            raw_content    = _string_list(item.get("raw_content")),
            chat_id_counts = _chat_count_list(item.get("chat_id_counts")),
            count=coerce_int(item.get("count"), default=0, minimum=0),
            confirmed=coerce_json_bool(item.get("confirmed"), default=False),
            last_inference_count=coerce_int(item.get("last_inference_count"), default=0, minimum=0),
            updated_at=coerce_finite_float(
                item.get("updated_at"),
                default = time.time(),
                minimum = 0.0,
            ),
        )

    def load(self) -> dict[str, JargonRecord]:
        return JargonSnapshot(self._load_records())

    def clear(self, chat_id: str) -> None:
        """删除一个会话的黑话记录，保留其它会话和没有会话的旧记录。"""

        target = str(chat_id or "").strip()
        if not target:
            return

        def keep(latest: dict[str, JargonRecord]) -> dict[str, JargonRecord]:
            return {key: record for key, record in latest.items() if record.scope_chat_id != target}

        self._rewrite(keep)

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

    @staticmethod
    def _count_pairs(counts: dict[str, int]) -> list[list[Any]]:
        """``[chat_id, count]`` 对：次数从多到少、同次数按会话号排，只留 ``MAX_COUNTED_CHATS`` 个。"""

        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [[chat_id, count] for chat_id, count in ranked if count > 0][:MAX_COUNTED_CHATS]

    @classmethod
    def bump_chat_count(cls, chat_counts: Sequence[Sequence[Any]], chat_id: str) -> list[list[Any]]:
        """这个会话的出现次数加一，返回新的 ``[chat_id, count]`` 对。"""

        counts          = cls._count_map(chat_counts)
        counts[chat_id] = counts.get(chat_id, 0) + 1
        return cls._count_pairs(counts)

    @classmethod
    def _merge_record(
        cls,
        latest: JargonRecord | None,
        baseline: JargonRecord | None,
        desired: JargonRecord,
    ) -> JargonRecord:
        if latest is None:
            return deepcopy(desired)

        merged         = deepcopy(latest)
        baseline_count = baseline.count if baseline is not None else 0
        merged.count   = max(0, latest.count + (desired.count - baseline_count))

        baseline_raw       = baseline.raw_content if baseline is not None else []
        removed_raw        = set(baseline_raw) - set(desired.raw_content)
        additions          = [item for item in desired.raw_content if item not in baseline_raw]
        merged.raw_content = [item for item in latest.raw_content if item not in removed_raw]
        merged.raw_content.extend(item for item in additions if item not in merged.raw_content)
        merged.raw_content = merged.raw_content[-20:]

        latest_counts   = cls._count_map(latest.chat_id_counts)
        baseline_counts = cls._count_map(baseline.chat_id_counts if baseline is not None else [])
        desired_counts  = cls._count_map(desired.chat_id_counts)
        for chat_id, desired_count in desired_counts.items():
            if chat_id in baseline_counts and chat_id not in latest_counts:
                continue
            delta                  = desired_count - baseline_counts.get(chat_id, 0)
            latest_counts[chat_id] = max(0, latest_counts.get(chat_id, 0) + delta)
        merged.chat_id_counts = cls._count_pairs(latest_counts)

        baseline_meaning = baseline.meaning if baseline is not None else ""
        if desired.meaning != baseline_meaning and latest.meaning == baseline_meaning:
            merged.meaning = desired.meaning
        for field_name in (
            "content",
            "scope_chat_id",
            "usage",
            "tone",
            "source",
        ):
            baseline_value = getattr(baseline, field_name) if baseline is not None else None
            desired_value  = getattr(desired, field_name)
            latest_value   = getattr(latest, field_name)
            if desired_value != baseline_value and latest_value == baseline_value:
                setattr(merged, field_name, desired_value)
        merged.confirmed            = latest.confirmed or desired.confirmed
        merged.last_inference_count = max(
            latest.last_inference_count,
            desired.last_inference_count,
        )
        merged.updated_at = max(latest.updated_at, desired.updated_at)
        return merged

    def save(self, items: Sequence[JargonRecord] | dict[str, JargonRecord]) -> None:
        baseline      = items.baseline if isinstance(items, JargonSnapshot) else self._baseline
        desired_items = deepcopy(list(items.values()) if isinstance(items, dict) else list(items))
        desired       = {self._key(record): record for record in desired_items}
        if self._merge_and_save(desired, baseline) and isinstance(items, JargonSnapshot):
            items.baseline = deepcopy(desired)
