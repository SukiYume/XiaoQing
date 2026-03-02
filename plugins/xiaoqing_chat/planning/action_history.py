from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

@dataclass(frozen=True)
class ActionRecord:
    ts: float
    local_target: str
    action: str
    reasoning: str
    detail: dict[str, Any]
    executed: bool

class ActionHistoryStore:
    """Stores action records per chat with debounced persistence.

    Uses a dirty-flag + debounced write strategy instead of writing on every
    append, to avoid I/O bottlenecks in high-frequency conversation scenarios.
    """

    def __init__(self) -> None:
        self._data_dir: Optional[Path] = None
        self._cache: dict[str, list[ActionRecord]] = {}
        self._dirty: set[str] = set()

    def bind(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def append(self, chat_id: str, record: ActionRecord) -> None:
        self._cache.setdefault(chat_id, []).append(record)
        self._dirty.add(chat_id)

    def flush(self, chat_id: Optional[str] = None) -> None:
        """Persist dirty chat(s) to disk. Call from debounced scheduler."""
        if chat_id is not None:
            if chat_id in self._dirty:
                self._persist(chat_id)
                self._dirty.discard(chat_id)
        else:
            for cid in list(self._dirty):
                self._persist(cid)
            self._dirty.clear()

    def flush_all(self) -> None:
        """Flush all dirty chats — used during shutdown."""
        self.flush()

    def get_recent(self, chat_id: str, *, max_items: int = 20) -> list[ActionRecord]:
        if chat_id not in self._cache:
            loaded = self._load(chat_id)
            self._cache[chat_id] = loaded or []
        if max_items <= 0:
            return []
        return list(self._cache[chat_id][-max_items:])

    def _path(self, chat_id: str) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "action_history" / f"{chat_id}.json"

    def _persist(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        items = self._cache.get(chat_id, [])
        payload = [asdict(x) for x in items[-200:]]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self, chat_id: str) -> Optional[list[ActionRecord]]:
        path = self._path(chat_id)
        if not path or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return None
            out: list[ActionRecord] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                ts_val = item.get("ts", time.time())
                try:
                    ts = float(ts_val)
                except (TypeError, ValueError):
                    ts = time.time()
                out.append(
                    ActionRecord(
                        ts=ts,
                        local_target=str(item.get("local_target", "") or ""),
                        action=str(item.get("action", "") or ""),
                        reasoning=str(item.get("reasoning", "") or ""),
                        detail=item.get("detail") if isinstance(item.get("detail"), dict) else {},
                        executed=bool(item.get("executed", False)),
                    )
                )
            return out
        except Exception:
            return None
