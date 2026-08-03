from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.plugin_base import load_json, write_json

from ..store_base import delete_json_artifacts


def _checkpoint_path(data_dir: Path, chat_id: str) -> Path:
    """Return a traversal-safe, stable checkpoint path for one chat."""

    scope = str(chat_id or "").strip()
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return Path(data_dir) / "fact_extraction" / f"{digest}.json"


def _message_timestamp(message: Any) -> float:
    try:
        value = float(getattr(message, "ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def load_last_observed_ts(data_dir: Path, chat_id: str) -> float:
    payload: Any = load_json(_checkpoint_path(data_dir, chat_id), default={})
    if not isinstance(payload, dict):
        return 0.0
    try:
        value = float(payload.get("last_observed_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def observed_message_count(history: Sequence[Any], *, after_ts: float) -> int:
    return sum(1 for message in history if _message_timestamp(message) > after_ts)


def latest_message_ts(history: Sequence[Any]) -> float:
    return max((_message_timestamp(message) for message in history), default=0.0)


def save_last_observed_ts(data_dir: Path, chat_id: str, observed_until: float) -> None:
    if not math.isfinite(observed_until) or observed_until <= 0:
        return
    write_json(
        _checkpoint_path(data_dir, chat_id),
        {"version": 1, "last_observed_ts": float(observed_until)},
    )


def clear_fact_extraction_checkpoint(data_dir: Path, chat_id: str) -> None:
    delete_json_artifacts(_checkpoint_path(data_dir, chat_id))
