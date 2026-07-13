from __future__ import annotations

import difflib
import logging as _logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..memory.memory import StoredMessage
from ..message_parts import render_stored_message
from ..utils.json_parsing import parse_first_json_object, strict_json_bool
from . import llm_client
from .control_payload import control_extra_payload
from .llm_client import LLMError, chat_completions_raw_with_fallback_paths

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


_FILLER_OPENER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("哈哈", re.compile(r"^哈哈+")),
    ("笑死", re.compile(r"^笑死(我了|了|啦)?")),
    ("笑哭", re.compile(r"^笑哭(了|啦)?")),
    ("啊这", re.compile(r"^啊这")),
    ("草", re.compile(r"^草+(?![木莓案])")),
)


def _filler_opener_key(text: str) -> str:
    head = (text or "").lstrip().lstrip("「『\"'“‘([【")
    if not head:
        return ""
    for key, pattern in _FILLER_OPENER_PATTERNS:
        if pattern.match(head):
            return key
    return ""


def _heuristic_check(
    *,
    reply: str,
    history: Sequence[StoredMessage],
    bot_name: str,
    max_repeat_compare: int,
    similarity_threshold: float,
    max_assistant_in_row: int,
) -> ReplyCheckResult | None:
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

    new_opener = _filler_opener_key(r)
    if new_opener:
        recent_openers = [_filler_opener_key(_normalize_text(prev)) for prev in bot_msgs[:3]]
        same_count = sum(1 for k in recent_openers if k == new_opener)
        if same_count >= 1 and len(recent_openers) >= 1 and recent_openers[0] == new_opener:
            return ReplyCheckResult(
                False,
                f"开头“{new_opener}”刚刚已经用过，换一个开头或直接进话题",
                False,
            )
        if same_count >= 2:
            return ReplyCheckResult(
                False,
                f"近几条都以“{new_opener}”开头，换一个开头",
                False,
            )

    in_row = 0
    for msg in reversed(history[-40:]):
        if msg.role == "assistant":
            in_row += 1
            continue
        break
    if max_assistant_in_row > 0 and in_row >= int(max_assistant_in_row):
        return ReplyCheckResult(False, "疑似消息轰炸（连续多条机器人发言）", True)

    return None


_CURRENT_MEDIA_MARKER_RE = re.compile(r"\[(图片|表情包|QQ表情)：([^\]\n]{1,400})\]")
_MEDIA_FORM_RE = re.compile(r"(图片|这张图|那张图|表情包|QQ表情|表情|媒体)")
_INTENT_QUESTION_RE = re.compile(r"(啥意思|什么意思|几个意思|什么情况|干嘛|为何|为什么)")
_VISIBLE_SPEECH_PATTERNS = (
    r"写着[“\"]([^”\"\]]{1,80})[”\"]",
    r"文字(?:内容)?(?:是|为)[“\"]([^”\"\]]{1,80})[”\"]",
    r"配文(?:字)?(?:是|为)?[“\"]([^”\"\]]{1,80})[”\"]",
)


def _current_media_only_anchors(current_text: str) -> tuple[str, ...]:
    text = str(current_text or "").strip()
    if not text:
        return ()
    anchors: list[str] = []
    for match in _CURRENT_MEDIA_MARKER_RE.finditer(text):
        label = str(match.group(2) or "").strip()
        label = re.split(r"[；;]", label, maxsplit=1)[0].strip()
        if label:
            anchors.extend(part.strip() for part in re.split(r"[，,、\s]+", label) if part.strip())
        marker = str(match.group(0) or "")
        for pattern in _VISIBLE_SPEECH_PATTERNS:
            speech_match = re.search(pattern, marker)
            if speech_match:
                anchors.append(str(speech_match.group(1) or "").strip())
                break
    if not anchors:
        return ()
    remainder = _CURRENT_MEDIA_MARKER_RE.sub("", text).strip()
    if remainder:
        return ()
    deduped: list[str] = []
    for anchor in anchors:
        if anchor and anchor not in deduped:
            deduped.append(anchor)
    return tuple(deduped)


def _media_meta_reply_check(*, reply: str, current_text: str) -> ReplyCheckResult | None:
    anchors = _current_media_only_anchors(current_text)
    if not anchors:
        return None

    normalized_reply = _normalize_text(reply)
    if not normalized_reply:
        return None

    anchor_in_reply = any(anchor and anchor in normalized_reply for anchor in anchors if len(anchor) <= 40)
    media_form_mentioned = _MEDIA_FORM_RE.search(normalized_reply) is not None
    asks_about_intent = _INTENT_QUESTION_RE.search(normalized_reply) is not None

    if media_form_mentioned and not anchor_in_reply:
        return ReplyCheckResult(
            False,
            "当前用户只发了媒体消息，回复主要围绕媒体形式本身，没有接住其中的文字、情绪或上下文意图",
            True,
        )

    if asks_about_intent:
        for anchor in anchors:
            if not anchor or len(anchor) > 12 or anchor not in normalized_reply:
                continue
            return ReplyCheckResult(
                False,
                "当前用户只发了媒体消息，回复把媒体摘要标签当作要解释的对象，而不是接住它表达的反应",
                True,
            )
    return None


async def _llm_check(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    reply: str,
    goal: str,
    current_text: str = "",
    policy_text: str,
    chat_history_text: str,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
    extra_payload: dict[str, Any] | None = None,
) -> ReplyCheckResult:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return ReplyCheckResult(True, "reply_checker missing credentials", False)

    # Trim history to last 800 chars to reduce input tokens
    _hist = chat_history_text.strip()
    if len(_hist) > 800:
        _hist = _hist[-800:]

    _current = str(current_text or "").strip()
    if len(_current) > 500:
        _current = _current[-500:]
    _current_block = ""
    if _current:
        _current_block = (
            "当前最新用户消息（这是待检查回复要回应的目标；"
            "如果里面有媒体摘要，需要判断它在聊天里的交际作用："
            "可见文字通常是对方借媒体说的话，情绪标签或 QQ 表情名称通常是对方的语气/态度/反应，"
            "图片内容通常是对方抛出的新话题素材）：\n"
            f"{_current}\n\n"
        )

    _policy = ""
    if policy_text.strip():
        _policy = "策略：" + policy_text.strip()[:200] + "\n"

    prompt = (
        f"你是一个聊天逻辑检查器。{bot_name}是一个拟人聊天角色，不是信息助手。"
        "请检查以下回复是否合适。空回复、完全相同/高度相似文本和连续机器人发言"
        "已有结构性规则检查；其余上下文、语义和节奏问题都由你判断。"
        "你必须只输出一个 JSON object，不要输出推理过程。\n\n"
        f"当前对话目标：{goal}\n"
        f"{_policy}"
        f"最近的对话记录：\n{_hist}\n\n"
        f"{_current_block}"
        "注意：如果回复里出现 [表情包：...] 或 [QQ表情：...]，表示最终消息会附带相应媒体，"
        "这些媒体也算回复内容的一部分，需要一起判断是否自然、贴切。\n\n"
        f"待检查的最终回复：\n{reply}\n\n"
        "请结合对话记录检查以下几点：\n"
        "1. 这条回复是否符合当前对话目标和上下文\n"
        "2. 是否与最近的对话记录保持一致性（不矛盾、不答非所问）\n"
        "3. 是否包含明显不适合公开群聊的内容\n"
        "4. 是否自问自答或混淆了说话人身份\n"
        "5. 是否逻辑通顺\n"
        "6. 是否使用了完全没必要的修辞或过于刻意\n"
        "7. 是否又重复使用近期同一个梗、口癖、调侃角度、追问角度或结论。"
        "即使换了句式，只要听起来还是在拿同一个点反复说，也应判为不合适。"
        "但如果用户当前明确追问某个词或梗，回复可以先解释它；不要解释完又继续把它当笑点反复用\n"
        "8. 如果当前用户消息主要是图片/表情/QQ 表情摘要，"
        "要判断回复是否接住了其中的可见文字、情绪反应、图片内容或原上下文话题；"
        "如果回复只是在围绕媒体形式本身说话，而没有回应它承载的交际意图，通常应判为不合适\n"
        "9. 如果附带的媒体没有为回复增加新的交流功能，只是在机械复读、镜像、"
        "重复上一条媒体语义，通常应判为不合适\n"
        "10. 只有当用户明确要求解释、评价或寻找媒体本身时，回复才适合把媒体形式作为话题；"
        "否则应优先回应媒体表达的内容、态度、情绪或上下文含义\n\n"
        "注意：这是拟人角色的日常聊天。"
        "口语化、犹豫、撒娇、吐槽、调侃、说不知道、反问对方都是正常的拟人表现，不应因此判为不合适。"
        "简短随意的回复是正常的聊天风格，不要因为回复短或没有提供'有价值的信息'就拒绝。\n\n"
        '仅输出JSON：{"suitable": true/false, "reason": "...", "need_replan": false}'
    )
    request_payload = control_extra_payload(extra_payload, json_object=True)
    try:
        resp, _path = await chat_completions_raw_with_fallback_paths(
            session=http_session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            top_p=0.8,
            max_tokens=128,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=endpoint_path,
            extra_payload=request_payload,
        )
    except LLMError as exc:
        if "response_format" not in request_payload or "http_400" not in str(exc):
            raise
        fallback_payload = control_extra_payload(extra_payload, json_object=False)
        resp, _path = await chat_completions_raw_with_fallback_paths(
            session=http_session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            top_p=0.8,
            max_tokens=128,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=endpoint_path,
            extra_payload=fallback_payload,
        )
    content = llm_client.extract_response_content(resp)
    obj = parse_first_json_object(content)
    if not obj:
        return ReplyCheckResult(False, "reply_checker invalid response", True)
    suitable = strict_json_bool(obj.get("suitable"))
    need_replan = strict_json_bool(obj.get("need_replan"))
    if suitable is None or need_replan is None:
        return ReplyCheckResult(False, "reply_checker invalid boolean fields", True)
    reason = str(obj.get("reason", "") or "").strip()
    return ReplyCheckResult(suitable=suitable, reason=reason, need_replan=need_replan)


async def check_reply(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    reply: str,
    heuristic_reply: str = "",
    goal: str,
    current_text: str = "",
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
    extra_payload: dict[str, Any] | None = None,
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
    media_h = _media_meta_reply_check(reply=heuristic_source, current_text=current_text)
    if media_h:
        return media_h
    if not enable_llm_checker:
        return ReplyCheckResult(True, "", False)
    try:
        return await _llm_check(
            http_session=http_session,
            secrets=secrets,
            bot_name=bot_name,
            reply=reply,
            current_text=current_text,
            goal=goal,
            policy_text=policy_text,
            chat_history_text=chat_history_text,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=endpoint_path,
            extra_payload=extra_payload,
        )
    except Exception as exc:
        _log.warning("reply_checker LLM call failed: %s", type(exc).__name__)
        return ReplyCheckResult(False, "reply_checker unavailable", True)
