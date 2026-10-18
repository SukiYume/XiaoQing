"""回复草稿：规范化文本分段、附加出站媒体，以及审查所需的输入与尝试状态。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass
from .llm.check_result import ReplyCheckResult
from .llm.postprocess import join_reply
from .media.marker_resolver import (
    ResolvedMarker,
    marker_media_part,
)
from .message_parts import (
    build_text_message_parts,
    merge_reply_media_parts,
    normalize_message_parts,
)
from .reply_payload import build_reply_payload_from_parts


@dataclass(frozen=True)
class ReplyDraft:
    """已规范化、可进入审查和媒体合并阶段的回复草稿。"""

    text: str
    text_parts: tuple[str, ...]
    parts: tuple[dict[str, Any], ...]
    raw_text: str                       = ""
    rewritten_text: str                 = ""
    media_marker: ResolvedMarker | None = None


def _normalize_reply_text_parts(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    cleaned: list[str] = []
    for item in values:
        if isinstance(item, str):
            text = item.strip()
        elif item in (None, "", False):
            text = ""
        else:
            continue
        if text:
            cleaned.append(text)
    return tuple(cleaned)


def build_reply_draft(
    text_parts: Any,
    *,
    raw_text: str       = "",
    rewritten_text: str = "",
) -> ReplyDraft | None:
    normalized_text_parts = _normalize_reply_text_parts(text_parts)
    reply_text            = join_reply(normalized_text_parts)
    if not reply_text:
        return None
    return ReplyDraft(
        text           = reply_text,
        text_parts     = normalized_text_parts,
        parts          = build_text_message_parts(reply_text),
        raw_text       = str(raw_text or ""),
        rewritten_text = str(rewritten_text or raw_text or ""),
    )


def _draft_has_media(parts: Any) -> bool:
    return any(
        str(part.get("kind", "") or "").strip() != "text" for part in normalize_message_parts(parts)
    )


def attach_reply_media_marker(
    draft: ReplyDraft,
    *,
    context,
    resolved_marker: ResolvedMarker | None = None,
) -> ReplyDraft:
    if resolved_marker is None or _draft_has_media(draft.parts):
        return draft

    media_part = marker_media_part(context, resolved_marker)
    if media_part is None:
        return draft

    merged_parts = merge_reply_media_parts(
        draft.parts,
        (media_part,),
    )
    return ReplyDraft(
        text           = draft.text or resolved_marker.marker,
        text_parts     = draft.text_parts,
        parts          = merged_parts,
        raw_text       = draft.raw_text,
        rewritten_text = draft.rewritten_text,
        media_marker   = resolved_marker,
    )


def reply_checker_inputs(draft: ReplyDraft) -> tuple[str, str]:
    normalized_parts = normalize_message_parts(draft.parts)
    payload          = build_reply_payload_from_parts(normalized_parts)
    final_reply      = str(payload.display_text or "").strip() or str(draft.text or "").strip()
    heuristic_reply  = "".join(
        str(part.get("text", "") or "")
        for part in normalized_parts
        if str(part.get("kind", "") or "").strip() == "text"
    ).strip()
    if not heuristic_reply:
        heuristic_reply = final_reply
    return final_reply, heuristic_reply


@dataclass
class ReplyAttemptState:
    max_items: int
    prefetched_memory: asyncio.Task[str] | None = None
    cached_memory: str | None                   = None
    regen_used: int                             = 0
    extra_check_hint: str                       = ""


@dataclass(frozen=True)
class RejectedCandidate:
    text: str
    result: ReplyCheckResult
    draft: ReplyDraft | None = None
    # 只有明确标记为 soft 的风格拒绝才允许在强制回复的重生成耗尽后采用。
    # 上下文、说话人、人物经历、事实、结构和媒体错误都属于 hard，绝不放行。
    allow_after_regen_exhausted: bool = False
