from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .media.emoji_library import resolve_emoji_file_path
from .media.reply_planner_common import extract_inbound_marker_labels
from .message_parts import merge_reply_media_parts


@dataclass(frozen=True)
class ReplyMediaSelection:
    image_plan: Any = None
    emoji_plan: Any = None
    face_plan: Any = None
    media_parts: tuple[dict[str, Any], ...] = ()
    suppress_text: bool = False


def _is_image_only(image_plan) -> bool:
    return str(getattr(image_plan, "mode", "") or "") == "image_only"


def _is_emoji_only(emoji_plan) -> bool:
    return str(getattr(emoji_plan, "mode", "") or "") == "emoji_only"


def _is_face_only(face_plan) -> bool:
    return str(getattr(face_plan, "mode", "") or "") == "face_only"


_GENERIC_IMAGE_LABELS = frozenset({"图片", "一张图片"})
_GENERIC_EMOJI_LABELS = frozenset({"表情包", "一张表情包", "动画表情", "聊天表情包"})


def _normalize_label(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    for prefix in ("图片：", "表情包：", "QQ表情："):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text


def _specificity_score(kind: str, plan) -> int:
    entry = getattr(plan, "entry", None)
    marker = _normalize_label(getattr(plan, "marker", "") or getattr(entry, "marker", "") or "")

    if kind == "image":
        description = _normalize_label(getattr(entry, "description", "") or "")
        score = 0
        if description and description not in _GENERIC_IMAGE_LABELS:
            score += 6
        if marker and marker not in _GENERIC_IMAGE_LABELS:
            score += 2
        return score

    if kind == "emoji":
        description = _normalize_label(getattr(entry, "description", "") or "")
        tags = [
            str(tag).strip()
            for tag in getattr(entry, "emotion_tags", ())
            if str(tag).strip()
        ]
        score = min(len(tags), 4) * 2
        if description and description not in _GENERIC_EMOJI_LABELS:
            score += 3
        if marker and marker not in _GENERIC_EMOJI_LABELS:
            score += 1
        return score

    label = _normalize_label(getattr(entry, "label", "") or "")
    aliases = [
        str(alias).strip()
        for alias in getattr(entry, "aliases", ())
        if str(alias).strip()
    ]
    score = min(len(aliases), 4) * 2
    if label and not label.startswith("系统表情#"):
        score += 4
    if marker and "系统表情#" not in marker:
        score += 1
    return score


def _plan_sort_key(kind: str, plan) -> tuple[int, int, str]:
    mode = str(getattr(plan, "mode", "") or "")
    score = 0
    if kind == "image" and _is_image_only(plan):
        score += 40
    elif kind == "emoji" and _is_emoji_only(plan):
        score += 40
    elif kind == "qq_face" and _is_face_only(plan):
        score += 40
    elif mode:
        score += 10

    specificity = _specificity_score(kind, plan)
    stable_label = _normalize_label(
        getattr(plan, "marker", "")
        or getattr(getattr(plan, "entry", None), "marker", "")
        or getattr(getattr(plan, "entry", None), "description", "")
        or getattr(getattr(plan, "entry", None), "label", "")
    )
    return score, specificity, stable_label


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


def _image_media_part(image_plan) -> dict[str, Any] | None:
    if image_plan is None:
        return None
    entry = getattr(image_plan, "entry", None)
    if entry is None:
        return None
    raw_file_path = str(getattr(entry, "file_path", "") or "").strip()
    if not raw_file_path:
        return None
    return {
        "kind": "image",
        "media_key": str(getattr(entry, "media_key", "") or ""),
        "media_hash": str(getattr(entry, "media_hash", "") or ""),
        "marker": str(getattr(image_plan, "marker", "") or getattr(entry, "marker", "") or ""),
        "description": str(getattr(entry, "description", "") or ""),
        "file_path": raw_file_path,
        "mode": str(getattr(image_plan, "mode", "") or ""),
    }


def _select_media_kind(
    runtime,
    user_text: str,
    *,
    image_plan=None,
    emoji_plan=None,
    face_plan=None,
) -> list[str]:
    choices: list[tuple[str, Any]] = []
    if image_plan is not None:
        choices.append(("image", image_plan))
    if emoji_plan is not None:
        choices.append(("emoji", emoji_plan))
    if face_plan is not None:
        choices.append(("qq_face", face_plan))
    if not choices:
        return []

    max_media = 1
    media_cfg = getattr(getattr(runtime, "cfg", None), "media", None) if runtime is not None else None
    if media_cfg is not None:
        try:
            max_media = int(getattr(media_cfg, "max_media_per_message", 1) or 1)
        except (TypeError, ValueError):
            max_media = 1
    max_media = max(0, max_media)
    if max_media == 0:
        return []

    inbound_image = bool(extract_inbound_marker_labels(user_text, "image"))
    inbound_emoji = bool(extract_inbound_marker_labels(user_text, "emoji"))
    inbound_face = bool(extract_inbound_marker_labels(user_text, "qq_face"))

    ranked_choices: list[tuple[str, tuple[int, int, str], bool]] = []
    for kind, plan in choices:
        score = 0
        if kind == "image":
            if inbound_image:
                score += 10
        elif kind == "emoji":
            if inbound_emoji:
                score += 10
        else:
            if inbound_face:
                score += 10
        plan_score, specificity, stable_label = _plan_sort_key(kind, plan)
        only_mode = (
            (kind == "image" and _is_image_only(plan))
            or (kind == "emoji" and _is_emoji_only(plan))
            or (kind == "qq_face" and _is_face_only(plan))
        )
        ranked_choices.append(
            (kind, (score * 1000 + plan_score, specificity, stable_label), only_mode)
        )

    ranked_choices.sort(key=lambda item: item[1], reverse=True)
    only_mode_choices = [kind for kind, _score, only_mode in ranked_choices if only_mode]
    if only_mode_choices:
        return only_mode_choices[:1]
    return [kind for kind, _score, _only_mode in ranked_choices[:max_media]]


def resolve_reply_media_selection(
    context,
    *,
    runtime=None,
    user_text: str,
    image_plan=None,
    emoji_plan=None,
    face_plan=None,
) -> ReplyMediaSelection:
    image_part = _image_media_part(image_plan)
    emoji_part = _emoji_media_part(context, emoji_plan)
    face_part = _face_media_part(face_plan)
    if image_part is None:
        image_plan = None
    if emoji_part is None:
        emoji_plan = None
    if face_part is None:
        face_plan = None

    selected_kinds = _select_media_kind(
        runtime,
        user_text,
        image_plan=image_plan,
        emoji_plan=emoji_plan,
        face_plan=face_plan,
    )
    selected_kind_set = set(selected_kinds)
    if "image" not in selected_kind_set:
        image_plan = None
        image_part = None
    if "emoji" not in selected_kind_set:
        emoji_plan = None
        emoji_part = None
    if "qq_face" not in selected_kind_set:
        face_plan = None
        face_part = None

    ordered_parts: list[dict[str, Any]] = []
    part_map = {
        "image": image_part,
        "emoji": emoji_part,
        "qq_face": face_part,
    }
    for kind in selected_kinds:
        part = part_map.get(kind)
        if part is not None:
            ordered_parts.append(part)
    return ReplyMediaSelection(
        image_plan=image_plan,
        emoji_plan=emoji_plan,
        face_plan=face_plan,
        media_parts=tuple(ordered_parts),
        suppress_text=_is_image_only(image_plan) or _is_emoji_only(emoji_plan) or _is_face_only(face_plan),
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
