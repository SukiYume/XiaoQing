from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .media.emoji_library import resolve_emoji_file_path
from .media.reply_planner_common import extract_inbound_marker_labels
from .message_parts import merge_reply_media_parts


@dataclass(frozen=True)
class ReplyMediaSelection:
    emoji_plan: Any = None
    face_plan: Any = None
    media_parts: tuple[dict[str, Any], ...] = ()
    suppress_text: bool = False


def _is_emoji_only(emoji_plan) -> bool:
    return str(getattr(emoji_plan, "mode", "") or "") == "emoji_only"


def _is_face_only(face_plan) -> bool:
    return str(getattr(face_plan, "mode", "") or "") == "face_only"


def _emoji_media_part(context, emoji_plan) -> dict[str, Any] | None:
    if emoji_plan is None:
        return None
    entry = getattr(emoji_plan, "entry", None)
    if entry is None:
        return None
    raw_file_path = str(getattr(entry, "file_path", "") or "").strip()
    if not raw_file_path:
        return None
    return {
        "kind": "emoji",
        "media_hash": str(getattr(entry, "media_hash", "") or ""),
        "marker": str(getattr(emoji_plan, "marker", "") or ""),
        "description": str(getattr(entry, "description", "") or ""),
        "emotion_tags": [
            str(tag).strip()
            for tag in getattr(entry, "emotion_tags", ())
            if str(tag).strip()
        ],
        "file_path": str(resolve_emoji_file_path(context, raw_file_path)),
        "mode": str(getattr(emoji_plan, "mode", "") or ""),
    }


def _face_media_part(face_plan) -> dict[str, Any] | None:
    if face_plan is None:
        return None
    entry = getattr(face_plan, "entry", None)
    if entry is None:
        return None
    return {
        "kind": "qq_face",
        "face_id": str(getattr(entry, "face_id", "") or ""),
        "marker": str(getattr(face_plan, "marker", "") or ""),
        "label": str(getattr(entry, "label", "") or ""),
        "aliases": [
            str(alias).strip()
            for alias in getattr(entry, "aliases", ())
            if str(alias).strip()
        ],
        "mode": str(getattr(face_plan, "mode", "") or ""),
    }


def _prefer_emoji_plan(user_text: str, emoji_plan, face_plan) -> bool:
    emoji_inbound = bool(extract_inbound_marker_labels(user_text, "emoji"))
    face_inbound = bool(extract_inbound_marker_labels(user_text, "qq_face"))
    if emoji_inbound != face_inbound:
        return emoji_inbound

    emoji_only = _is_emoji_only(emoji_plan)
    face_only = _is_face_only(face_plan)
    if emoji_only != face_only:
        return emoji_only

    return True


def resolve_reply_media_selection(
    context,
    *,
    user_text: str,
    emoji_plan=None,
    face_plan=None,
) -> ReplyMediaSelection:
    emoji_part = _emoji_media_part(context, emoji_plan)
    face_part = _face_media_part(face_plan)
    if emoji_part is None:
        emoji_plan = None
    if face_part is None:
        face_plan = None

    if emoji_plan is not None and face_plan is not None:
        if _prefer_emoji_plan(user_text, emoji_plan, face_plan):
            face_plan = None
            face_part = None
        else:
            emoji_plan = None
            emoji_part = None

    media_parts = tuple(part for part in (emoji_part, face_part) if part)
    return ReplyMediaSelection(
        emoji_plan=emoji_plan,
        face_plan=face_plan,
        media_parts=media_parts,
        suppress_text=_is_emoji_only(emoji_plan) or _is_face_only(face_plan),
    )


def merge_selected_reply_media_parts(
    base_parts: Sequence[dict[str, Any]] | None,
    selection: ReplyMediaSelection,
) -> tuple[dict[str, Any], ...]:
    return merge_reply_media_parts(
        base_parts,
        selection.media_parts,
        suppress_text=selection.suppress_text,
    )
