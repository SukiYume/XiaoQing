from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.plugin_base import image, segments

from .reply_splitter import _split_chat_reply


@dataclass(frozen=True)
class ReplyPayload:
    display_text: str
    outbound_batches: list[list[dict[str, Any]]]


def build_reply_payload(
    reply_text: str,
    *,
    emoji_file_path: str = "",
    emoji_marker: str = "",
    display_text: str | None = None,
) -> ReplyPayload:
    text = (reply_text or "").strip()
    parts = _split_chat_reply(text) if text else []
    outbound_batches = [segments(part) for part in parts if part and part.strip()]

    visible_text = (display_text if display_text is not None else text).strip()
    marker = (emoji_marker or "").strip()
    if emoji_file_path:
        outbound_batches.append([image(emoji_file_path)])
        if marker and marker != visible_text and not visible_text.endswith(f"\n{marker}"):
            visible_text = f"{visible_text}\n{marker}".strip() if visible_text else marker

    return ReplyPayload(display_text=visible_text, outbound_batches=outbound_batches)
