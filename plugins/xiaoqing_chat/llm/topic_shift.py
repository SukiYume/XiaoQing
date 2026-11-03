"""新消息到达后，由规划路由复核已经生成的回复是否仍适合投递。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from ..memory.memory import StoredMessage
from ..persona import resolve_bot_name
from ..utils.json_parsing import parse_first_json_object, strict_json_bool
from .gateway import chat_completions_raw_with_fallback_paths, extract_response_content
from .llm_config import LLMCallConfig
from .prompt_builder import build_dialogue_prompt

# 发送前确认不能拖太久：这段时间里群里还在继续说。
_TOPIC_CHECK_TIMEOUT_SECONDS = 6.0
# 要接的那句之前再带几条，模型才知道原来在聊什么。
_CONTEXT_BEFORE_TARGET = 4

_PROMPT = """你在看一个 QQ 群的聊天。{bot_name}准备回复【她要接的话】里的最后一条，回复内容见【她的回复】。在她发出去之前，群里又有人说了【后来的消息】。

结合完整上下文判断这条回复是否应当取消。话题已经结束或转移、对方明确要求她停止回复、当前应当给别人留出空间时，选择取消。
识别叫停要求的实际接收对象，区分对她的要求、对其他人的要求、转述和引用。结合上下文理解否定、补充和转折。
后续消息延续原话题且这条回复仍有内容可说时，可以保留。

【她要接的话】
{before}

【她的回复】
{reply}

【后来的消息】
{after}

只输出 JSON：{{"withhold_reply": true 或 false}}"""


async def reply_should_be_withheld(
    *,
    secrets: dict[str, Any],
    bot_name: str,
    before: Sequence[StoredMessage],
    after: Sequence[StoredMessage],
    reply: str,
    llm: LLMCallConfig,
    log: Any = None,
) -> bool:
    """请规划路由判断是否取消投递；复核不可用时保留旁听。"""

    if "_ai" in secrets and secrets.get("_ai") is None:
        return True
    name   = resolve_bot_name(bot_name)
    prompt = _PROMPT.format(
        bot_name=name,
        before=build_dialogue_prompt(before[-(_CONTEXT_BEFORE_TARGET + 1) :], bot_name=name),
        reply=reply.strip() or "（无文字）",
        after=build_dialogue_prompt(after, bot_name=name),
    )
    request = replace(
        llm,
        timeout_seconds       = min(float(llm.timeout_seconds), _TOPIC_CHECK_TIMEOUT_SECONDS),
        total_timeout_seconds = min(float(llm.total_timeout_seconds), _TOPIC_CHECK_TIMEOUT_SECONDS),
        max_retry             = 0,
    )
    started = time.monotonic()
    try:
        response, _profile = await chat_completions_raw_with_fallback_paths(
            route    = "reasoning",
            secrets  = secrets,
            messages = [{"role": "user", "content": prompt}],
            **request.as_kwargs(),
        )
    except Exception as exc:
        if log is not None:
            log("smalltalk.context_check.error", {"error_type": type(exc).__name__})
        return True
    parsed  = parse_first_json_object(extract_response_content(response)) or {}
    changed = strict_json_bool(parsed.get("withhold_reply")) is not False
    if log is not None:
        log(
            "smalltalk.context_check",
            {
                "changed"  : changed,
                "after"    : len(after),
                "elapsed_s": round(time.monotonic() - started, 3),
            },
        )
    return changed
