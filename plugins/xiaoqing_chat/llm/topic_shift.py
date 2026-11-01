"""回复晚到时确认群里有没有换话题：换了就不再接旧话，没换就照常发。

只在回复离要接的那句已经隔了好几条，或者中间有人用“对了”“话说”这类话转开时才问一次
规划路由；离得近又没人转话题时不多花这一次调用。模型不可用、超时或答非所问时按没换
处理，宁可接上也不白白丢掉已经审查过的回复。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from ..memory.memory import StoredMessage
from ..message_parts import render_stored_message
from ..persona import resolve_bot_name
from ..utils.json_parsing import parse_first_json_object, strict_json_bool
from .gateway import chat_completions_raw_with_fallback_paths, extract_response_content
from .llm_config import LLMCallConfig
from .prompt_builder import build_dialogue_prompt

# 发送前确认不能拖太久：这段时间里群里还在继续说。
_TOPIC_CHECK_TIMEOUT_SECONDS = 6.0
# 要接的那句之前再带几条，模型才知道原来在聊什么。
_CONTEXT_BEFORE_TARGET = 4

_TOPIC_OPENER_RE = re.compile(
    r"^\s*(?:对了|话说|说起来|说到这|换个话题|另外|顺便问|突然想[起到]|btw\b|by the way)",
    re.IGNORECASE,
)

_PROMPT = """你在看一个 QQ 群的聊天。{bot_name}准备回复【她要接的话】里的最后一条，回复内容见【她的回复】。在她发出去之前，群里又有人说了【后来的消息】。

判断【后来的消息】是不是已经换到另一个话题，使她这句回复现在发出去显得接不上。
接着原来的事补充、反驳、追问、开玩笑、发表情、简单反应，都算没换；只有大家明显开始聊另一件事才算换了。

【她要接的话】
{before}

【她的回复】
{reply}

【后来的消息】
{after}

只输出 JSON：{{"topic_changed": true 或 false}}"""


def opens_new_topic(message: StoredMessage) -> bool:
    """这条群友消息以“对了”“话说”这类话开头，像是在转话题。"""

    return message.role == "user" and bool(_TOPIC_OPENER_RE.search(render_stored_message(message)))


async def topic_changed(
    *,
    secrets: dict[str, Any],
    bot_name: str,
    before: Sequence[StoredMessage],
    after: Sequence[StoredMessage],
    reply: str,
    llm: LLMCallConfig,
    log: Any = None,
) -> bool:
    """问规划路由群聊是否已经换了话题；不可用或答不上来时返回 False。"""

    if "_ai" in secrets and secrets.get("_ai") is None:
        return False
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
            log("smalltalk.topic_check.error", {"error_type": type(exc).__name__})
        return False
    parsed  = parse_first_json_object(extract_response_content(response)) or {}
    changed = strict_json_bool(parsed.get("topic_changed")) is True
    if log is not None:
        log(
            "smalltalk.topic_check",
            {
                "changed"  : changed,
                "after"    : len(after),
                "elapsed_s": round(time.monotonic() - started, 3),
            },
        )
    return changed
