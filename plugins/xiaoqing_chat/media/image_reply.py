from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..logging_utils import _log_step
from ..media_registry import resolve_registered_media_items
from ..memory.memory import StoredMessage
from ..message_parts import normalize_message_parts
from .event_media_common import _SUPPORTED_IMAGE_SUFFIXES
from .reply_planner_common import (
    assistant_turns_since_last_media,
    build_recent_dialogue,
    extract_inbound_marker_labels,
    find_candidate_by_hint,
    parse_candidate_choice,
    pick_ranked_candidates,
    run_selector_llm,
    should_force_media_consideration,
)


@dataclass(frozen=True)
class ImageReplyEntry:
    media_key: str
    media_hash: str
    file_path: str
    description: str
    marker: str


@dataclass(frozen=True)
class ImageReplyPlan:
    entry: ImageReplyEntry
    marker: str
    reasoning: str
    mode: str = "text_with_image"


def _media_cfg(runtime):
    return getattr(getattr(runtime, "cfg", None), "media", None)


def _media_cfg_value(runtime, field: str, default):
    cfg = _media_cfg(runtime)
    if cfg is None:
        return default
    return getattr(cfg, field, default)


_MODE_ALIASES = {
    "none": "none",
    "text": "none",
    "text_only": "none",
    "image_only": "image_only",
    "pic_only": "image_only",
    "photo_only": "image_only",
    "只发图": "image_only",
    "只发图片": "image_only",
    "text_with_image": "text_with_image",
    "text+image": "text_with_image",
    "image_with_text": "text_with_image",
    "文字+图片": "text_with_image",
    "文字加图片": "text_with_image",
}
_TEXT_MODE_HINTS = (
    ("image_only", ("image_only", "只发图", "只发图片", "只发一张图")),
    ("text_with_image", ("text_with_image", "文字+图片", "文字加图片", "图文一起发")),
    ("none", ("none", "不发", "纯文字", "只发文字")),
)
_TOKEN_PATTERN = r"[\u4e00-\u9fff]{1,4}|[a-z0-9_]+"
_DIGIT_PATTERN = r"(?<!\d)(\d{1,2})(?!\d)"


def _entry_relevance(entry: ImageReplyEntry, tokens: Sequence[str]) -> tuple[float, float, float]:
    haystack = " ".join([entry.description, entry.marker]).lower()
    score = 0.0
    for token in tokens:
        if not token:
            continue
        if token in haystack:
            score += 1.8
    if entry.description and not entry.description.startswith("一张图片"):
        score += 0.3
    return score, float(bool(entry.description)), float(bool(entry.marker))


def _pick_candidate_entries(
    entries: Sequence[ImageReplyEntry],
    *,
    user_text: str,
    reply_text: str,
    max_items: int,
) -> list[ImageReplyEntry]:
    return pick_ranked_candidates(
        entries,
        user_text=user_text,
        reply_text=reply_text,
        max_items=max_items,
        token_pattern=_TOKEN_PATTERN,
        score_fn=_entry_relevance,
    )


def _render_candidate_block(candidates: Sequence[ImageReplyEntry]) -> str:
    lines: list[str] = []
    for index, entry in enumerate(candidates, start=1):
        lines.append(
            f"{index}. marker={entry.marker or '[图片]'} | 描述={entry.description or '无'}"
        )
    return "\n".join(lines)


def _find_entry_by_hint(candidates: Sequence[ImageReplyEntry], hint: str) -> ImageReplyEntry | None:
    return find_candidate_by_hint(
        candidates,
        hint,
        key_fn=lambda entry: (
            entry.marker,
            entry.description,
        ),
    )


def _parse_image_choice(output: str, candidates: Sequence[ImageReplyEntry]) -> tuple[str, ImageReplyEntry | None]:
    return parse_candidate_choice(
        output,
        candidates,
        mode_aliases=_MODE_ALIASES,
        text_mode_hints=_TEXT_MODE_HINTS,
        hint_keys=("candidate", "index", "option", "image", "description", "marker"),
        resolve_candidate=lambda hint: _find_entry_by_hint(candidates, hint),
        default_mode="text_with_image",
        digit_pattern=_DIGIT_PATTERN,
    )


def _media_store():
    try:
        from ..runtime_state import get_state as _state

        return getattr(_state(), "media_store", None)
    except Exception:
        return None


def _resolve_image_library_dir(context, runtime) -> Path:
    raw = str(_media_cfg_value(runtime, "image_library_dir", "figures/reply_images") or "").strip()
    if not raw:
        raw = "figures/reply_images"
    path = Path(raw)
    if not path.is_absolute():
        path = (Path(context.plugin_dir) / raw).resolve()
    return path


def _iter_image_library_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _SUPPORTED_IMAGE_SUFFIXES
    ]
    files.sort()
    return files


def _entry_from_media_ref(
    media_ref: dict[str, str],
    *,
    file_path: Path,
) -> ImageReplyEntry:
    description = str(media_ref.get("description", "") or "").strip() or file_path.stem.replace("_", " ")
    marker = str(media_ref.get("marker", "") or "").strip()
    if not marker:
        marker = f"[图片：{description or file_path.stem.replace('_', ' ')}]"
    return ImageReplyEntry(
        media_key=str(media_ref.get("media_key", "") or "").strip(),
        media_hash=str(media_ref.get("media_hash", "") or "").strip(),
        file_path=str(file_path),
        description=description,
        marker=marker,
    )


def _collect_library_image_entries(context, runtime) -> list[ImageReplyEntry]:
    library_dir = _resolve_image_library_dir(context, runtime)
    files = _iter_image_library_files(library_dir)
    if not files:
        return []

    store = _media_store()
    entries: list[ImageReplyEntry] = []
    for file_path in files:
        media_ref = {
            "kind": "image",
            "file_path": str(file_path),
            "description": file_path.stem.replace("_", " "),
        }
        resolved_items = resolve_registered_media_items([media_ref], store=store) or [media_ref]
        resolved = resolved_items[0] if resolved_items else media_ref
        entries.append(_entry_from_media_ref(resolved, file_path=file_path))
    return entries


def _collect_history_image_entries(history: Sequence[StoredMessage]) -> list[ImageReplyEntry]:
    store = _media_store()

    seen_keys: set[str] = set()
    entries: list[ImageReplyEntry] = []
    for message in reversed(history):
        # Only reuse images the assistant has already sent. Replaying user-originated
        # photos/screenshots back at them is usually the wrong behavior.
        if str(getattr(message, "role", "") or "").strip() != "assistant":
            continue
        for part in normalize_message_parts(getattr(message, "parts", ()) or ()):
            if str(part.get("kind", "") or "").strip() != "image":
                continue
            resolved_items = resolve_registered_media_items([part], store=store) or [part]
            resolved = resolved_items[0] if resolved_items else dict(part)
            file_path = str(resolved.get("file_path", "") or "").strip()
            if not file_path:
                continue
            path = Path(file_path)
            if not path.exists():
                continue
            candidate = _entry_from_media_ref(resolved, file_path=path)
            dedupe_key = candidate.media_key or candidate.media_hash or str(path).lower()
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            entries.append(candidate)
    return entries


def _merge_candidate_entries(*entry_groups: Sequence[ImageReplyEntry]) -> list[ImageReplyEntry]:
    seen_keys: set[str] = set()
    merged: list[ImageReplyEntry] = []
    for group in entry_groups:
        for entry in group:
            dedupe_key = entry.media_key or entry.media_hash or entry.file_path.lower()
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            merged.append(entry)
    return merged


def _forced_image_fallback(
    candidates: Sequence[ImageReplyEntry],
    inbound_labels: Sequence[str],
) -> ImageReplyEntry | None:
    for label in inbound_labels:
        selected = _find_entry_by_hint(candidates, label)
        if selected is not None:
            return selected
    return None


async def plan_image_reply(
    *,
    context,
    runtime,
    history: Sequence[StoredMessage],
    user_text: str,
    reply_text: str,
    secrets: dict[str, str] | dict[str, object],
    chat_id: str = "",
    force_consider: bool = False,
) -> ImageReplyPlan | None:
    inbound_labels = extract_inbound_marker_labels(user_text, "image")
    forced = bool(force_consider or should_force_media_consideration(user_text, "image"))
    if chat_id:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.image.plan.start",
            fields={
                "forced": forced,
                "reply_text": reply_text,
                "inbound_marker": "，".join(inbound_labels),
            },
        )

    if not bool(_media_cfg_value(runtime, "enable_outbound_image_reply", False)):
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.image.plan.skip",
                fields={"reason": "disabled"},
            )
        return None
    if not (reply_text or "").strip():
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.image.plan.skip",
                fields={"reason": "empty_reply"},
            )
        return None

    probability = float(_media_cfg_value(runtime, "image_reply_probability", 0.12))
    roll = random.random()
    if not forced and roll > probability:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.image.plan.skip",
                fields={"reason": "probability", "roll": round(roll, 4), "threshold": probability},
            )
        return None

    cooldown_turns = max(0, int(_media_cfg_value(runtime, "image_cooldown_turns", 4)))
    turns_since_last = assistant_turns_since_last_media(history, "image")
    if turns_since_last is not None and turns_since_last < cooldown_turns:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.image.plan.skip",
                fields={
                    "reason": "cooldown",
                    "turns_since_last": turns_since_last,
                    "cooldown_turns": cooldown_turns,
                },
            )
        return None

    entries = _merge_candidate_entries(
        _collect_library_image_entries(context, runtime),
        _collect_history_image_entries(history),
    )
    if not entries:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.image.plan.skip",
                fields={"reason": "no_entries"},
            )
        return None

    candidate_count = max(1, int(_media_cfg_value(runtime, "image_candidate_count", 4)))
    candidates = _pick_candidate_entries(
        entries,
        user_text=user_text,
        reply_text=reply_text,
        max_items=candidate_count,
    )
    if not candidates:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.image.plan.skip",
                fields={"reason": "no_candidates"},
            )
        return None

    recent_dialogue = build_recent_dialogue(history, context=context, current_text=user_text)
    candidate_block = _render_candidate_block(candidates)
    prompt = (
        "你要决定这次回复要不要带一张图片或动图，并判断是只发图，还是发文字后再补一张图。"
        '只输出 JSON，不要额外解释。格式: {"mode":"none|image_only|text_with_image","candidate":"候选编号","reason":"..."}。\n'
        f"最近对话\n{recent_dialogue or '（无）'}\n\n"
        f"对方刚发\n{user_text.strip()}\n\n"
        f"你准备回复\n{reply_text.strip()}\n\n"
        f"可选图片候选\n{candidate_block}\n\n"
        "选择规则："
        "\n1. 如果纯文字已经最自然，或者候选都只是普通配图、没有额外交流价值，mode 选 none。"
        "\n2. 如果只发一张图更像真人、更像甩图回应，mode 选 image_only。"
        "\n3. 如果文字已经合适，但补一张图能增加语气、包袱、反差、收尾或补充场景，mode 选 text_with_image。"
        "\n4. candidate 必须从候选编号里选；如果 mode 是 none，candidate 留空。"
        "\n5. 如果这张图只是重复对方刚发图片的内容，或者只是把文字已经表达清楚的意思再说一遍，优先选 none。"
    )

    output = await run_selector_llm(
        context=context,
        runtime=runtime,
        secrets=secrets,
        system_prompt="你是聊天回复模态选择器，只输出指定 JSON。mode 只能是 none、image_only、text_with_image。",
        user_prompt=prompt,
    )

    mode, selected = _parse_image_choice(output or "", candidates)
    if selected is None and forced:
        selected = _forced_image_fallback(candidates, inbound_labels)
        if selected is not None and mode == "none":
            mode = "text_with_image"
    if mode == "none" or selected is None:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.image.plan.skip",
                fields={"reason": "selector_none", "selector_output": (output or "").strip()},
            )
        return None

    plan = ImageReplyPlan(
        entry=selected,
        marker=selected.marker,
        reasoning=f"image_mode:{mode};source=image_candidates",
        mode=mode,
    )
    if chat_id:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.image.plan.ok",
            fields={
                "mode": mode,
                "marker": selected.marker,
                "selector_output": (output or "").strip(),
            },
        )
    return plan
