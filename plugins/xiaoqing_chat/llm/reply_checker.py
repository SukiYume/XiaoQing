from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from . import llm_client
from .llm_client import LLMError, chat_completions_raw_with_fallback_paths
from ..constants import is_question
from ..message_parts import render_stored_message
from ..memory.memory import StoredMessage
from ..utils.json_parsing import parse_first_json_object

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


def _last_bot_messages(history: Sequence[StoredMessage], *, bot_name: str, limit: int) -> list[str]:
    out: list[str] = []
    for msg in reversed(history[-200:]):
        if msg.role != "assistant":
            continue
        name = (msg.name or "").strip()
        if name and bot_name and name != bot_name:
            continue
        text = render_stored_message(msg)
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


def _is_question_sentence(text: str) -> bool:
    return is_question(text)


def _check_repeated_question(
    *,
    reply: str,
    bot_msgs: list[str],
    similarity_threshold: float = 0.75,
) -> Optional[ReplyCheckResult]:
    """Reject reply if it repeats a question that was already asked but not answered."""
    if not _is_question_sentence(reply):
        return None
    r = _normalize_text(reply)
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

    max_look_back = max(4, int(max_repeat_compare))
    bot_msgs = _last_bot_messages(history, bot_name=bot_name, limit=max_look_back)

    if bot_msgs and max_repeat_compare > 0:
        for prev_msg in bot_msgs[: int(max_repeat_compare)]:
            last = _normalize_text(prev_msg)
            if r == last:
                return ReplyCheckResult(False, "回复与之前机器人消息完全相同", True)
            sim = difflib.SequenceMatcher(None, r, last).ratio()
            if sim >= float(similarity_threshold):
                return ReplyCheckResult(False, f"回复与之前机器人消息高度相似({sim:.2f})", True)

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
        bot_msgs=bot_msgs[:4],
        similarity_threshold=similarity_threshold,
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
        return ReplyCheckResult(False, "reply_checker missing credentials", True)

    # Trim history to last 800 chars to reduce input tokens
    _hist = chat_history_text.strip()
    if len(_hist) > 800:
        _hist = _hist[-800:]

    _policy = ""
    if policy_text.strip():
        _policy = "策略：" + policy_text.strip()[:200] + "\n"

    prompt = (
        f"你是一个聊天逻辑检查器。{bot_name}是一个拟人聊天角色，不是信息助手。"
        "请检查以下回复是否合适。重复/轰炸已由前置规则检查过，无需再判断。\n\n"
        f"当前对话目标：{goal}\n"
        f"{_policy}"
        f"最近的对话记录：\n{_hist}\n\n"
        "注意：如果回复里出现 [表情包：...] 或 [QQ表情：...]，表示最终消息会附带相应媒体，"
        "这些媒体也算回复内容的一部分，需要一起判断是否自然、贴切。\n\n"
        f"待检查的最终回复：\n{reply}\n\n"
        "请结合对话记录检查以下几点：\n"
        "1. 这条回复是否符合当前对话目标和上下文\n"
        "2. 是否与最近的对话记录保持一致性（不矛盾、不答非所问）\n"
        "3. 是否包含违规内容（血腥暴力、政治敏感等）\n"
        "4. 是否自问自答或混淆了说话人身份\n"
        "5. 是否逻辑通顺\n"
        "6. 是否使用了完全没必要的修辞或过于刻意\n"
        "7. 如果附带的媒体没有为回复增加新的交流功能，只是在机械复读、镜像、重复上一条媒体语义，通常应判为不合适\n\n"
        "注意：这是拟人角色的日常聊天。"
        "口语化、犹豫、撒娇、吐槽、调侃、说不知道、反问对方都是正常的拟人表现，不应因此判为不合适。"
        "简短随意的回复是正常的聊天风格，不要因为回复短或没有提供'有价值的信息'就拒绝。\n\n"
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
    content = llm_client.extract_response_content(resp)
    obj = parse_first_json_object(content)
    if not obj:
        return ReplyCheckResult(False, "reply_checker returned invalid response", True)
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
    heuristic_reply: str = "",
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
    heuristic_source = str(heuristic_reply or reply or "").strip()
    h = _heuristic_check(
        reply=heuristic_source,
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
        _log.warning("reply_checker LLM 调用失败，拒绝当前回复: %s", exc)
        return ReplyCheckResult(False, f"reply_checker failed: {exc}", True)
