"""学到的东西靠不靠得住（学 dafeiyu 的知识门控）。

- 一个来源的说法只是“看到有人这么说”（``hypothesis``）：聊天时她会说明还没核实，睡前整理会给它开一个
  核实用的小课题，去找另一个来源对一下；
- 两个互相独立的来源（不同 UP 主，没有 UP 主时按不同条目算），或者百科 / 维基里对得上，才算查实
  （``verified``）；
- 造谣、营销、标题党的材料不能当要点的依据（确定性规则，不调模型）。

这里只做判断，不写存储；调用方决定把结果记在哪。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

VERIFIED          = "verified"
HYPOTHESIS        = "hypothesis"
REFERENCE_SOURCES = frozenset({"wiki_read", "baike_read"})
UNVERIFIED_NOTE   = "（只在一个地方看到这么说，还没找别的来源核实）"

# 营销引流、诈骗、标题党和“转发保平安”式的谣言写法：命中的材料不能当要点的依据。
_RUMOR_RE = re.compile(
    r"加(?:微|v|V|薇)信|私信领(?:取|福利)|点击下方链接|橱窗同款|领券立减|限时秒杀|"
    r"包治百病|震惊[！!]|99%的人(?:都)?不知道|不转不是中国人|转发保平安|内部(?:绝密)?消息|"
    r"稳赚不赔|日入过万|免费领(?:取)?皮肤|刷单返利|速看.{0,4}(?:马上|即将)删|看完.{0,4}(?:马上|就)删"
)


def is_rumor(text: str) -> bool:
    return bool(_RUMOR_RE.search(text or ""))


def source_key(episode: Mapping[str, Any]) -> str:
    """一条经历算哪个“来源”：百科 / 维基按条目，B站按 UP 主（同一个 UP 主的几个视频算一个来源）。"""

    item   = str(episode.get("item_id") or "")
    source = str(episode.get("source") or "")
    if source in REFERENCE_SOURCES:
        return f"ref:{item}"
    uploader = str(episode.get("uploader") or "").strip().casefold()
    return f"up:{uploader}" if uploader else f"item:{item}"


def assess(episodes: Iterable[Mapping[str, Any]]) -> tuple[str, int]:
    """（可信度, 独立来源数）。有百科 / 维基，或独立来源 ≥2 才算查实。"""

    keys = {source_key(e) for e in episodes if e.get("item_id")}
    if any(k.startswith("ref:") for k in keys) or len(keys) >= 2:
        return VERIFIED, len(keys)
    return HYPOTHESIS, len(keys)


def note_episodes(
    thread: Mapping[str, Any], episodes: Iterable[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """小课题还有效的笔记（没被取代的）所引的经历。"""

    wanted = {
        str(n.get("episode_id"))
        for n in thread.get("notes", [])
        if n.get("episode_id") and not n.get("superseded_by")
    }
    return [e for e in episodes if e.get("id") in wanted]


def hedge(text: str, confidence: str) -> str:
    """交给聊天的那句：没核实的说法带上“还没核实”。"""

    return f"{text}{UNVERIFIED_NOTE}" if text and confidence == HYPOTHESIS else text
