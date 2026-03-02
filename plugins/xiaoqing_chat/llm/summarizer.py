from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .llm_client import chat_completions
from ..memory.memory import StoredMessage
from ..memory.memory_db import MemoryDB
from .prompt_builder import ChatMessage, build_dialogue_prompt

@dataclass
class TopicSummary:
    topic_id: str
    topic: str
    keywords: list[str]
    summary: str
    key_points: list[str]
    updated_at: float

def _summarizer_path(data_dir: Path, chat_id: str) -> Path:
    return data_dir / "hippo_memorizer" / f"{chat_id}.json"

def _load_cache(data_dir: Path, chat_id: str) -> list[TopicSummary]:
    path = _summarizer_path(data_dir, chat_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        out: list[TopicSummary] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            topic_id = str(item.get("topic_id", "")).strip()
            topic = str(item.get("topic", "")).strip()
            summary = str(item.get("summary", "")).strip()
            keywords = item.get("keywords", [])
            key_points = item.get("key_points", [])
            updated_at = float(item.get("updated_at", 0.0) or 0.0)
            if not topic_id or not topic or not summary:
                continue
            if not isinstance(keywords, list):
                keywords = []
            if not isinstance(key_points, list):
                key_points = []
            out.append(
                TopicSummary(
                    topic_id=topic_id,
                    topic=topic,
                    keywords=[str(k).strip() for k in keywords if isinstance(k, str) and k.strip()],
                    summary=summary,
                    key_points=[str(k).strip() for k in key_points if isinstance(k, str) and k.strip()],
                    updated_at=updated_at,
                )
            )
        return out
    except Exception:
        return []

def _save_cache(data_dir: Path, chat_id: str, topics: Sequence[TopicSummary]) -> None:
    path = _summarizer_path(data_dir, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: list[dict[str, Any]] = []
    for t in topics:
        payload.append(
            {
                "topic_id": t.topic_id,
                "topic": t.topic,
                "keywords": t.keywords,
                "summary": t.summary,
                "key_points": t.key_points,
                "updated_at": t.updated_at,
            }
        )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

from ..planning.pfc_utils import extract_first_json_dict

_TOPIC_SYSTEM = (
    "你是聊天记录总结助手。你会把最近的对话压缩成一个话题摘要，用于机器人长期记忆检索。\n"
    "要求：只输出 JSON，不要输出解释文字。\n"
    "字段：topic（短标题），keywords（3-8 个关键词），summary（100-200 字），key_points（3-6 条要点）。\n"
)

def build_topic_messages(*, bot_name: str, history: Sequence[StoredMessage]) -> list[ChatMessage]:
    dialogue = build_dialogue_prompt(history, bot_name=bot_name, truncate=True, max_chars=1600)
    user = (
        "对话如下：\n"
        "{dialogue}\n\n"
        "输出 JSON：\n"
        '{{"topic":"...","keywords":["..."],"summary":"...","key_points":["..."]}}'
    ).format(dialogue=dialogue)
    return [
        ChatMessage(role="system", content=_TOPIC_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]

async def maybe_update_topic_summary(
    *,
    data_dir: Path,
    memory_db: MemoryDB,
    http_session,
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
    proxy: str,
    endpoint_path: str,
) -> None:
    if min_messages_per_update <= 0:
        return
    if len(history) < min_messages_per_update:
        return
    if len(history) % min_messages_per_update != 0:
        return

    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return

    msgs = build_topic_messages(bot_name=bot_name, history=history[-min(40, len(history)) :])
    payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
    out = await chat_completions(
        session=http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=payload_msgs,
        temperature=min(0.6, temperature),
        top_p=top_p,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        proxy=proxy,
        endpoint_path=endpoint_path,
    )
    obj = extract_first_json_dict(out)
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
    cache = _load_cache(data_dir, chat_id)
    cache.append(ts)
    if max_cache_topics > 0 and len(cache) > max_cache_topics:
        cache = cache[-max_cache_topics:]
    _save_cache(data_dir, chat_id, cache)

    memory_db.bind(data_dir)
    memory_db.upsert_text(
        doc_id=f"topic:{chat_id}:{topic_id}",
        text=f"话题：{topic}\n摘要：{summary}\n要点：\n- " + "\n- ".join(ts.key_points),
        meta={"type": "topic_summary", "chat_id": chat_id, "keywords": ts.keywords},
    )
