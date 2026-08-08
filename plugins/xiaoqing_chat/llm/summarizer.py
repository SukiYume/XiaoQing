"""把群聊历史压缩为可检索、可增量更新的话题摘要。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.plugin_base import write_json

from ..memory.memory import StoredMessage
from ..memory.memory_db import MemoryDB
from ..memory.topic_summary_cache import load_topic_summary_entries, topic_summary_cache_path
from ..utils.json_parsing import parse_first_json_object
from .llm_client import chat_completions
from .prompt_builder import ChatMessage, build_dialogue_prompt


@dataclass
class TopicSummary:
    """一条写入缓存和向量索引的话题摘要。"""

    topic_id: str
    topic: str
    keywords: list[str]
    summary: str
    key_points: list[str]
    updated_at: float


# ---------------------------------------------------------------------------
# 摘要缓存与索引写入
# ---------------------------------------------------------------------------


def _load_cache(data_dir: Path, chat_id: str) -> list[TopicSummary]:
    out: list[TopicSummary] = []
    for idx, item in enumerate(load_topic_summary_entries(data_dir, chat_id)):
        if not item.topic or not item.summary:
            continue
        topic_id = item.topic_id or f"legacy-{idx}-{int(item.updated_at or 0.0)}"
        out.append(
            TopicSummary(
                topic_id=topic_id,
                topic=item.topic,
                keywords=item.keywords,
                summary=item.summary,
                key_points=item.key_points,
                updated_at=item.updated_at,
            )
        )
    return out


def _save_cache(data_dir: Path, chat_id: str, topics: Sequence[TopicSummary]) -> None:
    path = topic_summary_cache_path(data_dir, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: list[dict[str, Any]] = [
        {
            "topic_id": topic.topic_id,
            "topic": topic.topic,
            "keywords": topic.keywords,
            "summary": topic.summary,
            "key_points": topic.key_points,
            "updated_at": topic.updated_at,
        }
        for topic in topics
    ]
    write_json(path, payload)


_TOPIC_SYSTEM = (
    "你是聊天记录总结器。把最近对话压缩成可供长期检索的话题摘要。\n"
    "摘要必须保留信息来源、说话人和不确定性，不把任何人的主张直接改写成已证实事实。\n"
    "假设、引用、转述、玩笑、反问、夸张、角色扮演和未经验证的推断，只能按其原本性质记录；"
    "不能升级成角色的真实经历、稳定属性或客观结论。\n"
    "只保留对后续理解有用且能从原对话直接支持的信息，不补写动机、关系或背景。\n"
    "要求：只输出 JSON，不要输出解释文字。\n"
    "字段：topic（短标题），keywords（3-8 个关键词），summary（100-200 字），key_points（3-6 条要点）。\n"
)


def build_topic_messages(*, bot_name: str, history: Sequence[StoredMessage]) -> list[ChatMessage]:
    dialogue = build_dialogue_prompt(history, bot_name=bot_name, truncate=True, max_chars=1600)
    user = (
        "对话如下：\n"
        f"{dialogue}\n\n"
        "输出 JSON：\n"
        '{"topic":"...","keywords":["..."],"summary":"...","key_points":["..."]}'
    )
    return [
        ChatMessage(role="system", content=_TOPIC_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]


def _persist_topic_summary(
    *,
    data_dir: Path,
    chat_id: str,
    cache: list[TopicSummary],
    memory_db: MemoryDB,
    summary: TopicSummary,
) -> None:
    _save_cache(data_dir, chat_id, cache)
    memory_db.bind(data_dir)
    memory_db.upsert_text(
        doc_id=f"topic:{chat_id}:{summary.topic_id}",
        text=(
            f"话题：{summary.topic}\n摘要：{summary.summary}\n要点：\n- "
            + "\n- ".join(summary.key_points)
        ),
        meta={
            "type": "topic_summary",
            "chat_id": chat_id,
            "keywords": summary.keywords,
        },
    )


async def maybe_update_topic_summary(
    *,
    data_dir: Path,
    memory_db: MemoryDB,
    secrets: dict[str, Any],
    bot_name: str,
    chat_id: str,
    history: Sequence[StoredMessage],
    min_messages_per_update: int,
    max_cache_topics: int,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
) -> None:
    """达到增量阈值时生成一次摘要，并原子更新缓存和检索索引。"""

    if min_messages_per_update <= 0:
        return
    if len(history) < min_messages_per_update:
        return
    cache = await asyncio.to_thread(_load_cache, data_dir, chat_id)
    if cache:
        last_updated = max((float(t.updated_at or 0.0) for t in cache), default=0.0)
        observed_since_last = sum(
            1 for msg in history if float(getattr(msg, "ts", 0.0) or 0.0) > last_updated
        )
        if observed_since_last < min_messages_per_update:
            return

    if "_ai" in secrets and secrets.get("_ai") is None:
        return

    msgs = build_topic_messages(bot_name=bot_name, history=history[-min(40, len(history)) :])
    payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
    out = await chat_completions(
        secrets=secrets,
        messages=payload_msgs,
        temperature=min(0.6, temperature),
        top_p=top_p,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
    )
    obj = parse_first_json_object(out)
    if not obj:
        return
    topic = str(obj.get("topic", "")).strip()
    summary = str(obj.get("summary", "")).strip()
    if not topic or not summary:
        return
    keywords = obj.get("keywords", [])
    if not isinstance(keywords, list):
        keywords = []
    key_points = obj.get("key_points", [])
    if not isinstance(key_points, list):
        key_points = []

    now = time.time()
    topic_id = f"{int(now)}"
    ts = TopicSummary(
        topic_id=topic_id,
        topic=topic,
        keywords=[str(k).strip() for k in keywords if isinstance(k, str) and k.strip()],
        summary=summary,
        key_points=[str(k).strip() for k in key_points if isinstance(k, str) and k.strip()],
        updated_at=now,
    )
    cache.append(ts)
    if max_cache_topics > 0 and len(cache) > max_cache_topics:
        cache = cache[-max_cache_topics:]
    await asyncio.to_thread(
        _persist_topic_summary,
        data_dir=data_dir,
        chat_id=chat_id,
        cache=cache,
        memory_db=memory_db,
        summary=ts,
    )
