"""Crash-safe per-target progress for scheduled notification fanout."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .atomic_store import AtomicJsonStore

_SCHEMA_VERSION     = 1
_MAX_TARGETS        = 1000
_MAX_SEGMENTS       = 1000
_MAX_EVENT_ID_CHARS = 256


class DurableFanoutStateError(ValueError):
    """Raised when persisted fanout state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class FanoutTarget:
    kind: str
    target_id: int

    def __post_init__(self) -> None:
        if self.kind not in {"group", "private"}:
            raise DurableFanoutStateError("fanout target kind is invalid")
        if (
            isinstance(self.target_id, bool)
            or not isinstance(self.target_id, int)
            or self.target_id <= 0
        ):
            raise DurableFanoutStateError("fanout target id is invalid")

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target_id}"

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.target_id}


def default_group_targets(context: object) -> tuple[FanoutTarget, ...]:
    """规范化插件上下文中当前调用的有效群发目标。

    只接受正整数或纯 ASCII 十进制字符串；忽略无效值和重复值，并保持配置顺序。
    """

    raw_targets = list(context.default_groups()) if hasattr(context, "default_groups") else []
    target_ids: list[int] = []
    seen: set[int] = set()
    for value in raw_targets:
        if type(value) is int:
            target_id = value
        elif type(value) is str and value and value.isascii() and value.isdecimal():
            target_id = int(value)
        else:
            continue
        if target_id > 0 and target_id not in seen:
            target_ids.append(target_id)
            seen.add(target_id)
    return tuple(FanoutTarget("group", target_id) for target_id in target_ids)


@dataclass(slots=True)
class PendingFanout:
    event_id: str
    payload: list[dict[str, Any]]
    targets: tuple[FanoutTarget, ...]
    commit: dict[str, Any]
    delivered: set[str] = field(default_factory=set)

    @property
    def complete(self) -> bool:
        return all(target.key in self.delivered for target in self.targets)

    def pending_targets(self) -> tuple[FanoutTarget, ...]:
        return tuple(target for target in self.targets if target.key not in self.delivered)

    def as_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "payload": self.payload,
            "targets": [target.as_json() for target in self.targets],
            "delivered": sorted(self.delivered),
            "commit": self.commit,
        }


def _decode_pending(value: Any) -> PendingFanout:
    if not isinstance(value, dict):
        raise DurableFanoutStateError("fanout pending state must be an object")
    event_id      = value.get("event_id")
    payload       = value.get("payload")
    raw_targets   = value.get("targets")
    raw_delivered = value.get("delivered")
    commit        = value.get("commit")
    if not isinstance(event_id, str) or not event_id or len(event_id) > _MAX_EVENT_ID_CHARS:
        raise DurableFanoutStateError("fanout event id is invalid")
    if (
        not isinstance(payload, list)
        or len(payload) > _MAX_SEGMENTS
        or any(not isinstance(segment, dict) for segment in payload)
    ):
        raise DurableFanoutStateError("fanout payload is invalid")
    if not isinstance(raw_targets, list) or not 0 < len(raw_targets) <= _MAX_TARGETS:
        raise DurableFanoutStateError("fanout targets are invalid")
    targets: list[FanoutTarget] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            raise DurableFanoutStateError("fanout target must be an object")
        targets.append(
            FanoutTarget(
                str(item.get("kind", "")),
                cast(int, item.get("id")),
            )
        )
    target_keys = [target.key for target in targets]
    if len(set(target_keys)) != len(target_keys):
        raise DurableFanoutStateError("fanout targets must be unique")
    if (
        not isinstance(raw_delivered, list)
        or any(not isinstance(key, str) for key in raw_delivered)
        or not set(raw_delivered).issubset(target_keys)
    ):
        raise DurableFanoutStateError("fanout delivered set is invalid")
    if not isinstance(commit, dict):
        raise DurableFanoutStateError("fanout commit data must be an object")
    return PendingFanout(
        event_id  = event_id,
        payload   = list(payload),
        targets   = tuple(targets),
        commit    = dict(commit),
        delivered = set(raw_delivered),
    )


def load_pending(path: Path) -> PendingFanout | None:
    try:
        payload = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    root = json.loads(payload.decode("utf-8"))
    if not isinstance(root, dict) or root.get("version") != _SCHEMA_VERSION:
        raise DurableFanoutStateError("fanout root schema is invalid")
    pending = root.get("pending")
    return None if pending is None else _decode_pending(pending)


def save_pending(path: Path, pending: PendingFanout) -> None:
    validated = _decode_pending(pending.as_json())
    AtomicJsonStore(path).write({"version": _SCHEMA_VERSION, "pending": validated.as_json()})


def create_pending(
    path: Path,
    *,
    event_id: str,
    payload: list[dict[str, Any]],
    targets: tuple[FanoutTarget, ...],
    commit: dict[str, Any],
) -> PendingFanout:
    pending = PendingFanout(
        event_id = event_id,
        payload  = list(payload),
        targets  = tuple(targets),
        commit   = dict(commit),
    )
    save_pending(path, pending)
    return pending


def mark_delivered(path: Path, pending: PendingFanout, target: FanoutTarget) -> None:
    if target not in pending.targets:
        raise DurableFanoutStateError("cannot acknowledge an unknown fanout target")
    pending.delivered.add(target.key)
    save_pending(path, pending)


def clear_pending(path: Path) -> None:
    AtomicJsonStore(path).write({"version": _SCHEMA_VERSION, "pending": None})
