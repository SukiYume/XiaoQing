"""主动开口的节奏记录：每个会话（群，或白名单里的私聊朋友）上次开口的时间、当天次数、上次尝试的
时间和最近几次说了什么。

开口时间、次数和说的话只在投递确认后写入；尝试时间在调用模型前写入，没发出去也算。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from ..store_base import StoreBase, coerce_finite_float, coerce_int

_FILE               = ("life", "proactive_state.json")
MAX_SHARED_LIFE_IDS = 20
MAX_RECENT_LINES    = 5
RECENT_LINE_CHARS   = 200


@dataclass(frozen=True)
class ProactiveRecord:
    last_ts: float = 0.0
    day    : str   = ""
    count  : int   = 0
    # 上次调用模型起草的时间，不论最后发没发。
    last_attempt_ts: float = 0.0
    # 已经分享过的网上经历 ID（最近 20 个）；下次作为 exclude_ids 交给 xiaoqing_life。
    shared_life_ids: tuple[str, ...] = ()
    # 最近几次主动说的话（时间, 原话）：下次开口前给她看，连同之后有没有人接。
    recent: tuple[tuple[float, str], ...] = ()

    def count_on(self, day: str) -> int:
        return self.count if self.day == day else 0


def _recent(raw: object) -> tuple[tuple[float, str], ...]:
    rows: list[tuple[float, str]] = []
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], str):
            ts = coerce_finite_float(item[0], default=0.0, minimum=0.0)
            if ts > 0 and item[1]:
                rows.append((ts, item[1][:RECENT_LINE_CHARS]))
    return tuple(rows[-MAX_RECENT_LINES:])


class ProactiveStateStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._lock                                       = threading.Lock()
        self._records: dict[str, ProactiveRecord] | None = None

    def _drop_cache(self) -> None:
        with self._lock:
            self._records = None

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
                        shared_life_ids=tuple(
                            str(v)
                            for v in (item.get("shared_life_ids") or [])
                            if isinstance(v, str)
                        )[-MAX_SHARED_LIFE_IDS:],
                        recent=_recent(item.get("recent")),
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

    def record(
        self,
        chat_id: str,
        *,
        ts: float,
        day: str,
        shared_life_ids: Sequence[str] = (),
        text: str                      = "",
    ) -> None:
        with self._lock:
            records = self._load_locked()
            current = records.get(chat_id, ProactiveRecord())
            shared  = tuple(dict.fromkeys([*current.shared_life_ids, *shared_life_ids]))[
                -MAX_SHARED_LIFE_IDS:
            ]
            records[chat_id] = replace(
                current,
                last_ts         = ts,
                day             = day,
                count           = current.count_on(day) + 1,
                shared_life_ids = shared,
                recent          = (
                    (*current.recent, (ts, text[:RECENT_LINE_CHARS]))[-MAX_RECENT_LINES:]
                    if text
                    else current.recent
                ),
            )
            self._save_locked()

    def _save_locked(self) -> None:
        self._save_json_to_path_parts(
            *_FILE, data={key: asdict(value) for key, value in (self._records or {}).items()}
        )
