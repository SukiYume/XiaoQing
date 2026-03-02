from __future__ import annotations

import asyncio
import difflib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .llm_client import LLMError, chat_completions_raw_with_fallback_paths
from ..memory.memory import StoredMessage

import logging as _logging
_log = _logging.getLogger(__name__)

@dataclass(frozen=True)
class ReplyCheckResult:
    suitable: bool
    reason: str
    need_replan: bool

class ReplyRejected(RuntimeError):
    def __init__(self, reason: str, need_replan: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.need_replan = need_replan

_RE_JSON_OBJ = re.compile(r"\{[\s\S]*\}")

def _last_bot_messages(history: Sequence[StoredMessage], *, bot_name: str, limit: int) -> list[str]:
    out: list[str] = []
    for msg in reversed(history[-200:]):
        if msg.role != "assistant":
            continue
        name = (msg.name or "").strip()
        if name and bot_name and name != bot_name:
            continue
        text = (msg.content or "").strip()
        if not text:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out

def _normalize_text(s: str) -> str:
    t = (s or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t

_QUESTION_KEYWORDS = ("啥", "谁", "咋", "为啥", "为什么", "什么", "哪", "哪里", "哪个", "多少", "几", "吗", "嘛")


def _is_question_sentence(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("？") or t.endswith("?"):
        return True
    return any(kw in t for kw in _QUESTION_KEYWORDS)


def _check_repeated_question(
    *,
    reply: str,
    history: Sequence[StoredMessage],
    bot_name: str,
    max_look_back: int = 4,
    similarity_threshold: float = 0.75,
) -> Optional[ReplyCheckResult]:
    """Reject reply if it repeats a question that was already asked but not answered."""
    if not _is_question_sentence(reply):
        return None
    r = _normalize_text(reply)
    bot_msgs = _last_bot_messages(history, bot_name=bot_name, limit=max_look_back)
    for prev_msg in bot_msgs:
        if not _is_question_sentence(prev_msg):
            continue
        prev = _normalize_text(prev_msg)
        sim = difflib.SequenceMatcher(None, r, prev).ratio()
        if sim >= similarity_threshold:
            return ReplyCheckResult(
                suitable=False,
                reason=f"重复了之前已经问过的问题（相似度{sim:.2f}），换个话题",
                need_replan=False,
            )
    return None


def _heuristic_check(
    *,
    reply: str,
    history: Sequence[StoredMessage],
    bot_name: str,
    max_repeat_compare: int,
    similarity_threshold: float,
    max_assistant_in_row: int,
) -> Optional[ReplyCheckResult]:
    r = _normalize_text(reply)
    if not r:
        return ReplyCheckResult(False, "回复为空", True)

    bot_msgs = _last_bot_messages(history, bot_name=bot_name, limit=max(1, int(max_repeat_compare)))
    if bot_msgs:
        last = _normalize_text(bot_msgs[0])
        if r == last:
            return ReplyCheckResult(False, "回复与上一条机器人消息完全相同", True)
        sim = difflib.SequenceMatcher(None, r, last).ratio()
        if sim >= float(similarity_threshold):
            return ReplyCheckResult(False, f"回复与上一条机器人消息高度相似({sim:.2f})", True)

    in_row = 0
    for msg in reversed(history[-40:]):
        if msg.role == "assistant":
            in_row += 1
            continue
        break
    if max_assistant_in_row > 0 and in_row >= int(max_assistant_in_row):
        return ReplyCheckResult(False, "疑似消息轰炸（连续多条机器人发言）", True)

    rq = _check_repeated_question(
        reply=reply,
        history=history,
        bot_name=bot_name,
    )
    if rq:
        return rq

    return None

async def _llm_check(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    reply: str,
    goal: str,
    policy_text: str,
    chat_history_text: str,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> ReplyCheckResult:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return ReplyCheckResult(True, "", False)

    # Trim history to last 800 chars to reduce input tokens
    _hist = chat_history_text.strip()
    if len(_hist) > 800:
        _hist = _hist[-800:]

    _policy = ""
    if policy_text.strip():
        _policy = "策略：" + policy_text.strip()[:200] + "\n"

    prompt = (
        "判断这条回复是否合适。重复/轰炸已检查过，只需关注：\n"
        "1. 是否符合对话目标和上下文\n"
        "2. 是否有违规内容\n"
        "3. 语气是否自然，不生硬\n"
        "4. 是否自问自答或前后矛盾\n\n"
        f"机器人：{bot_name}\n"
        f"目标：{goal}\n"
        f"{_policy}"
        f"最近对话：\n{_hist}\n\n"
        f"待检查回复：\n{reply}\n\n"
        '仅输出JSON：{"suitable": true/false, "reason": "...", "need_replan": false}'
    )
    resp, _path = await chat_completions_raw_with_fallback_paths(
        session=http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        top_p=0.8,
        max_tokens=128,
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        proxy=proxy,
        endpoint_path=endpoint_path,
    )
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    s = str(content).strip()
    m = _RE_JSON_OBJ.search(s)
    if m:
        s = m.group(0)
    try:
        obj = json.loads(s)
    except Exception:
        return ReplyCheckResult(True, "", False)
    if not isinstance(obj, dict):
        return ReplyCheckResult(True, "", False)
    suitable = bool(obj.get("suitable", True))
    reason = str(obj.get("reason", "") or "").strip()
    need_replan = bool(obj.get("need_replan", False))
    return ReplyCheckResult(suitable=suitable, reason=reason, need_replan=need_replan)

async def check_reply(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    reply: str,
    goal: str,
    policy_text: str = "",
    history: Sequence[StoredMessage],
    chat_history_text: str,
    enable_llm_checker: bool,
    max_repeat_compare: int,
    similarity_threshold: float,
    max_assistant_in_row: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> ReplyCheckResult:
    h = _heuristic_check(
        reply=reply,
        history=history,
        bot_name=bot_name,
        max_repeat_compare=max_repeat_compare,
        similarity_threshold=similarity_threshold,
        max_assistant_in_row=max_assistant_in_row,
    )
    if h:
        return h
    if not enable_llm_checker:
        return ReplyCheckResult(True, "", False)
    try:
        return await _llm_check(
            http_session=http_session,
            secrets=secrets,
            bot_name=bot_name,
            reply=reply,
            goal=goal,
            policy_text=policy_text,
            chat_history_text=chat_history_text,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=endpoint_path,
        )
    except (LLMError, TimeoutError, asyncio.TimeoutError, Exception) as exc:
        _log.warning("reply_checker LLM 调用失败，跳过检查: %s", exc)
        return ReplyCheckResult(True, "", False)
