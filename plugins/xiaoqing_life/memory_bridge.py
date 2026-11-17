"""把她在网上的经历、睡前日记和自己查明白的事发布到统一记忆（xiaoqing_memory）。

每次定时检查时，把游标之后新增的条目用稳定 ID 写进 ``memory.record``（第一次运行时全量发布一次）。
统一记忆把它们记在 ``global``、来源记为网页文字（日记是整理产出），夜间整理时写进她的自我页；
来源链里有网页文字的内容不会交给有命令执行能力的 agent。聊天每轮取用网上生活仍走 ``life.turn_context``。

统一记忆不在、超时或出错时什么也不做，游标不前移，下次再发。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.log import LogEvent

from .config import LifeConfig
from .stores import LifeStore
from .turn_context import diary_key

log = logging.getLogger(__name__)

CURSOR_KEY     = "memory_cursor"
_CHUNK         = 40
_TIMEOUT       = 5.0
_DIARY_KEEP    = 56
_SOURCE_LABELS = {
    "bilibili": "B站",
    "bili"    : "B站",
    "zhihu"   : "知乎",
    "weibo"   : "微博",
    "wiki"    : "维基",
    "baike"   : "百科",
}


def _service(context: Any) -> Any:
    capabilities = getattr(context, "capabilities", None)
    return getattr(capabilities, "memory", None) if capabilities is not None else None


def _source_label(source: str) -> str:
    for key, label in _SOURCE_LABELS.items():
        if source.startswith(key):
            return label
    return ""


def episode_op(episode: dict[str, Any], cfg: LifeConfig) -> dict[str, Any] | None:
    title    = str(episode.get("title") or "").strip()
    reaction = str(episode.get("reaction") or "").strip()
    if not episode.get("id") or not (title or reaction):
        return None
    where = _source_label(str(episode.get("source") or ""))
    text  = f"在{where}看了「{title}」" if where else f"看了「{title}」"
    if reaction:
        text += f"：{reaction}"
    return {
        "op"         : "add",
        "kind"       : "episode",
        "key"        : str(episode["id"]),
        "text"       : text[:120],
        "observed_at": float(episode.get("at") or 0.0),
        "keep_days"  : cfg.episode_keep_days,
        "data"       : {
            "title"  : title[:60],
            "feeling": str(episode.get("feeling") or ""),
            "topics" : [str(topic)[:16] for topic in episode.get("topics") or ()][:6],
            "thread" : str(episode.get("thread_id") or ""),
        },
    }


def diary_op(entry: dict[str, Any]) -> dict[str, Any] | None:
    text = str(entry.get("text") or "").strip()
    day  = str(entry.get("day") or "")
    if not text or not day:
        return None
    return {
        "op"         : "add",
        "kind"       : "self.diary",
        "key"        : diary_key(entry),
        "text"       : text[:300],
        "observed_at": float(entry.get("at") or 0.0),
        "keep_days"  : _DIARY_KEEP,
        "data"       : {
            "day"     : day,
            "episodes": [str(item) for item in entry.get("episode_ids") or ()][:8],
        },
    }


def learning_op(row: dict[str, Any], cfg: LifeConfig) -> dict[str, Any] | None:
    text = str(row.get("text") or "").strip()
    if not row.get("id") or not text:
        return None
    topic = str(row.get("topic") or "")
    return {
        "op"         : "add",
        "kind"       : "world.learning",
        "key"        : str(row["id"]),
        "text"       : (f"关于{topic}：{text}" if topic else text)[:160],
        "observed_at": float(row.get("created_at") or 0.0),
        "keep_days"  : cfg.learning_keep_days,
        "data"       : {
            "topic"   : topic,
            "verified": str(row.get("confidence") or "") not in ("", "hypothesis"),
            "sources" : int(row.get("sources") or 0),
        },
    }


def pending_ops(store: LifeStore, cfg: LifeConfig, cursor: float) -> list[dict[str, Any]]:
    ops: list[dict[str, Any] | None] = []
    ops += [episode_op(item, cfg) for item in store.episodes if float(item.get("at") or 0) > cursor]
    ops += [
        diary_op(item)
        for item in store.journal
        if item.get("kind") == "diary" and float(item.get("at") or 0) > cursor
    ]
    ops += [
        learning_op(row, cfg)
        for row in store.learnings
        if float(row.get("created_at") or 0) > cursor
    ]
    return [op for op in ops if op is not None]


async def publish(
    context: Any, store: LifeStore, cfg: LifeConfig, now: float, *, settled: bool = True
) -> int:
    """发布游标之后的新条目；全部写成功才把游标移到 ``now``。返回发布的条数。

    ``settled`` 为假表示还有一次上网或睡前整理在跑：它稍后写下的经历、日记和查明白的事带的是它
    开始时的时间，可能不晚于现在，游标这时前移，这些条目就再也发不出去。所以这时只发已经写下的，
    游标不动；下一次照常再发一遍，记忆按稳定 ID 更新同一条。
    """

    service = _service(context)
    if service is None:
        return 0
    cursor = float(store.state.get(CURSOR_KEY) or 0.0)
    ops    = pending_ops(store, cfg, cursor)
    for start in range(0, len(ops), _CHUNK):
        try:
            result = await asyncio.wait_for(
                service.record({"scope": "global", "ops": ops[start : start + _CHUNK]}),
                timeout=_TIMEOUT,
            )
        except Exception as exc:
            log.warning(LogEvent("memory_publish.skipped", error=exc))
            return 0
        if not isinstance(result, dict) or result.get("error"):
            return 0
    if settled:
        await store.mutate(lambda s: s.state.__setitem__(CURSOR_KEY, now))
    if ops:
        log.info(LogEvent("memory_publish", items=len(ops)))
    return len(ops)
