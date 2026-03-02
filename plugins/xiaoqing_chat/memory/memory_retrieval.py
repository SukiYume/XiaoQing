from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..config.config import MemoryConfig
from ..llm.llm_client import LLMError, chat_completions_raw_with_fallback_paths
from .memory import StoredMessage
from .memory_db import MemoryDB, RetrievedItem
from ..llm.prompt_builder import ChatMessage, build_dialogue_prompt
from .thinking_back import append_record, get_cached_answer

_logger = logging.getLogger("plugin.xiaoqing_chat")

@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

ToolFunc = Callable[[dict[str, Any]], dict[str, Any]]

def _tools_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "query_chat_history",
                "description": "在最近聊天记录里按语义/关键词找相关片段。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                        "user_id": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_topic_summaries",
                "description": "在话题摘要（长期记忆）里检索相关信息。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_person_info",
                "description": "检索人物信息/事实记忆（例如某人喜欢什么、某个约定）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer"},
                        "subject_id": {"type": "integer"}
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_person_profile",
                "description": "获取某个人的画像摘要（按 subject_id）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subject_id": {"type": "integer"}
                    },
                    "required": ["subject_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_words",
                "description": "查询黑话/缩写/词语解释。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_knowledge",
                "description": "查询本地知识库片段（如果有配置）。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "found_answer",
                "description": "当你已能回答问题时调用，给出最终答案。",
                "parameters": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "not_enough_info",
                "description": "信息不足时调用，说明原因。",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                },
            },
        },
    ]

_QUESTION_SYSTEM = (
    "你是聊天记忆检索问题生成器。你只输出 JSON，不要解释。\n"
    "你会从当前对话中提炼一个最关键的问题，用于检索长期记忆。\n"
    "输出：{\"question\":\"...\"}\n"
)

def build_question_messages(
    *,
    bot_name: str,
    history: Sequence[StoredMessage],
    current_text: str,
) -> list[ChatMessage]:
    dialogue = build_dialogue_prompt(history, bot_name=bot_name, truncate=True, max_chars=1200)
    user = (
        "对话如下（你是“{bot}(你)”）：\n"
        "{dialogue}\n\n"
        "当前一句话：{text}\n"
        "输出 JSON：{{\"question\":\"...\"}}"
    ).format(bot=bot_name, dialogue=dialogue, text=current_text.strip())
    return [
        ChatMessage(role="system", content=_QUESTION_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]

def _parse_question_json(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    try:
        obj = json.loads(s[start : end + 1])
    except Exception:
        return ""
    q = obj.get("question", "")
    return str(q).strip() if isinstance(q, str) else ""

_REACT_SYSTEM = (
    "你是记忆检索代理。你可以调用工具查询信息。\n"
    "目标：回答“问题”。如果信息不足，就说信息不足。\n"
    "你必须通过工具来获得信息，不要凭空编造。\n"
)

def build_react_messages(*, question: str) -> list[dict[str, Any]]:
    user = f"问题：{question.strip()}\n请通过工具检索后再回答。"
    return [
        {"role": "system", "content": _REACT_SYSTEM.strip()},
        {"role": "user", "content": user.strip()},
    ]

def _extract_tool_calls(resp: dict[str, Any]) -> list[ToolCall]:
    choices = resp.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return []
    msg = (choices[0] or {}).get("message") or {}
    if not isinstance(msg, dict):
        return []
    tool_calls = msg.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return []
    out: list[ToolCall] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id", "")).strip() or str(len(out))
        func = call.get("function") or {}
        if not isinstance(func, dict):
            continue
        name = str(func.get("name", "")).strip()
        arg_text = func.get("arguments", "{}")
        args: dict[str, Any] = {}
        if isinstance(arg_text, str):
            try:
                parsed = json.loads(arg_text)
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                args = {}
        elif isinstance(arg_text, dict):
            args = arg_text
        if name:
            out.append(ToolCall(call_id=call_id, name=name, arguments=args))
    return out

def _tool_query_chat_history(history: Sequence[StoredMessage], args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    limit = int(args.get("limit", 6) or 6)
    user_id_filter = args.get("user_id", None)
    try:
        user_id_filter = int(user_id_filter) if user_id_filter is not None else None
    except (TypeError, ValueError):
        user_id_filter = None
    if limit < 1:
        limit = 1
    if limit > 10:
        limit = 10
    if not query:
        return {"snippets": []}
    out: list[str] = []
    q = query.lower()
    for msg in reversed(history[-120:]):
        if user_id_filter is not None and msg.user_id != user_id_filter:
            continue
        text = (msg.content or "").strip()
        if not text:
            continue
        if q in text.lower():
            lid = getattr(msg, "local_id", "") or ""
            prefix = f"{lid} " if lid else ""
            out.append(f"{prefix}{msg.role}:{msg.name}<{msg.user_id}>:{text}")
        if len(out) >= limit:
            break
    return {"snippets": out}

def _tool_query_db(db: MemoryDB, args: dict[str, Any], *, type_filter: str, chat_id: str) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    top_k = int(args.get("top_k", 5) or 5)
    subject_id_raw = args.get("subject_id", None)
    subject_id = None
    if subject_id_raw is not None:
        try:
            subject_id = int(subject_id_raw)
        except (TypeError, ValueError):
            subject_id = None
    if top_k < 1:
        top_k = 1
    if top_k > 10:
        top_k = 10
    if not query:
        return {"items": []}
    meta_filter = None
    scoped_chat_id = (chat_id or "").strip()
    if scoped_chat_id and type_filter in ("topic_summary", "person_info", "person_profile"):
        meta_filter = {"chat_id": scoped_chat_id}
    if subject_id is not None and type_filter in ("person_info", "person_profile"):
        meta_filter = {**(meta_filter or {}), "subject_id": subject_id}
    items = db.query(query, top_k=top_k, min_score=0.0, type_filter=type_filter, meta_filter=meta_filter)
    return {"items": [{"doc_id": it.doc_id, "score": it.score, "text": it.text, "meta": it.meta} for it in items]}

def _tool_get_person_profile(db: MemoryDB, args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
    subject_id_raw = args.get("subject_id", None)
    try:
        subject_id = int(subject_id_raw)
    except (TypeError, ValueError):
        subject_id = 0
    if subject_id <= 0:
        return {"items": []}
    scoped_chat_id = (chat_id or "").strip()
    if not scoped_chat_id:
        return {"items": []}
    item = db.get(f"profile:{scoped_chat_id}:{subject_id}")
    if not item:
        return {"items": []}
    return {"items": [{"doc_id": item.doc_id, "score": 1.0, "text": item.text, "meta": item.meta}]}

async def react_retrieve(
    *,
    http_session,
    secrets: dict[str, Any],
    cfg: MemoryConfig,
    history: Sequence[StoredMessage],
    chat_id: str,
    question: str,
    memory_db: MemoryDB,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> str:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return ""

    tool_impl: dict[str, ToolFunc] = {
        "query_chat_history": lambda a: _tool_query_chat_history(history, a),
        "query_topic_summaries": lambda a: _tool_query_db(memory_db, a, type_filter="topic_summary", chat_id=chat_id),
        "query_person_info": lambda a: _tool_query_db(memory_db, a, type_filter="person_info", chat_id=chat_id),
        "query_words": lambda a: _tool_query_db(memory_db, a, type_filter="word_def", chat_id=""),
        "query_knowledge": lambda a: _tool_query_db(memory_db, a, type_filter="knowledge", chat_id=""),
        "query_person_profile": lambda a: _tool_get_person_profile(memory_db, a, chat_id=chat_id),
        "found_answer": lambda a: {"final": str(a.get("answer", "")).strip()},
        "not_enough_info": lambda a: {"final": ""},
    }

    messages: list[dict[str, Any]] = build_react_messages(question=question)
    start = time.time()
    api_call_count = 0

    for _ in range(max(1, int(cfg.max_agent_iterations))):
        if time.time() - start > float(cfg.agent_timeout_seconds):
            break
        api_call_count += 1
        resp, _path = await chat_completions_raw_with_fallback_paths(
            session=http_session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=min(0.4, temperature),
            top_p=top_p,
            max_tokens=min(768, max_tokens),
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=endpoint_path,
            tools=_tools_schema(),
            tool_choice="auto",
        )
        tool_calls = _extract_tool_calls(resp)
        if not tool_calls:
            content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
            return str(content).strip()

        assistant_msg = (resp.get("choices") or [{}])[0].get("message") or {}
        messages.append(
            {
                "role": "assistant",
                "content": assistant_msg.get("content") or "",
                "tool_calls": assistant_msg.get("tool_calls") or [],
            }
        )

        final = ""
        for call in tool_calls:
            fn = tool_impl.get(call.name)
            result: dict[str, Any]
            if not fn:
                result = {"error": f"unknown_tool:{call.name}"}
            else:
                result = fn(call.arguments)
                if "final" in result and isinstance(result.get("final"), str):
                    final = result.get("final", "")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        if final:
            _logger.info("memory_retrieval agent api_calls=%d elapsed=%.2fs", api_call_count, time.time() - start)
            return final

        await asyncio.sleep(0)

    _logger.info("memory_retrieval agent api_calls=%d elapsed=%.2fs (exhausted)", api_call_count, time.time() - start)
    return ""

async def build_memory_block(
    *,
    data_dir: Path,
    chat_id: str,
    http_session,
    secrets: dict[str, Any],
    cfg: MemoryConfig,
    bot_name: str,
    history: Sequence[StoredMessage],
    current_text: str,
    planner_question: str,
    memory_db: MemoryDB,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> str:
    if not cfg.enable_memory_retrieval:
        return ""
    soft_budget = 4.0
    question = planner_question.strip()
    if not question and cfg.planner_question:
        msgs = build_question_messages(bot_name=bot_name, history=history, current_text=current_text)
        payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
        try:
            raw, _path = await asyncio.wait_for(
                chat_completions_raw_with_fallback_paths(
                    session=http_session,
                    api_base=secrets.get("api_base", ""),
                    api_key=secrets.get("api_key", ""),
                    model=secrets.get("model", ""),
                    messages=payload_msgs,
                    temperature=min(0.5, temperature),
                    top_p=top_p,
                    max_tokens=min(256, max_tokens),
                    timeout_seconds=min(3.0, float(timeout_seconds)),
                    max_retry=0,
                    retry_interval_seconds=0.2,
                    proxy=proxy,
                    endpoint_path=endpoint_path,
                ),
                timeout=min(2.0, soft_budget),
            )
            content = (((raw.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
            question = _parse_question_json(str(content))
        except Exception:
            question = ""

    if not question:
        return ""

    if cfg.enable_thinking_back_cache:
        cached = get_cached_answer(
            data_dir=data_dir,
            chat_id=chat_id,
            question=question,
            window_seconds=float(cfg.thinking_back_window_seconds or 0.0),
        )
        if cached:
            return f"你回忆起了以下信息：\n{cached}\n"

    answer = ""
    try:
        answer = await asyncio.wait_for(
            react_retrieve(
                http_session=http_session,
                secrets=secrets,
                cfg=cfg,
                history=history,
                chat_id=chat_id,
                question=question,
                memory_db=memory_db,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                timeout_seconds=min(4.0, float(timeout_seconds)),
                max_retry=0,
                retry_interval_seconds=0.2,
                proxy=proxy,
                endpoint_path=endpoint_path,
            ),
            timeout=float(soft_budget),
        )
    except Exception:
        answer = ""
    answer = (answer or "").strip()
    if not answer:
        items = memory_db.query(
            question,
            top_k=max(6, int(cfg.top_k) * 4),
            min_score=cfg.min_score,
            type_filter=None,
            meta_filter={"chat_id": chat_id},
        )
        if not items:
            return ""
        lines = []
        for it in items:
            lines.append(f"- {it.text.strip()}")
        answer = "\n".join(lines).strip()

    if not answer:
        return ""
    if cfg.enable_thinking_back_cache:
        append_record(
            data_dir=data_dir,
            chat_id=chat_id,
            question=question,
            answer=answer,
            max_entries=int(cfg.thinking_back_max_entries or 200),
        )
    return f"你回忆起了以下信息：\n{answer}\n"
