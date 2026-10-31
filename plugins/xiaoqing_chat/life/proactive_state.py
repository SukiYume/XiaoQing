"""主动开口的节奏记录：每个群上次开口的时间、当天次数和上次尝试的时间。

开口时间和次数只在投递确认后写入；尝试时间在调用模型前写入，没发出去也算。
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace

from ..store_base import StoreBase, coerce_finite_float, coerce_int

_FILE = ("life", "proactive_state.json")


@dataclass(frozen=True)
class ProactiveRecord:
    last_ts: float = 0.0
    day    : str   = ""
    count  : int   = 0
    # 上次调用模型起草的时间，不论最后发没发。
    last_attempt_ts: float = 0.0

    def count_on(self, day: str) -> int:
        return self.count if self.day == day else 0


class ProactiveStateStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._lock                                       = threading.Lock()
        self._records: dict[str, ProactiveRecord] | None = None

    def bind(self, data_dir) -> None:
        if data_dir != self._data_dir:
            with self._lock:
                self._records = None
        super().bind(data_dir)

    def _load_locked(self) -> dict[str, ProactiveRecord]:
        if self._records is None:
            raw = self._load_json_from_path_parts(*_FILE, default={})
            records: dict[str, ProactiveRecord] = {}
            for chat_id, item in (raw if isinstance(raw, dict) else {}).items():
                if isinstance(item, dict):
                    records[str(chat_id)] = ProactiveRecord(
                        last_ts=coerce_finite_float(item.get("last_ts"), default=0.0, minimum=0.0),
                        day=str(item.get("day") or ""),
                        count=coerce_int(item.get("count"), default=0, minimum=0),
                        last_attempt_ts=coerce_finite_float(
                            item.get("last_attempt_ts"), default=0.0, minimum=0.0
                        ),
                    )
            self._records = records
        return self._records

    def get(self, chat_id: str) -> ProactiveRecord:
        with self._lock:
            return self._load_locked().get(chat_id, ProactiveRecord())

    def record_attempt(self, chat_id: str, *, ts: float) -> None:
        with self._lock:
            records = self._load_locked()
            current = records.get(chat_id, ProactiveRecord())
            records[chat_id] = replace(current, last_attempt_ts=ts)
            self._save_locked()

    def record(self, chat_id: str, *, ts: float, day: str) -> None:
        with self._lock:
            records          = self._load_locked()
            current          = records.get(chat_id, ProactiveRecord())
            records[chat_id] = replace(
                current, last_ts=ts, day=day, count=current.count_on(day) + 1
            )
            self._save_locked()

    def _save_locked(self) -> None:
        self._save_json_to_path_parts(
            *_FILE, data={key: asdict(value) for key, value in (self._records or {}).items()}
        )
