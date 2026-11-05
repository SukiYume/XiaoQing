"""知乎热榜、微博热搜、百度热搜：只取标题和摘要，不读正文。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from .base import (
    Fetcher,
    SourceItem,
    as_dict,
    as_list,
    build_item,
    require_ok,
    to_int,
)

_WEIBO_AD_LABELS = frozenset({"商", "广告", "推广", "商业"})
_BAIDU_MAX_DEPTH = 4


async def zhihu(fetch: Fetcher, blocked: Iterable[str], limit: int = 20) -> list[SourceItem]:
    payload = require_ok(
        "zhihu_hot",
        await fetch.get_json(
            "https://api.zhihu.com/topstory/hot-lists/total",
            params  = {"limit": str(limit)},
            referer = "https://www.zhihu.com/hot",
        ),
        key="",
    )
    items: list[SourceItem] = []
    for row in as_list(payload.get("data")):
        row    = as_dict(row)
        target = as_dict(row.get("target"))
        item   = build_item(
            source    = "zhihu_hot",
            native_id = str(target.get("id") or ""),
            title     = target.get("title"),
            text      = target.get("excerpt"),
            url       = f"https://www.zhihu.com/question/{target.get('id')}",
            heat      = to_int(row.get("detail_text")),
            tags      = (),
            blocked   = blocked,
        )
        if item is not None:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def _weibo_is_ad(row: dict[str, Any]) -> bool:
    label = str(row.get("label_name") or row.get("icon_desc") or "")
    return label in _WEIBO_AD_LABELS or bool(row.get("is_ad") or row.get("ad_info"))


async def weibo(fetch: Fetcher, blocked: Iterable[str], limit: int = 20) -> list[SourceItem]:
    """只读 ``data.realtime``；``hotgov`` 置顶的时政条目整段不看。"""

    payload = require_ok(
        "weibo_hot",
        await fetch.get_json(
            "https://weibo.com/ajax/side/hotSearch",
            referer="https://weibo.com/",
        ),
        key="",
    )
    rows                    = as_list(as_dict(payload.get("data")).get("realtime"))
    items: list[SourceItem] = []
    for row in rows:
        row = as_dict(row)
        if _weibo_is_ad(row):
            continue
        word = row.get("word") or row.get("note")
        item = build_item(
            source    = "weibo_hot",
            native_id = str(word or ""),
            title     = word,
            text      = "",
            url       = f"https://s.weibo.com/weibo?q={quote(str(word or ''))}",
            heat      = to_int(row.get("num")),
            tags      = (row.get("label_name"),),
            blocked   = blocked,
        )
        if item is not None:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def _baidu_rows(node: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
    """百度热搜的卡片嵌套不固定：递归找出带 ``word`` 的条目。"""

    if depth > _BAIDU_MAX_DEPTH:
        return
    for child in as_list(node):
        child = as_dict(child)
        if "word" in child:
            yield child
        else:
            yield from _baidu_rows(child.get("content"), depth + 1)


async def baidu(fetch: Fetcher, blocked: Iterable[str], limit: int = 20) -> list[SourceItem]:
    payload = require_ok(
        "baidu_hot",
        await fetch.get_json(
            "https://top.baidu.com/api/board",
            params  = {"platform": "wise", "tab": "realtime"},
            referer = "https://top.baidu.com/board?tab=realtime",
        ),
        key="",
    )
    cards                   = as_list(as_dict(payload.get("data")).get("cards"))
    items: list[SourceItem] = []
    for row in _baidu_rows(cards):
        item = build_item(
            source    = "baidu_hot",
            native_id = str(row.get("word") or ""),
            title     = row.get("word"),
            text      = row.get("desc"),
            url       = f"https://www.baidu.com/s?wd={quote(str(row.get('word') or ''))}",
            heat      = to_int(row.get("hotScore")),
            tags      = (),
            blocked   = blocked,
        )
        if item is not None:
            items.append(item)
        if len(items) >= limit:
            break
    return items
