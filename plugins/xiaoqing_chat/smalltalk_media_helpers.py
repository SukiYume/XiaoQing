from __future__ import annotations

from typing import Any

from .media import render_event_media
from .media.qq_face_catalog import mark_qq_face_used_by_id
from .media.qq_face_catalog import mark_qq_face_used
from .media.emoji_library import mark_emoji_used, mark_emoji_used_by_hash
from .media_registry import upsert_registered_media_items
from .message_parts import (
    build_text_message_parts,
    message_parts_to_legacy,
    normalize_message_parts,
    replace_message_media_parts,
)
from .smalltalk_models import _GeneratedSmalltalkTurn


_MEDIA_PART_KINDS = ("emoji", "qq_face", "image")


def _first_media_part(
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    for part in normalize_message_parts(parts):
        if str(part.get("kind", "") or "").strip() in _MEDIA_PART_KINDS:
            return dict(part)
    return None


def _drop_empty(detail: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in detail.items() if value}


def _media_action_detail(
    marker,
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if marker is not None:
        entry = getattr(marker, "entry", None)
        return _drop_empty({
            "media_kind": str(getattr(marker, "kind", "") or ""),
            "media_marker": str(getattr(marker, "marker", "") or ""),
            "media_mode": str(getattr(marker, "mode", "") or ""),
            "media_hash": str(getattr(entry, "media_hash", "") or ""),
            "media_key": str(getattr(entry, "media_key", "") or ""),
            "media_face_id": str(getattr(entry, "face_id", "") or ""),
        })

    part = _first_media_part(parts)
    if not part:
        return {}
    return _drop_empty({
        "media_kind": str(part.get("kind", "") or ""),
        "media_marker": str(part.get("marker", "") or ""),
        "media_mode": str(part.get("mode", "") or ""),
        "media_hash": str(part.get("media_hash", "") or ""),
        "media_key": str(part.get("media_key", "") or ""),
        "media_face_id": str(part.get("face_id", "") or ""),
    })


def _serialize_rendered_media_items(rendered_items: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for item in rendered_items:
        marker = str(getattr(item, "marker", "") or "").strip()
        description = str(getattr(item, "description", "") or "").strip()
        media_hash = str(getattr(item, "media_hash", "") or "").strip()
        kind = str(getattr(item, "kind", "") or "").strip()
        face_id = str(getattr(item, "face_id", "") or "").strip()
        emotion_tags = [
            str(tag).strip() for tag in (getattr(item, "emotion_tags", ()) or ()) if str(tag).strip()
        ]
        cached_path = getattr(item, "cached_path", None)

        payload: dict[str, Any] = {}
        if kind:
            payload["kind"] = kind
        if media_hash:
            payload["media_hash"] = media_hash
        if face_id:
            payload["face_id"] = face_id
        if marker:
            payload["marker"] = marker
        if description:
            payload["description"] = description
            if kind == "qq_face":
                payload["label"] = description
        if emotion_tags:
            payload["emotion_tags"] = emotion_tags
        if cached_path is not None:
            payload["file_path"] = str(cached_path)
        if payload:
            serialized.append(payload)
    return serialized


async def _event_media_items_for_memory(event: dict[str, Any], *, context, runtime) -> list[dict[str, Any]]:
    cached_items = event.get("_xc_rendered_media_items")
    if isinstance(cached_items, list):
        return _serialize_rendered_media_items(cached_items)
    try:
        rendered_items = await render_event_media(event, context=context, runtime=runtime)
    except Exception:
        return []
    return _serialize_rendered_media_items(rendered_items)


def _sync_message_parts_to_registry(
    state,
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    *,
    context=None,
    runtime=None,
    schedule_media_registry_flush=None,
) -> tuple[dict[str, Any], ...]:
    normalized_parts = normalize_message_parts(parts)
    if not normalized_parts:
        return ()
    _content, media_items = message_parts_to_legacy(normalized_parts)
    media_store = getattr(state, "media_store", None)
    synced_media_items = upsert_registered_media_items(
        media_items,
        store=media_store,
        compact=False,
    ) or media_items
    if media_items and media_store is not None and callable(schedule_media_registry_flush):
        try:
            is_dirty = getattr(media_store, "is_dirty", None)
            if not callable(is_dirty) or is_dirty():
                schedule_media_registry_flush(context, runtime)
        except Exception:
            pass
    return replace_message_media_parts(
        normalized_parts,
        synced_media_items,
        store=media_store,
    )


def _prefix_reply_parts(
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    prefix_text: str,
) -> tuple[dict[str, Any], ...]:
    normalized_parts = normalize_message_parts(parts)
    prefix = str(prefix_text or "")
    if not prefix:
        return normalized_parts
    if not normalized_parts:
        return build_text_message_parts(prefix)

    merged_parts = [dict(part) for part in normalized_parts]
    if str(merged_parts[0].get("kind", "") or "").strip() == "text":
        merged_parts[0]["text"] = prefix + str(merged_parts[0].get("text", "") or "")
    else:
        merged_parts.insert(0, {"kind": "text", "text": prefix})
    return normalize_message_parts(merged_parts)


def _assistant_reply_parts(
    context,
    generated: _GeneratedSmalltalkTurn,
) -> tuple[dict[str, Any], ...]:
    return normalize_message_parts(generated.reply_parts)


def _display_reply_text(generated: _GeneratedSmalltalkTurn) -> str:
    if generated.reply_output is not None:
        return generated.reply_output.payload.display_text
    return generated.reply


def _reply_send_prefix(reply_text: str, reply_for_send: str) -> str:
    reply = str(reply_text or "")
    send_text = str(reply_for_send or "")
    if reply and send_text != reply and send_text.endswith(reply):
        return send_text[: -len(reply)]
    return ""


def _normalize_generated_reply_state(
    generated: _GeneratedSmalltalkTurn,
    *,
    reply_text: str,
    reply_parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
) -> None:
    generated.reply = str(reply_text or "").strip()
    if not generated.reply:
        generated.reply_parts = ()
        generated.reply_output = None
        return

    normalized_parts = normalize_message_parts(reply_parts)
    if not normalized_parts:
        normalized_parts = build_text_message_parts(generated.reply)

    generated.reply_parts = normalized_parts
    generated.reply_output = None


def _mark_reply_media_used(context, runtime, generated: _GeneratedSmalltalkTurn) -> None:
    marker = generated.media_marker
    marker_kind = str(getattr(marker, "kind", "") or "") if marker is not None else ""

    if marker is not None and marker_kind == "emoji":
        try:
            mark_emoji_used(context, runtime, marker.entry)
        except Exception:
            pass
    elif marker_kind != "emoji":
        for part in normalize_message_parts(generated.reply_parts):
            if str(part.get("kind", "") or "").strip() != "emoji":
                continue
            try:
                mark_emoji_used_by_hash(context, runtime, str(part.get("media_hash", "") or ""))
            except Exception:
                pass

    if marker is not None and marker_kind == "qq_face":
        try:
            mark_qq_face_used(context, marker.entry)
        except Exception:
            pass
    elif marker_kind != "qq_face":
        for part in normalize_message_parts(generated.reply_parts):
            if str(part.get("kind", "") or "").strip() != "qq_face":
                continue
            try:
                mark_qq_face_used_by_id(
                    context,
                    str(part.get("face_id", "") or ""),
                    label=str(part.get("label", "") or ""),
                )
            except Exception:
                pass
