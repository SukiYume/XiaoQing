"""深读：把“扫了一眼”的 B 站条目读深一点——简介全文、UP 主置顶 / 回复、字幕。

读到多少说多少：有字幕段的条目是“跟着字幕看了”（``subtitle_partial`` 记是否节选），有其他
段的是“认真读了”，什么都没有就还是“扫了一眼”。任何一步失败只是少一块，不抛错。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from ..safety import is_blocked
from . import subtitle
from .base import (
    Fetcher,
    SourceError,
    SourceItem,
    SourcePassage,
    as_dict,
    as_list,
    segment_text,
)
from .bilibili import bvid_of

logger = logging.getLogger(__name__)

MAX_DESC_CHARS    = 800
MAX_DESC_SEGMENTS = 4
MAX_PINNED_CHARS  = 300
MAX_PINNED_SEGS   = 2
MAX_UP_REPLIES    = 3
UP_REPLY_CHARS    = 200

SKIM, READ, WATCHED = "skim", "read", "watched"


def assign_refs(passages: Iterable[SourcePassage], index: int) -> tuple[SourcePassage, ...]:
    """按条目序号重新编号 ``p<序号>.<n>``。"""

    return tuple(replace(p, ref=f"p{index}.{n}") for n, p in enumerate(passages, start=1))


def depth_of(item: SourceItem) -> str:
    """经历的 ``depth``：有字幕段是看了，有其他段是读了，否则扫了一眼。"""

    if any(p.kind == "subtitle" for p in item.passages):
        return WATCHED
    return READ if item.passages else SKIM


def _message(reply: Any) -> str:
    return str(as_dict(as_dict(reply).get("content")).get("message") or "")


def pinned_passages(page: dict[str, Any], blocked: Iterable[str]) -> list[SourcePassage]:
    """``data.top_replies[]`` 或 ``data.top.upper``（UP 主置顶，≤300 字切成 ≤2 段）。"""

    candidates = [*as_list(page.get("top_replies")), as_dict(page.get("top")).get("upper")]
    for reply in candidates:
        text = _message(reply)
        if not text:
            continue
        found = segment_text(
            text,
            "pinned",
            blocked,
            max_total    = MAX_PINNED_CHARS,
            max_segments = MAX_PINNED_SEGS,
        )
        if found:
            return found
    return []


def up_reply_passages(page: dict[str, Any], blocked: Iterable[str]) -> list[SourcePassage]:
    """楼中楼里 ``member.mid`` 等于 ``data.upper.mid`` 的回复（≤3 条）；结构缺失就没有。"""

    mid = as_dict(page.get("upper")).get("mid")
    if mid in (None, "", 0, "0"):
        return []
    blocked                    = tuple(blocked)
    found: list[SourcePassage] = []
    roots                      = [*as_list(page.get("top_replies")), *as_list(page.get("replies"))]
    for root in roots:
        for sub in as_list(as_dict(root).get("replies")):
            member = as_dict(as_dict(sub).get("member"))
            if str(member.get("mid")) != str(mid):
                continue
            text = segment_text(
                _message(sub), "up_reply", blocked, max_total=UP_REPLY_CHARS, max_segments=1
            )
            if text:
                found.append(text[0])
            if len(found) >= MAX_UP_REPLIES:
                return found
    return found


def desc_passages(item: SourceItem, blocked: Iterable[str]) -> list[SourcePassage]:
    """简介全文（``watch`` 留下的原始 desc）：合计 ≤800 字、≤4 段。"""

    return segment_text(
        item.desc_raw, "desc", blocked, max_total=MAX_DESC_CHARS, max_segments=MAX_DESC_SEGMENTS
    )


async def deep_read(
    fetch: Fetcher,
    item: SourceItem,
    index: int,
    blocked: Iterable[str],
    *,
    subtitles: bool,
    reply_page: dict[str, Any] | None = None,
) -> SourceItem:
    """补 ``passages``。``reply_page`` 是已取到的热评接口 ``data``（没有就不读置顶 / UP 主回复）。

    除字幕（``nav``、``player/wbi/v2`` 与字幕文件）外不发新请求。
    """

    blocked                    = tuple(blocked)
    found: list[SourcePassage] = desc_passages(item, blocked)
    if reply_page:
        found += pinned_passages(reply_page, blocked)
        found += up_reply_passages(reply_page, blocked)
    partial = False
    bvid    = bvid_of(item)
    if subtitles and bvid and item.cid:
        try:
            lines, partial = await subtitle.read(fetch, bvid, item.cid, blocked)
            found += lines
        except SourceError as exc:
            logger.info("xiaoqing_life step=source name=bili_subtitle failed reason=%s", exc.reason)
    kept = [p for p in found if not is_blocked(p.text, blocked)]
    return replace(
        item,
        passages         = assign_refs(kept, index),
        subtitle_partial = partial and any(p.kind == "subtitle" for p in kept),
    )
