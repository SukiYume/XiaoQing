"""按媒体哈希缓存渲染结果，带容量上限、按时间淘汰与提示词版本校验。"""

from __future__ import annotations

import json
import math
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.atomic_store import keyed_path_lock
from core.plugin_base import load_json, write_json

if TYPE_CHECKING:
    pass

from .markers import build_marker, normalize_emotion_tags
from .models import RenderedMedia, ResolvedMedia
from .settings import MEDIA_ANALYSIS_PROMPT_VERSION

_RENDER_CACHE_MAX_ENTRIES = 1_000


_RENDER_CACHE_MAX_BYTES = 4 * 1024 * 1024


def _render_cache_path(data_dir: Path) -> Path:
    return data_dir / "media" / "render_cache.json"


def load_render_cache(
    data_dir: Path,
    *,
    max_bytes: int = _RENDER_CACHE_MAX_BYTES,
) -> dict[str, Any]:
    cache_path = _render_cache_path(data_dir)
    byte_limit = max(0, int(max_bytes))
    if byte_limit == 0:
        return {"items": {}}
    for candidate in (cache_path, cache_path.with_name(f"{cache_path.name}.bak")):
        try:
            if candidate.stat().st_size > byte_limit:
                return {"items": {}}
        except FileNotFoundError:
            continue
        except OSError:
            return {"items": {}}
    loaded: object = load_json(cache_path, default={"items": {}})
    if not isinstance(loaded, dict):
        return {"items": {}}
    payload = loaded
    items   = payload.get("items")
    if not isinstance(items, dict):
        payload["items"] = {}
    return payload


def _save_render_cache(data_dir: Path, cache: dict[str, Any]) -> None:
    write_json(_render_cache_path(data_dir), cache)


def render_cache_lock(data_dir: Path) -> AbstractContextManager[None]:
    return keyed_path_lock(_render_cache_path(data_dir))


def _render_cache_entry_timestamp(item: tuple[str, Any]) -> tuple[float, str]:
    key, payload = item
    try:
        timestamp = float(payload.get("updated_at", 0.0)) if isinstance(payload, dict) else 0.0
    except (TypeError, ValueError):
        timestamp = 0.0
    if not math.isfinite(timestamp):
        timestamp = 0.0
    return timestamp, key


def _serialized_render_cache_size(cache: dict[str, Any]) -> int:
    return len(
        json.dumps(
            cache,
            ensure_ascii = False,
            indent       = 2,
            allow_nan    = False,
        ).encode("utf-8")
    )


def _prune_render_cache(
    cache: dict[str, Any],
    *,
    max_entries: int,
    max_bytes: int,
) -> dict[str, Any]:
    items                         = cache.get("items")
    normalized_items              = dict(items) if isinstance(items, dict) else {}
    bounded_cache: dict[str, Any] = {"items": normalized_items}
    entry_limit                   = max(0, int(max_entries))
    byte_limit                    = max(0, int(max_bytes))
    oldest_first = sorted(normalized_items.items(), key=_render_cache_entry_timestamp)

    while len(normalized_items) > entry_limit and oldest_first:
        key, _payload = oldest_first.pop(0)
        normalized_items.pop(key, None)
    while normalized_items and (
        byte_limit == 0 or _serialized_render_cache_size(bounded_cache) > byte_limit
    ):
        key, _payload = oldest_first.pop(0)
        normalized_items.pop(key, None)
    return bounded_cache


def write_render_cache_entry(
    data_dir: Path,
    resolved: ResolvedMedia,
    rendered: RenderedMedia,
    *,
    source: str,
    quality: str,
    prompt_version: int | None = None,
    max_entries: int           = _RENDER_CACHE_MAX_ENTRIES,
    max_bytes: int             = _RENDER_CACHE_MAX_BYTES,
) -> None:
    normalized_source = str(source or "").strip() or "fallback"
    if prompt_version is None:
        prompt_version = MEDIA_ANALYSIS_PROMPT_VERSION if normalized_source == "llm" else 0
    cache_payload = {
        "kind": rendered.kind,
        "description": rendered.description,
        "emotion_tags": list(rendered.emotion_tags),
        "marker": rendered.marker,
        "analysis_source": normalized_source,
        "analysis_quality": str(quality or "").strip(),
        "analysis_prompt_version": int(prompt_version or 0),
        "cultural_hint": rendered.cultural_hint.strip(),
        "updated_at": time.time(),
    }
    with render_cache_lock(data_dir):
        cache = load_render_cache(data_dir, max_bytes=max_bytes)
        items                      = cache.setdefault("items", {})
        items[resolved.media_hash] = cache_payload
        bounded_cache              = _prune_render_cache(
            cache,
            max_entries = max_entries,
            max_bytes   = max_bytes,
        )
        _save_render_cache(data_dir, bounded_cache)


def rendered_media_from_cache(cached: dict[str, Any], *, resolved: ResolvedMedia) -> RenderedMedia:
    kind         = str(cached.get("kind", "") or "").strip() or "image"
    description  = str(cached.get("description", "") or "").strip()
    emotion_tags = normalize_emotion_tags(cached.get("emotion_tags"))
    marker       = str(cached.get("marker", "") or "").strip() or build_marker(
        kind, description, emotion_tags
    )
    face_id       = str(cached.get("face_id", "") or "").strip()
    cultural_hint = str(cached.get("cultural_hint", "") or "").strip()
    return RenderedMedia(
        media_hash    = resolved.media_hash,
        kind          = kind,
        description   = description,
        emotion_tags  = emotion_tags,
        marker        = marker,
        cached_path   = resolved.cached_path,
        face_id       = face_id,
        cultural_hint = cultural_hint,
    )
