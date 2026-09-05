"""为当前会话构造有来源、受范围约束的记忆上下文。

会话摘要、人物事实和画像只能查询当前 ``chat_id``；知识与词义只有显式批准为全局
类型后才能跨会话查询。ReAct 代理必须先获得非空工具证据才可提交答案，模型裸答、坏参数
或未知工具都不能扩大检索范围。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config.config import MemoryConfig
from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..llm.prompt_builder import ChatMessage, build_dialogue_prompt
from ..message_parts import render_stored_message
from ..utils.json_parsing import parse_first_json_object
from .memory import StoredMessage
from .memory_db import MemoryDB, RetrievedItem
from .thinking_back import append_record, get_cached_answer

_logger                   = logging.getLogger("plugin.xiaoqing_chat")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_META_KEYS           = frozenset(
    {
        "path",
        "source_path",
        "file_path",
        "cached_path",
        "directory",
        "data_dir",
        "plugin_dir",
    }
)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


ToolFunc = Callable[[dict[str, Any]], dict[str, Any]]


def _looks_like_absolute_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return (
        Path(text).is_absolute()
        or bool(_WINDOWS_ABSOLUTE_PATH_RE.match(text))
        or text.startswith(("\\\\", "file://"))
    )


def _opaque_local_reference(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"local-ref:{digest}"


def _public_memory_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """移除仅供本机定位的字段，并封装遗留元数据中的绝对路径。"""

    def sanitize(value: Any) -> Any:
        if isinstance(value, str) and _looks_like_absolute_path(value):
            return _opaque_local_reference(value)
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [sanitize(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): sanitize(item)
                for key, item in value.items()
                if str(key).casefold() not in _PATH_META_KEYS
                and not str(key).casefold().endswith(("_path", "_dir"))
            }
        return value

    sanitized = sanitize(dict(meta))
    return sanitized if isinstance(sanitized, dict) else {}


def _public_retrieved_item(item: RetrievedItem) -> dict[str, Any]:
    doc_id = str(item.doc_id)
    if _looks_like_absolute_path(doc_id):
        doc_id = _opaque_local_reference(doc_id)
    return {
        "doc_id": doc_id,
        "score": item.score,
        "text": item.text,
        "meta": _public_memory_meta(item.meta),
    }


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
                        "user_id": {"type": "integer", "minimum": 1},
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
                "description": "检索与指定人物直接相关的事实记忆和对话约定。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                        "subject_id": {"type": "integer", "minimum": 1},
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
                    "properties": {"subject_id": {"type": "integer", "minimum": 1}},
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
                "name": "query_knowledge",
                "description": "查询本地知识库片段（如果有配置）。",
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
    "从当前对话中提炼一个最关键、可检索的问题。保留相关人物和对象，不预设答案，"
    "也不把玩笑、引用或不确定说法改写成事实。\n"
    '输出：{"question":"..."}\n'
)


def build_question_messages(
    *,
    bot_name: str,
    history: Sequence[StoredMessage],
    current_text: str,
) -> list[ChatMessage]:
    dialogue = build_dialogue_prompt(history, bot_name=bot_name, truncate=True, max_chars=1200)
    user = (
        f"对话如下（你是“{bot_name}(你)”）：\n"
        f"{dialogue}\n\n"
        f"当前一句话：{current_text.strip()}\n"
        '输出 JSON：{"question":"..."}'
    )
    return [
        ChatMessage(role="system", content=_QUESTION_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]


def _parse_question_json(text: str) -> str:
    obj = parse_first_json_object(text)
    if not obj:
        return ""
    q = obj.get("question", "")
    return str(q).strip() if isinstance(q, str) else ""


_REACT_SYSTEM = (
    "你是记忆检索代理。你可以调用工具查询信息。\n"
    "只根据本轮工具返回的证据回答问题，并保留证据中的说话人、范围和不确定性。\n"
    "没有直接相关的非空证据就报告信息不足，不用常识、相近主题或推断补全人物信息。\n"
)


def build_react_messages(*, question: str) -> list[dict[str, Any]]:
    user = f"问题：{question.strip()}\n请通过工具检索后再回答。"
    return [
        {"role": "system", "content": _REACT_SYSTEM.strip()},
        {"role": "user", "content": user.strip()},
    ]


def _extract_tool_calls(resp: dict[str, Any]) -> list[ToolCall]:
    """只接收结构完整的调用，并为缺失/重复 ID 生成稳定的唯一标识。"""

    choices = resp.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return []
    msg = (choices[0] or {}).get("message") or {}
    if not isinstance(msg, dict):
        return []
    tool_calls = msg.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return []
    out: list[ToolCall]     = []
    used_call_ids: set[str] = set()
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        func = call.get("function") or {}
        if not isinstance(func, dict):
            continue
        name                 = str(func.get("name", "")).strip()
        arg_text             = func.get("arguments", "{}")
        args: dict[str, Any] = {}
        if isinstance(arg_text, str):
            try:
                parsed = json.loads(arg_text)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                args = {}
        elif isinstance(arg_text, dict):
            args = arg_text
        if name:
            call_id = str(call.get("id", "")).strip()
            if not call_id or call_id in used_call_ids:
                call_id = f"memory_call_{index}"
                suffix  = 1
                while call_id in used_call_ids:
                    call_id = f"memory_call_{index}_{suffix}"
                    suffix += 1
            used_call_ids.add(call_id)
            out.append(ToolCall(call_id=call_id, name=name, arguments=args))
    return out


def _bounded_tool_int(
    args: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int = 10,
) -> int:
    raw_value = args.get(name, default)
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        value = int(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return min(maximum, max(minimum, value))


def _optional_positive_tool_int(args: dict[str, Any], name: str) -> int | None:
    raw_value = args.get(name)
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        value = int(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _tool_query_chat_history(
    history: Sequence[StoredMessage], args: dict[str, Any]
) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    limit = _bounded_tool_int(args, "limit", default=6)
    user_id_filter = _optional_positive_tool_int(args, "user_id")
    if not query:
        return {"snippets": []}
    out: list[str] = []
    q              = query.lower()
    for msg in reversed(history[-120:]):
        if user_id_filter is not None and msg.user_id != user_id_filter:
            continue
        text = render_stored_message(msg)
        if not text:
            continue
        if q in text.lower():
            lid    = getattr(msg, "local_id", "") or ""
            prefix = f"{lid} " if lid else ""
            out.append(f"{prefix}{msg.role}:{msg.name}<{msg.user_id}>:{text}")
        if len(out) >= limit:
            break
    return {"snippets": out}


def _tool_query_db(
    db: MemoryDB, args: dict[str, Any], *, type_filter: str, chat_id: str
) -> dict[str, Any]:
    if type_filter not in {"topic_summary", "person_info", "person_profile"}:
        raise ValueError("unsupported scoped memory type")
    query = str(args.get("query", "")).strip()
    top_k = _bounded_tool_int(args, "top_k", default=5)
    subject_id = _optional_positive_tool_int(args, "subject_id")
    if not query:
        return {"items": []}
    scoped_chat_id = (chat_id or "").strip()
    if not scoped_chat_id:
        raise ValueError("chat_id is required for scoped memory tools")
    meta_filter = None
    if subject_id is not None and type_filter in ("person_info", "person_profile"):
        meta_filter = {"subject_id": subject_id}
    items = db.query(
        query,
        chat_id     = scoped_chat_id,
        top_k       = top_k,
        min_score   = 0.0,
        type_filter = type_filter,
        meta_filter = meta_filter,
    )
    return {"items": [_public_retrieved_item(item) for item in items]}


def _tool_query_global_db(
    db: MemoryDB,
    args: dict[str, Any],
    *,
    type_filter: str,
) -> dict[str, Any]:
    if type_filter not in {"knowledge", "word_def"}:
        raise ValueError("unsupported global memory type")
    query = str(args.get("query", "")).strip()
    top_k = _bounded_tool_int(args, "top_k", default=5)
    if not query:
        return {"items": []}
    items = db.query_global(
        query,
        top_k       = top_k,
        min_score   = 0.0,
        type_filter = type_filter,
    )
    return {"items": [_public_retrieved_item(item) for item in items]}


def _execute_memory_tool(fn: ToolFunc, arguments: dict[str, Any]) -> dict[str, Any]:
    """把工具或数据库错误转换为稳定且不敏感的观察结果。"""
    try:
        return fn(arguments)
    except (TypeError, ValueError) as exc:
        _logger.warning("memory tool request rejected error_type=%s", type(exc).__name__)
        return {"error": "invalid_memory_tool_request"}
    except Exception as exc:
        _logger.error("memory tool query failed error_type=%s", type(exc).__name__)
        return {"error": "memory_query_failed"}


def _tool_get_person_profile(db: MemoryDB, args: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
    subject_id = _optional_positive_tool_int(args, "subject_id")
    if subject_id is None:
        return {"items": []}
    scoped_chat_id = (chat_id or "").strip()
    if not scoped_chat_id:
        raise ValueError("chat_id is required for scoped memory tools")
    item = db.get(f"profile:{scoped_chat_id}:{subject_id}")
    if not item:
        return {"items": []}
    return {"items": [_public_retrieved_item(item)]}


def _query_direct_memory_items(
    memory_db: MemoryDB,
    question: str,
    *,
    cfg: MemoryConfig,
    chat_id: str,
) -> list[RetrievedItem]:
    limit = max(0, int(cfg.top_k))
    if limit == 0:
        return []
    items = memory_db.query(
        question,
        chat_id     = chat_id,
        top_k       = max(6, limit * 4),
        min_score   = cfg.min_score,
        type_filter = None,
        meta_filter = None,
    )
    return sorted(items, key=lambda item: (-float(item.score), item.doc_id))[:limit]


def _bounded_memory_text(text: str, cfg: MemoryConfig) -> str:
    """限制长期记忆在主提示词中的体积，确保近期对话保持主导。"""

    value = str(text or "").strip()
    limit = max(0, cfg.max_block_chars)
    if limit <= 0 or len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def _format_memory_items(items: Sequence[RetrievedItem], cfg: MemoryConfig) -> str:
    lines = [f"- {it.text.strip()}" for it in items if str(it.text or "").strip()]
    return _bounded_memory_text("\n".join(lines), cfg)


async def react_retrieve(
    *,
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
) -> str:
    """执行有界工具循环；只有非空检索证据可以授权最终答案。"""

    if "_ai" in secrets and secrets.get("_ai") is None:
        return ""

    tool_impl: dict[str, ToolFunc] = {
        "query_chat_history": lambda a: _tool_query_chat_history(history, a),
        "query_topic_summaries": lambda a: _tool_query_db(
            memory_db, a, type_filter="topic_summary", chat_id=chat_id
        ),
        "query_person_info": lambda a: _tool_query_db(
            memory_db, a, type_filter="person_info", chat_id=chat_id
        ),
        "query_words": lambda a: _tool_query_global_db(
            memory_db,
            a,
            type_filter="word_def",
        ),
        "query_knowledge": lambda a: _tool_query_global_db(
            memory_db,
            a,
            type_filter="knowledge",
        ),
        "query_person_profile": lambda a: _tool_get_person_profile(memory_db, a, chat_id=chat_id),
    }

    messages: list[dict[str, Any]] = build_react_messages(question=question)
    started        = time.monotonic()
    api_call_count = 0
    has_evidence   = False

    for _ in range(max(1, int(cfg.max_agent_iterations))):
        if time.monotonic() - started > float(cfg.agent_timeout_seconds):
            break
        api_call_count += 1
        resp, _ = await chat_completions_raw_with_fallback_paths(
            secrets                = secrets,
            messages               = messages,
            temperature            = min(0.4, temperature),
            top_p                  = top_p,
            max_tokens             = min(768, max_tokens),
            timeout_seconds        = timeout_seconds,
            max_retry              = max_retry,
            retry_interval_seconds = retry_interval_seconds,
            tools                  = _tools_schema(),
            tool_choice            = "auto",
        )
        tool_calls = _extract_tool_calls(resp)
        if not tool_calls:
            content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get(
                "content"
            ) or ""
            return str(content).strip() if has_evidence else ""

        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in tool_calls
                ],
            }
        )

        final: str | None = None
        for call in tool_calls:
            fn = tool_impl.get(call.name)
            result: dict[str, Any]
            if call.name == "found_answer":
                if has_evidence:
                    result = {
                        "done": True,
                        "final": str(call.arguments.get("answer", "")).strip(),
                    }
                else:
                    result = {"error": "memory_evidence_required"}
            elif call.name == "not_enough_info":
                result = {"done": True, "final": ""}
            elif not fn:
                result = {"error": "unknown_memory_tool"}
            else:
                result = await asyncio.to_thread(_execute_memory_tool, fn, call.arguments)
                for result_key in ("items", "snippets"):
                    values = result.get(result_key)
                    if isinstance(values, list) and values:
                        has_evidence = True
                        break
            if result.get("done") is True and isinstance(result.get("final"), str):
                final = result["final"]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        if final is not None:
            _logger.info(
                "memory_retrieval agent api_calls=%d elapsed=%.2fs",
                api_call_count,
                time.monotonic() - started,
            )
            return final

        await asyncio.sleep(0)

    _logger.info(
        "memory_retrieval agent api_calls=%d elapsed=%.2fs (exhausted)",
        api_call_count,
        time.monotonic() - started,
    )
    return ""


_EXPLICIT_MEMORY_REFERENCE_RE = re.compile(
    r"(?:记得|还记得|记不记得|忘了|想起来|回忆|记录|历史|聊天记录|"
    r"之前|以前|曾经|上次|上回|那次|刚才|刚刚|前面|早先|说过|提过|聊过|讲过)"
)
_PERSON_MEMORY_QUERY_RE = re.compile(
    r"(?:你|我|他|她|他们|她们|大家|群里(?:的人)?|"
    r"(?:小|老|阿)[\u4e00-\u9fff])"
    r"[^，。！？；;\n]{0,20}"
    r"(?:喜欢|讨厌|习惯|生日|住哪|来自|专业|工作|身份|关系|叫什么|是谁|"
    r"想要|打算|答应|有没有)"
)


def _needs_memory_agent(text: str) -> bool:
    """判断直接检索未命中后，是否值得继续启动工具代理。

    只识别明确回指既往对话或人物稳定信息的句法结构，不维护话题词表。普通知识
    问题即使向量库未命中，也不应为一次注定无关的记忆代理额外等待数秒。
    """

    normalized = re.sub(r"\s+", "", str(text or ""))
    if not normalized:
        return False
    return bool(
        _EXPLICIT_MEMORY_REFERENCE_RE.search(normalized)
        or _PERSON_MEMORY_QUERY_RE.search(normalized)
    )


async def build_memory_block(
    *,
    data_dir: Path,
    chat_id: str,
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
) -> str:
    """按“缓存 → 直接向量命中 → 有证据工具代理”顺序构造有界记忆块。"""

    if not cfg.enable_memory_retrieval:
        return ""
    soft_budget               = 4.0
    explicit_planner_question = planner_question.strip()
    question                  = explicit_planner_question
    current_query             = str(current_text or "").strip()
    if not question and current_query and len(current_query) <= 120:
        question = current_query
    if not question and cfg.planner_question:
        msgs = build_question_messages(
            bot_name=bot_name, history=history, current_text=current_text
        )
        payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
        try:
            raw, _ = await asyncio.wait_for(
                chat_completions_raw_with_fallback_paths(
                    secrets                = secrets,
                    messages               = payload_msgs,
                    temperature            = min(0.5, temperature),
                    top_p                  = top_p,
                    max_tokens             = min(256, max_tokens),
                    timeout_seconds        = min(3.0, float(timeout_seconds)),
                    max_retry              = 0,
                    retry_interval_seconds = 0.2,
                    extra_payload          = {"response_format": {"type": "json_object"}},
                ),
                timeout=min(2.0, soft_budget),
            )
            content = (((raw.get("choices") or [{}])[0] or {}).get("message") or {}).get(
                "content"
            ) or ""
            question = _parse_question_json(str(content))
        except Exception:
            question = ""

    if not question:
        question = current_query
        if len(question) > 240:
            question = question[:240].rstrip()
    if not question:
        return ""

    if cfg.enable_thinking_back_cache:
        cached = await asyncio.to_thread(
            get_cached_answer,
            data_dir       = data_dir,
            chat_id        = chat_id,
            question       = question,
            window_seconds = float(cfg.thinking_back_window_seconds or 0.0),
        )
        if cached:
            return f"你回忆起了以下信息：\n{_bounded_memory_text(cached, cfg)}\n"

    direct_items = await asyncio.to_thread(
        _query_direct_memory_items,
        memory_db,
        question,
        cfg     = cfg,
        chat_id = chat_id,
    )
    direct_answer = _format_memory_items(direct_items, cfg)
    if direct_answer:
        if cfg.enable_thinking_back_cache:
            await asyncio.to_thread(
                append_record,
                data_dir    = data_dir,
                chat_id     = chat_id,
                question    = question,
                answer      = direct_answer,
                max_entries = int(cfg.thinking_back_max_entries or 200),
            )
        return f"你回忆起了以下信息：\n{direct_answer}\n"

    if (
        bool(getattr(cfg, "agent_on_direct_miss_requires_reference", False))
        and not explicit_planner_question
        and not _needs_memory_agent(f"{current_text}\n{question}")
    ):
        return ""

    answer = ""
    try:
        answer = await asyncio.wait_for(
            react_retrieve(
                secrets                = secrets,
                cfg                    = cfg,
                history                = history,
                chat_id                = chat_id,
                question               = question,
                memory_db              = memory_db,
                temperature            = temperature,
                top_p                  = top_p,
                max_tokens             = max_tokens,
                timeout_seconds        = min(4.0, float(timeout_seconds)),
                max_retry              = 0,
                retry_interval_seconds = 0.2,
            ),
            timeout=float(soft_budget),
        )
    except Exception:
        answer = ""
    answer = (answer or "").strip()
    if not answer:
        return ""
    answer = _bounded_memory_text(answer, cfg)
    if cfg.enable_thinking_back_cache:
        await asyncio.to_thread(
            append_record,
            data_dir    = data_dir,
            chat_id     = chat_id,
            question    = question,
            answer      = answer,
            max_entries = int(cfg.thinking_back_max_entries or 200),
        )
    return f"你回忆起了以下信息：\n{answer}\n"
