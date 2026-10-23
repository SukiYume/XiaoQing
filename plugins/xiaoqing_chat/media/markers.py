"""媒体标记与描述：生成 [图片：…]/[表情包：…] 标记，判断描述质量并给出降级描述。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..constants import GENERIC_MEDIA_LABELS
from .loading import looks_like_unusable_source_label, safe_source_name
from .models import RenderedMedia, ResolvedMedia

if TYPE_CHECKING:
    pass

_EMOJI_HINT_RE = re.compile(
    r"(表情|emoji|sticker|meme|梗图|mface|贴纸|无语|开心|委屈|猫猫|商城表情|收藏表情)",
    re.IGNORECASE,
)


_GENERIC_MEDIA_HINTS = frozenset({"[图片]", "[动画表情]", "[表情]", "图片", "表情"})


_STRUCTURED_MEDIA_TAG_STOPWORDS = frozenset(
    {
        "json",
        "kind",
        "emoji",
        "image",
        "description",
        "summary",
        "label",
        "visibletext",
        "detaileddescription",
        "detaildescription",
        "emotiontags",
        "emotions",
    }
)


def normalize_emotion_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = re.split(r"[,，/\s]+", value)
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []

    tags: list[str] = []
    for item in candidates:
        if looks_like_structured_media_text(item):
            continue
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", item or "").strip()
        lowered = cleaned.lower()
        if not cleaned:
            continue
        if lowered in _STRUCTURED_MEDIA_TAG_STOPWORDS:
            continue
        if cleaned.startswith("think") or "用户" in cleaned or "输入数据" in cleaned:
            continue
        if cleaned not in tags:
            tags.append(cleaned[:12])
    return tuple(tags[:4])


def segment_summary_hint(segment: dict[str, Any]) -> str:
    data    = segment.get("data", {}) or {}
    generic = ""
    for key in ("summary", "text", "name", "key", "emoji_id"):
        value = str(data.get(key, "") or "").strip()
        if not value:
            continue
        if value in _GENERIC_MEDIA_HINTS:
            if not generic:
                generic = value
            continue
        return value
    return generic


def segment_prefers_emoji(segment: dict[str, Any]) -> bool:
    segment_type = str(segment.get("type", "") or "")
    if segment_type == "mface":
        return True

    data = segment.get("data", {}) or {}
    for key in ("emoji_id", "emoji_package_id", "key"):
        value = data.get(key)
        if value not in (None, ""):
            return True

    return bool(_EMOJI_HINT_RE.search(segment_summary_hint(segment)))


def fallback_kind(source_name: str, *, width: int, height: int, segment_type: str) -> str:
    if segment_type == "mface":
        return "emoji"
    if _EMOJI_HINT_RE.search(source_name or ""):
        return "emoji"
    if (
        not source_name
        and width
        and height
        and max(width, height) <= 512
        and abs(width - height) <= 96
    ):
        return "emoji"
    return "image"


def build_marker(kind: str, description: str, emotion_tags: tuple[str, ...]) -> str:
    if kind == "emoji":
        label = "，".join(emotion_tags[:2]).strip() or description.strip() or "一张表情包"
        return f"[表情包：{label}]"
    desc = description.strip() or "一张图片"
    return f"[图片：{desc}]"


def build_context_marker(rendered: RenderedMedia) -> str:
    marker        = rendered.marker.strip()
    cultural_hint = rendered.cultural_hint.strip()
    if rendered.kind != "emoji" or not marker.startswith("[表情包："):
        return _append_cultural_hint(marker, cultural_hint)

    description = rendered.description.strip()
    if not description or is_generic_media_label(description):
        return _append_cultural_hint(marker, cultural_hint)

    label = "，".join(rendered.emotion_tags[:2]).strip()
    if not label:
        return _append_cultural_hint(marker, cultural_hint)

    clean_desc, visible_text = split_visible_text(description)
    rendered_marker = emoji_summary_marker(label, clean_desc, visible_text)
    return _append_cultural_hint(rendered_marker or marker, cultural_hint)


def emoji_summary_marker(label: str, clean_desc: str, visible_text: str) -> str:
    """表情包摘要同时保留情绪标签、画面内容和配字；画面和配字都缺时返回空串。

    追问动物、颜色、位置要靠画面内容，追问配字要靠文字，任何一项都不能因另一项存在而省略。
    """
    label   = str(label or "").strip()
    content = str(clean_desc or "").strip()
    speech  = str(visible_text or "").strip()
    if content and (
        is_generic_media_label(content)
        or _normalize_media_label(content) == _normalize_media_label(label)
    ):
        content = ""
    if not content and not speech:
        return ""
    pieces = [label or "一张表情包"]
    if content:
        pieces.append(f"内容：{content}")
    if speech:
        pieces.append(f"写着“{speech}”")
    return "[表情包：" + "；".join(pieces) + "]"


def _append_cultural_hint(marker: str, cultural_hint: str) -> str:
    base = str(marker or "").strip()
    hint = str(cultural_hint or "").strip()
    if not base or not hint:
        return base
    if not (base.startswith("[") and base.endswith("]")):
        return base
    inner = base[1:-1].strip()
    if not inner:
        return base
    if "梗背景" in inner:
        return base
    return f"[{inner}；梗背景：{hint}]"


def _normalize_media_label(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    text = re.sub(r"^(QQ表情|表情包|图片)\s*[：:]", "", text)
    return re.sub(r"\s+", "", text)


def is_generic_media_label(value: str) -> bool:
    normalized = _normalize_media_label(value)
    if not normalized:
        return True
    return normalized in GENERIC_MEDIA_LABELS


_VISIBLE_TEXT_RE = re.compile(r"^(?:(.+?)，)?文字内容是“(.+?)”$")


def split_visible_text(description: str) -> tuple[str, str]:
    """把 ``描述，文字内容是“X”`` 拆成（清洁描述，可见文字）。

    不含该后缀时返回原描述和空的可见文字。
    """
    text = str(description or "").strip()
    if not text:
        return "", ""
    match = _VISIBLE_TEXT_RE.match(text)
    if not match:
        return text, ""
    clean_desc   = (match.group(1) or "").strip()
    visible_text = (match.group(2) or "").strip()
    return clean_desc, visible_text


def looks_like_structured_media_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "<think" in lowered or "</think>" in lowered:
        return True
    if "```" in text:
        return True
    if lowered.startswith("json") and "{" in text:
        return True
    if any(
        token in text
        for token in (
            '"kind"',
            '"description"',
            '"emotion_tags"',
            '"detailed_description"',
            '"visible_text"',
            '{"detailed_description"',
            "输入数据：",
        )
    ):
        return True
    return any(
        phrase in text
        for phrase in (
            "用户给的例子",
            "现在重新分析",
            "需要提取信息",
            "只输出 JSON",
            "只输出JSON",
        )
    )


def can_use_raw_media_description(value: str) -> bool:
    text = str(value or "").strip().strip("`")
    if not text:
        return False
    if text.startswith("{"):
        return False
    return not looks_like_structured_media_text(text)


def is_low_quality_rendered_media(
    rendered: RenderedMedia,
    *,
    summary_hint: str,
    resolved: ResolvedMedia,
) -> bool:
    if looks_like_structured_media_text(rendered.description):
        return True
    # 来源标签只看模型自己的描述；图上转写的网址可以带任何参数。
    if looks_like_unusable_source_label(split_visible_text(rendered.description)[0]):
        return True
    if looks_like_structured_media_text(rendered.marker):
        return True
    if any(looks_like_structured_media_text(tag) for tag in rendered.emotion_tags):
        return True
    if is_generic_media_label(rendered.description):
        return True
    if (
        rendered.kind == "emoji"
        and not rendered.emotion_tags
        and is_generic_media_label(rendered.marker)
    ):
        return True
    summary_label = _normalize_media_label(summary_hint)
    if (
        summary_label
        and summary_label == _normalize_media_label(rendered.description)
        and is_generic_media_label(summary_hint)
    ):
        return True
    source_label = _normalize_media_label(resolved.source_name)
    return bool(
        source_label
        and source_label == _normalize_media_label(rendered.description)
        and is_generic_media_label(resolved.source_name)
    )


def build_fallback_render(
    resolved: ResolvedMedia,
    *,
    summary_hint: str  = "",
    prefer_emoji: bool = False,
) -> RenderedMedia:
    if prefer_emoji:
        kind = "emoji"
    else:
        kind = fallback_kind(
            summary_hint or resolved.source_name,
            width        = resolved.width,
            height       = resolved.height,
            segment_type = resolved.segment_type,
        )
    label = safe_source_name(summary_hint or resolved.source_name)
    if kind == "emoji":
        emotion_tags = normalize_emotion_tags(label)
        description  = label or "一张聊天表情包"
    else:
        emotion_tags = ()
        # 文件名和传输摘要都不是视觉证据。视觉路由失败时明确标记未理解像素，
        # 避免回复模型根据来源标签猜测图片内容。
        description = "图片内容暂时无法识别"
    marker = build_marker(kind, description, emotion_tags)
    return RenderedMedia(
        media_hash   = resolved.media_hash,
        kind         = kind,
        description  = description,
        emotion_tags = emotion_tags,
        marker       = marker,
        cached_path  = resolved.cached_path,
    )


def same_rendered_media(left: RenderedMedia, right: RenderedMedia) -> bool:
    return (
        left.kind == right.kind
        and left.face_id == right.face_id
        and left.description == right.description
        and tuple(left.emotion_tags) == tuple(right.emotion_tags)
        and left.marker == right.marker
    )
