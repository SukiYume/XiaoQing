"""B 站：热门、排行、搜索与热评（含楼中楼和评论图片）。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from ..safety import clean_text, is_blocked
from .base import (
    COMMENT_CHARS,
    MAX_COMMENT_PICS,
    MAX_REPLIES,
    REPLY_CHARS,
    Fetcher,
    SourceComment,
    SourceError,
    SourceItem,
    as_dict,
    as_list,
    build_item,
    require_ok,
    to_int,
)

API         = "https://api.bilibili.com"
REFERER     = "https://www.bilibili.com/"
RANKING_CAP = 30


def bvid_of(item: SourceItem) -> str:
    """条目链接末段（BV 号）；不是 BV 号时为空。"""

    tail = item.url.rsplit("/", 1)[-1]
    return tail if tail.startswith("BV") else ""


def aid_of(item: SourceItem) -> str:
    """条目 ID 的原生部分（视频 aid），用于取评论。"""

    return item.item_id.split(":", 1)[1]


def _video_item(source: str, raw: dict[str, Any], blocked: Iterable[str]) -> SourceItem | None:
    stat = as_dict(raw.get("stat"))
    aid  = raw.get("aid") or raw.get("id")
    if not aid:
        return None
    tags = [raw.get("tname") or raw.get("typename")]
    tags += list(str(raw.get("tag") or "").split(",")[:3])
    return build_item(
        source    = source,
        id_prefix = "bili",
        uploader  = as_dict(raw.get("owner")).get("name") or raw.get("author"),
        native_id = str(aid),
        title     = raw.get("title"),
        text      = raw.get("desc") or raw.get("description"),
        url       = f"https://www.bilibili.com/video/{raw.get('bvid') or 'av' + str(aid)}",
        heat      = to_int(stat.get("view", raw.get("play"))),
        tags      = tags,
        blocked   = blocked,
    )


def _collect(
    source: str, rows: Iterable[Any], blocked: Iterable[str], limit: int
) -> list[SourceItem]:
    items: list[SourceItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _video_item(source, row, blocked)
        if item is not None:
            items.append(item)
        if len(items) >= limit:
            break
    return items


async def popular(fetch: Fetcher, blocked: Iterable[str], limit: int = 20) -> list[SourceItem]:
    payload = require_ok(
        "bili_popular",
        await fetch.get_json(
            f"{API}/x/web-interface/popular",
            params  = {"ps": "20", "pn": "1"},
            referer = REFERER,
        ),
    )
    rows = as_list(as_dict(payload.get("data")).get("list"))
    return _collect("bili_popular", rows, blocked, limit)


async def ranking(
    fetch: Fetcher, blocked: Iterable[str], limit: int = RANKING_CAP
) -> list[SourceItem]:
    payload = require_ok(
        "bili_ranking",
        await fetch.get_json(
            f"{API}/x/web-interface/ranking/v2",
            params  = {"rid": "0", "type": "all"},
            referer = REFERER,
        ),
    )
    rows = as_list(as_dict(payload.get("data")).get("list"))[:RANKING_CAP]
    return _collect("bili_ranking", rows, blocked, limit)


async def search(
    fetch: Fetcher, keyword: str, blocked: Iterable[str], limit: int = 10
) -> list[SourceItem]:
    """搜索可能随时要求 wbi 签名；返回非 0 码按来源失败处理。"""

    payload = require_ok(
        "bili_search",
        await fetch.get_json(
            f"{API}/x/web-interface/search/type",
            params  = {"search_type": "video", "keyword": keyword, "page": "1"},
            referer = "https://search.bilibili.com/",
        ),
    )
    rows = as_list(as_dict(payload.get("data")).get("result"))
    return _collect("bili_search", rows, blocked, limit)


def _picture_urls(content: dict[str, Any]) -> tuple[str, ...]:
    urls = [
        str(pic.get("img_src"))
        for pic in as_list(content.get("pictures"))
        if isinstance(pic, dict) and str(pic.get("img_src") or "").startswith("http")
    ]
    return tuple(urls[:MAX_COMMENT_PICS])


def _reply_texts(raw: dict[str, Any], blocked: Iterable[str]) -> tuple[str, ...]:
    texts: list[str] = []
    for sub in as_list(raw.get("replies")):
        message = clean_text(as_dict(as_dict(sub).get("content")).get("message"), REPLY_CHARS)
        if message and not is_blocked(message, blocked):
            texts.append(message)
        if len(texts) >= MAX_REPLIES:
            break
    return tuple(texts)


def _comment(raw: dict[str, Any], ref: str, blocked: Iterable[str]) -> SourceComment | None:
    content = as_dict(raw.get("content"))
    text    = clean_text(content.get("message"), COMMENT_CHARS)
    # 纯表情或只有图片的评论也可能是梗图，保留图片但文本为空时仍要有图。
    pictures = _picture_urls(content)
    if is_blocked(text, blocked) or not (text or pictures):
        return None
    return SourceComment(
        ref      = ref,
        text     = text,
        likes    = to_int(raw.get("like")),
        pictures = pictures,
        replies  = _reply_texts(raw, blocked),
    )


async def hot_comments(
    fetch: Fetcher,
    item: SourceItem,
    item_index: int,
    blocked: Iterable[str],
    limit: int,
) -> tuple[SourceComment, ...]:
    """热评按序号编成 ``c<条目序号>.<评论序号>``，消化提示里用它引用。"""

    if limit <= 0:
        return ()
    payload = require_ok(
        "bili_comments",
        await fetch.get_json(
            f"{API}/x/v2/reply/main",
            params  = {"type": "1", "oid": aid_of(item), "mode": "3", "ps": "10"},
            referer = REFERER,
        ),
    )
    rows                          = as_list(as_dict(payload.get("data")).get("replies"))
    comments: list[SourceComment] = []
    used                          = 0  # 评论与楼中楼合计不超过 limit
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        comment = _comment(raw, f"c{item_index}.{len(comments) + 1}", blocked)
        if comment is None:
            continue
        room = limit - used - 1
        if room < 0:
            break
        if len(comment.replies) > room:
            comment = replace(comment, replies=comment.replies[:room])
        comments.append(comment)
        used += 1 + len(comment.replies)
    return tuple(comments)


__all__ = ["SourceError", "aid_of", "hot_comments", "popular", "ranking", "search"]
