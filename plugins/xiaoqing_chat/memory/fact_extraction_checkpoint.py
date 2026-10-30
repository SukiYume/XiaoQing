"""维护人物事实提取水位，避免对同一批消息重复调用模型。"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.plugin_base import load_json, write_json

from ..store_base import delete_json_artifacts


def _checkpoint_path(data_dir: Path, chat_id: str) -> Path:
    """返回一个防目录穿越、按会话稳定映射的检查点路径。"""

    scope  = str(chat_id or "").strip()
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return Path(data_dir) / "fact_extraction" / f"{digest}.json"


def _message_timestamp(message: Any) -> float:
    try:
        value = float(message.ts or 0.0)
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


def message_identity(message: Any) -> str:
    """同一时刻的消息靠稳定标识区分：本地编号、QQ 消息 ID，最后退回内容摘要。"""

    local_id = str(getattr(message, "local_id", "") or "").strip()
    if local_id:
        return f"l:{local_id}"
    message_id = getattr(message, "message_id", None)
    if message_id is not None:
        return f"m:{message_id}"
    raw = "\0".join(
        str(getattr(message, field, "") or "") for field in ("role", "user_id", "content")
    )
    return "h:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_unobserved(message: Any, *, after_ts: float, seen_at_ts: frozenset[str]) -> bool:
    """水位之后的消息，或与水位同一时刻但还没处理过的消息。"""

    ts = _message_timestamp(message)
    if after_ts <= 0:
        return True
    return ts > after_ts or (ts == after_ts and message_identity(message) not in seen_at_ts)


def load_observed_position(data_dir: Path, chat_id: str) -> tuple[float, frozenset[str]]:
    """返回 (水位时间戳, 该时刻已处理的消息标识)。"""

    payload: Any = load_json(_checkpoint_path(data_dir, chat_id), default={})
    ids  = payload.get("observed_ids_at_ts", []) if isinstance(payload, dict) else []
    seen = frozenset(str(item) for item in ids if isinstance(item, str))
    return load_last_observed_ts(data_dir, chat_id), seen


def observed_message_count(
    history: Sequence[Any],
    *,
    after_ts: float,
    seen_at_ts: frozenset[str] = frozenset(),
) -> int:
    return sum(
        1 for message in history if is_unobserved(message, after_ts=after_ts, seen_at_ts=seen_at_ts)
    )


def latest_message_ts(history: Sequence[Any]) -> float:
    return max((_message_timestamp(message) for message in history), default=0.0)


def save_last_observed_ts(
    data_dir: Path,
    chat_id: str,
    observed_until: float,
    observed_ids_at_ts: Sequence[str] = (),
) -> None:
    if not math.isfinite(observed_until) or observed_until <= 0:
        return
    write_json(
        _checkpoint_path(data_dir, chat_id),
        {
            "version"           : 2,
            "last_observed_ts"  : float(observed_until),
            "observed_ids_at_ts": sorted(set(observed_ids_at_ts)),
        },
    )


def clear_fact_extraction_checkpoint(data_dir: Path, chat_id: str) -> None:
    delete_json_artifacts(_checkpoint_path(data_dir, chat_id))
