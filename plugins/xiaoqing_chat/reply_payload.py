"""把规范化消息部件编排为展示文本和可投递批次。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.plugin_base import emoji, face, image, segments

from .media_registry import MEDIA_PLACEHOLDER_RE, rebuild_message_content
from .message_parts import (
    message_parts_to_legacy,
    normalize_message_parts,
    template_with_fallback_placeholders,
)
from .reply_splitter import _split_chat_reply


@dataclass(frozen=True)
class ReplyPayload:
    display_text: str
    outbound_batches: list[list[dict[str, Any]]]
    media_items: list[dict[str, Any]]
    parts: tuple[dict[str, Any], ...] = ()


def _media_segment(spec: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(spec.get("kind", "") or "").strip()
    if kind == "emoji":
        file_path = str(spec.get("file_path", "") or "").strip()
        if file_path:
            summary = str(spec.get("description", "") or spec.get("marker", "") or "").strip()
            return emoji(file_path, summary=summary)
        return None
    if kind == "image":
        file_path = str(spec.get("file_path", "") or "").strip()
        if file_path:
            return image(file_path)
        return None
    if kind == "qq_face":
        face_id = str(spec.get("face_id", "") or "").strip()
        if face_id:
            return face(face_id)
        return None
    return None


def _build_batch_segments(
    batch_text: str, media_sequence: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[int]]:
    batch = str(batch_text or "")
    if not batch:
        return [], []

    built: list[dict[str, Any]] = []
    consumed_indexes: list[int] = []
    cursor                      = 0
    for match in MEDIA_PLACEHOLDER_RE.finditer(batch):
        text_prefix = batch[cursor : match.start()].strip("\r\n")
        if text_prefix.strip():
            built.extend(segments(text_prefix))
        media_index = max(0, int(match.group(1)) - 1)
        if media_index < len(media_sequence):
            segment = _media_segment(media_sequence[media_index])
            if segment is not None:
                built.append(segment)
                consumed_indexes.append(media_index)
        cursor = match.end()
    suffix = batch[cursor:].lstrip("\r\n")
    if suffix.strip():
        built.extend(segments(suffix))
    return built, consumed_indexes


def _media_sequence_from_parts(parts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [dict(part) for part in parts if str(part.get("kind", "") or "").strip() != "text"]


def _build_reply_payload_core(
    *,
    reply_template: str,
    media_sequence: list[dict[str, Any]],
    display_template: str,
    payload_parts: tuple[dict[str, Any], ...],
) -> ReplyPayload:
    template = template_with_fallback_placeholders(reply_template, media_sequence)
    parts = _split_chat_reply(template) if template else []
    outbound_batches: list[list[dict[str, Any]]] = []
    consumed_indexes: set[int] = set()
    for part in parts:
        if not part or not part.strip():
            continue
        batch_segments, batch_indexes = _build_batch_segments(part, media_sequence)
        if batch_segments:
            outbound_batches.append(batch_segments)
        consumed_indexes.update(batch_indexes)

    trailing_segments: list[dict[str, Any]] = []
    for index, item in enumerate(media_sequence):
        if index in consumed_indexes:
            continue
        segment = _media_segment(item)
        if segment is not None:
            trailing_segments.append(segment)
    if trailing_segments:
        if outbound_batches:
            outbound_batches[-1] = [*outbound_batches[-1], *trailing_segments]
        else:
            outbound_batches.append(trailing_segments)

    visible_template = template_with_fallback_placeholders(display_template, media_sequence)
    visible_text     = rebuild_message_content(
        visible_template,
        media_sequence,
        resolved_items=media_sequence,
    )

    return ReplyPayload(
        display_text     = visible_text,
        outbound_batches = outbound_batches,
        media_items      = media_sequence,
        parts            = payload_parts,
    )


def build_reply_payload_from_parts(
    reply_parts,
    *,
    display_parts=None,
) -> ReplyPayload:
    normalized_reply_parts   = normalize_message_parts(reply_parts)
    normalized_display_parts = (
        normalize_message_parts(display_parts)
        if display_parts is not None
        else normalized_reply_parts
    )
    reply_template, _legacy_media_items = message_parts_to_legacy(normalized_reply_parts)
    display_template, _display_media_items = message_parts_to_legacy(normalized_display_parts)
    return _build_reply_payload_core(
        reply_template   = reply_template,
        media_sequence   = _media_sequence_from_parts(normalized_reply_parts),
        display_template = display_template,
        payload_parts    = normalized_display_parts,
    )
