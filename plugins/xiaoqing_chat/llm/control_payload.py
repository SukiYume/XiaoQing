from __future__ import annotations

from typing import Any

_REASONING_PAYLOAD_KEYS = frozenset({"reasoning_effort"})


def control_extra_payload(
    extra_payload: dict[str, Any] | None,
    *,
    json_object: bool = False,
) -> dict[str, Any]:
    """Return payload overrides for short control/JSON calls.

    Main chat replies may intentionally use reasoning-heavy provider settings.
    Control calls such as reply checking and memory query planning need fast,
    final-channel JSON/text output. If the provider exposes a thinking switch,
    force it off for these calls.
    """
    source = dict(extra_payload or {})
    payload = {
        str(key): value
        for key, value in source.items()
        if str(key) not in _REASONING_PAYLOAD_KEYS
    }
    if "thinking" in source:
        payload["thinking"] = {"type": "disabled"}
    if json_object:
        payload.setdefault("response_format", {"type": "json_object"})
    return payload
