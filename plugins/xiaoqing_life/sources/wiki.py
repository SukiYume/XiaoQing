"""中文维基阅读：按查询取最相关的 ≤2 个页面的介绍段。

维基原文可能是繁体，摘录照抄原字（核对时只做全半角与标点归一，不转繁简）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..safety import clean_text, is_blocked, overlap
from .base import (
    TEXT_CHARS,
    Fetcher,
    SourceError,
    SourceItem,
    as_dict,
    require_ok,
    segment_text,
    to_int,
)

_API           = "https://zh.wikipedia.org/w/api.php"
MAX_PAGES      = 2
INTRO_CHARS    = 600
INTRO_SEGMENTS = 3


def _page_item(page: dict[str, Any], blocked: tuple[str, ...]) -> SourceItem | None:
    title  = clean_text(page.get("title"), 40)
    pageid = page.get("pageid")
    if not title or not isinstance(pageid, int) or isinstance(pageid, bool):
        return None
    # 任何一部分命中屏蔽词就整页不读（不是只删掉那一句）。
    if is_blocked(f"{title} {clean_text(page.get('extract'), 4000)}", blocked):
        return None
    passages = segment_text(
        page.get("extract"), "wiki", blocked, max_total=INTRO_CHARS, max_segments=INTRO_SEGMENTS
    )
    if not passages:
        return None
    return SourceItem(
        item_id  = f"wiki_read:{pageid}",
        source   = "wiki_read",
        title    = title,
        text     = passages[0].text[:TEXT_CHARS],
        url      = f"https://zh.wikipedia.org/?curid={pageid}",
        heat     = 0,
        tags     = (),
        passages = tuple(passages),
    )


async def read(fetch: Fetcher, query: str, blocked: Iterable[str]) -> list[SourceItem]:
    """搜索 → 介绍段；按标题与查询的字符二元组重合排序，取 ≤2 个非空页面。"""

    blocked = tuple(blocked)
    payload = require_ok(
        "wiki_read",
        await fetch.get_json(
            _API,
            params={
                "action"     : "query",
                "generator"  : "search",
                "gsrsearch"  : query,
                "gsrlimit"   : "3",
                "prop"       : "extracts",
                "explaintext": "1",
                "exintro"    : "1",
                "exlimit"    : "3",
                "format"     : "json",
            },
        ),
        key="",
    )
    pages = as_dict(as_dict(payload.get("query")).get("pages"))
    rows  = [as_dict(p) for p in pages.values()]
    rows.sort(key=lambda p: (-overlap(str(p.get("title") or ""), query), to_int(p.get("index"))))
    items: list[SourceItem] = []
    for page in rows:
        item = _page_item(page, blocked)
        if item is not None:
            items.append(item)
        if len(items) >= MAX_PAGES:
            break
    return items


__all__ = ["SourceError", "read"]
