from __future__ import annotations

import random
from typing import Any

from ..config.config import RewriteConfig
from .llm_client import chat_completions
from .prompt_builder import ChatMessage

_REWRITE_SYSTEM = (
    "你是对话回复改写器。你会把一段回复改写得更像真人、更口语、更自然。\n"
    "要求：不改变核心意思；更短更随意；允许有点犹豫/停顿；不要加前后缀；不要使用括号解释；不要 emoji。\n"
    "不要输出“改写：”“回复：”之类前缀，只输出改写后的最终文本。"
)

def build_rewrite_messages(*, style: str, user_text: str, reply_text: str) -> list[ChatMessage]:
    user = (
        "场景\n你正在群聊里随口接话。\n\n"
        "对方刚刚说\n{u}\n\n"
        "你准备回复的原句（需要改得更像真人）\n{r}\n\n"
        "风格偏好\n{style}\n\n"
        "只输出改写后的回复："
    ).format(u=user_text.strip(), r=reply_text.strip(), style=style.strip())
    return [
        ChatMessage(role="system", content=_REWRITE_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]

async def maybe_rewrite_reply(
    *,
    http_session,
    secrets: dict[str, Any],
    cfg: RewriteConfig,
    style: str,
    user_text: str,
    reply_text: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> str:
    if not cfg.enable_rewrite:
        return reply_text
    if not reply_text.strip():
        return reply_text
    if len(reply_text) < int(cfg.max_length_trigger) and random.random() >= max(0.0, min(1.0, cfg.probability)):
        return reply_text

    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return reply_text

    msgs = build_rewrite_messages(style=style, user_text=user_text, reply_text=reply_text)
    payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
    out = await chat_completions(
        session=http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=payload_msgs,
        temperature=min(0.7, temperature),
        top_p=top_p,
        max_tokens=min(256, max_tokens),
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        proxy=proxy,
        endpoint_path=endpoint_path,
    )
    out = (out or "").strip()
    if not out:
        return reply_text
    if len(out) > len(reply_text) * 2:
        return reply_text
    return out
