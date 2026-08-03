"""闲聊链路的媒体桥接。

本模块只负责三件事：把入站媒体转成可持久化记录、把出站 parts 同步到媒体注册表，
以及在消息确认送达后登记媒体使用次数。消息 parts 始终是结构事实来源，展示文本和旧版
``content + media_items`` 仅在边界处派生，避免三套表示各自演化。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from .media.emoji_library import mark_emoji_used, mark_emoji_used_by_hash
from .media.event_media import render_event_media
from .media.event_media_common import RenderedMedia
from .media.marker_resolver import ResolvedMarker
from .media.qq_face_catalog import mark_qq_face_used, mark_qq_face_used_by_id
from .media_registry import upsert_registered_media_items
from .message_parts import (
    build_text_message_parts,
    message_parts_to_legacy,
    normalize_message_parts,
    replace_message_media_parts,
)
from .smalltalk_models import _GeneratedSmalltalkTurn

if TYPE_CHECKING:
    from .runtime_state import ChatRuntimeState, _ChatRuntime

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
    marker: ResolvedMarker | None,
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成行动历史需要的最小媒体摘要，优先采用已解析 marker。"""

    if marker is not None:
        entry = getattr(marker, "entry", None)
        return _drop_empty(
            {
                "media_kind": str(getattr(marker, "kind", "") or ""),
                "media_marker": str(getattr(marker, "marker", "") or ""),
                "media_mode": str(getattr(marker, "mode", "") or ""),
                "media_hash": str(getattr(entry, "media_hash", "") or ""),
                "media_key": str(getattr(entry, "media_key", "") or ""),
                "media_face_id": str(getattr(entry, "face_id", "") or ""),
            }
        )

    part = _first_media_part(parts)
    if not part:
        return {}
    return _drop_empty(
        {
            "media_kind": str(part.get("kind", "") or ""),
            "media_marker": str(part.get("marker", "") or ""),
            "media_mode": str(part.get("mode", "") or ""),
            "media_hash": str(part.get("media_hash", "") or ""),
            "media_key": str(part.get("media_key", "") or ""),
            "media_face_id": str(part.get("face_id", "") or ""),
        }
    )


def _serialize_rendered_media_items(
    rendered_items: Iterable[RenderedMedia],
) -> list[dict[str, Any]]:
    """把本轮渲染对象压成记忆层可持久化的普通字典。"""

    serialized: list[dict[str, Any]] = []
    for item in rendered_items:
        marker = str(item.marker or "").strip()
        description = str(item.description or "").strip()
        media_hash = str(item.media_hash or "").strip()
        kind = str(item.kind or "").strip()
        face_id = str(item.face_id or "").strip()
        emotion_tags = [str(tag).strip() for tag in (item.emotion_tags or ()) if str(tag).strip()]
        payload = _drop_empty(
            {
                "kind": kind,
                "media_hash": media_hash,
                "face_id": face_id,
                "marker": marker,
                "description": description,
                "emotion_tags": emotion_tags,
                "file_path": str(item.cached_path) if item.cached_path is not None else "",
            }
        )
        if kind == "qq_face" and description:
            payload["label"] = description
        if payload:
            serialized.append(payload)
    return serialized


async def _event_media_items_for_memory(
    event: dict[str, Any], *, context, runtime
) -> list[dict[str, Any]]:
    """复用事件级渲染缓存；解析失败时让文字消息继续进入记忆。"""

    cached_items = event.get("_xc_rendered_media_items")
    if isinstance(cached_items, list):
        return _serialize_rendered_media_items(
            item for item in cached_items if isinstance(item, RenderedMedia)
        )
    try:
        rendered_items = await render_event_media(event, context=context, runtime=runtime)
    except Exception:
        return []
    return _serialize_rendered_media_items(rendered_items)


def _sync_message_parts_to_registry(
    state: ChatRuntimeState,
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    *,
    context: Any,
    runtime: _ChatRuntime,
    schedule_media_registry_flush: Callable[[Any, _ChatRuntime], None],
) -> tuple[dict[str, Any], ...]:
    """登记 parts 中的媒体引用，并在注册表真正变脏时安排一次刷盘。"""

    normalized_parts = cast(tuple[dict[str, Any], ...], normalize_message_parts(parts))
    if not normalized_parts:
        return ()
    _content, media_items = message_parts_to_legacy(normalized_parts)
    media_store = state.media_store
    synced_media_items = cast(
        list[dict[str, Any]],
        upsert_registered_media_items(
            media_items,
            store=media_store,
            compact=False,
        ),
    )
    if media_items and media_store.is_dirty():
        # 刷盘属于旁路维护；调度失败不能让已经生成的回复消失。
        try:
            schedule_media_registry_flush(context, runtime)
        except Exception:
            pass
    return cast(
        tuple[dict[str, Any], ...],
        replace_message_media_parts(
            normalized_parts,
            synced_media_items,
            store=media_store,
        ),
    )


def _prefix_reply_parts(
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    prefix_text: str,
) -> tuple[dict[str, Any], ...]:
    """把发送层前缀并入首个文本 part，保持媒体的原始相对顺序。"""

    normalized_parts = cast(tuple[dict[str, Any], ...], normalize_message_parts(parts))
    prefix = str(prefix_text or "")
    if not prefix:
        return normalized_parts
    if not normalized_parts:
        return cast(tuple[dict[str, Any], ...], build_text_message_parts(prefix))

    merged_parts = [dict(part) for part in normalized_parts]
    if str(merged_parts[0].get("kind", "") or "").strip() == "text":
        merged_parts[0]["text"] = prefix + str(merged_parts[0].get("text", "") or "")
    else:
        merged_parts.insert(0, {"kind": "text", "text": prefix})
    return cast(tuple[dict[str, Any], ...], normalize_message_parts(merged_parts))


def _display_reply_text(generated: _GeneratedSmalltalkTurn) -> str:
    if generated.reply_output is not None:
        return str(generated.reply_output.payload.display_text)
    return str(generated.reply)


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
    """重写回复后清掉旧 payload，强制后续阶段从最新 parts 重建。"""

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


def _mark_reply_media_used(context, generated: _GeneratedSmalltalkTurn) -> None:
    """登记已送达回复中的媒体；统计失败不反向影响发送结果。"""

    marker = generated.media_marker
    marker_kind = str(getattr(marker, "kind", "") or "") if marker is not None else ""
    reply_parts = normalize_message_parts(generated.reply_parts)

    if marker is not None and marker_kind == "emoji":
        try:
            mark_emoji_used(context, marker.entry)
        except Exception:
            pass
    elif marker_kind != "emoji":
        for part in reply_parts:
            if str(part.get("kind", "") or "").strip() != "emoji":
                continue
            try:
                mark_emoji_used_by_hash(context, str(part.get("media_hash", "") or ""))
            except Exception:
                pass

    if marker is not None and marker_kind == "qq_face":
        try:
            mark_qq_face_used(context, marker.entry)
        except Exception:
            pass
    elif marker_kind != "qq_face":
        for part in reply_parts:
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
