"""查资料：百科卡片 + 中文维基。研究（``lookup`` 行动）和整理作品（核实待办）共用这一处。

- 参数是一到三个关键词，按整词截（``keywords``），不把最后一个词切成半截；
- 百科卡片只认一个词条名，用最具体的那个词（``key_word``，最长的）；
- 维基先按全部关键词搜，几个词同时出现的页面没有时，再只搜最具体的那个；
- 7 天内读过的条目（``seen``）不再读。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..safety import clean_text
from . import baike, deep, wiki
from .base import Fetcher, SourceError, SourceItem

logger = logging.getLogger(__name__)


def keywords(value: Any, limit: int) -> str:
    """按整个关键词截到 ``limit`` 字；只有一个词也超长时才按字数截。

    真实回放里模型写了“不锈钢锅 水滴 预热 莱顿弗罗斯特效应”，按字数截成了“……莱顿弗罗斯”，
    最关键的那个词反而没了。
    """

    words           = clean_text(value, 200).split()
    kept: list[str] = []
    for word in words:
        if len(" ".join([*kept, word])) > limit:
            break
        kept.append(word)
    if not kept and words:
        return words[0][:limit]
    return " ".join(kept)


def key_word(concept: str) -> str:
    """几个关键词里最具体的那个（最长的，一样长取前面的）。"""

    words = concept.split()
    return max(words, key=len) if words else ""


@dataclass(frozen=True, slots=True)
class Found:
    items : list[SourceItem]
    errors: int
    seen  : bool  # 查到了，但都是 7 天内读过的

    @property
    def reason(self) -> str:
        """取不到材料时的原因（与浏览一致）：all_seen / all_sources_failed / nothing_found。"""

        if self.items:
            return ""
        if self.seen:
            return "all_seen"
        return "all_sources_failed" if self.errors else "nothing_found"


async def read(
    fetch: Fetcher,
    concept: str,
    blocked: Iterable[str],
    *,
    baike_on: bool,
    wiki_on: bool,
    seen: Mapping[str, float],
    limit: int,
) -> Found:
    blocked                 = tuple(blocked)
    items: list[SourceItem] = []
    errors                  = 0
    key                     = key_word(concept)
    if baike_on and key:
        try:
            entry = await baike.read(fetch, key, blocked)
            if entry is not None:
                items.append(entry)
        except SourceError as exc:
            errors += 1
            logger.info("xiaoqing_life step=source name=baike failed reason=%s", exc.reason)
    if wiki_on and concept.strip():
        try:
            pages = await wiki.read(fetch, concept, blocked)
            if not pages and key and key != concept:
                pages = await wiki.read(fetch, key, blocked)
            items += pages
        except SourceError as exc:
            errors += 1
            logger.info("xiaoqing_life step=source name=wiki failed reason=%s", exc.reason)
    fresh = [i for i in items if i.item_id not in seen][:limit]
    return Found(
        items=[
            replace(i, passages=deep.assign_refs(i.passages, n)) for n, i in enumerate(fresh, 1)
        ],
        errors = errors,
        seen   = bool(items) and not fresh,
    )


__all__ = ["Found", "key_word", "keywords", "read"]
