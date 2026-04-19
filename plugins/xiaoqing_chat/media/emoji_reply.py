from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from typing import Sequence

from ..helper_utils import _resolve_llm_config
from ..llm.llm_client import chat_completions
from ..llm.prompt_builder import build_dialogue_prompt
from ..memory.memory import StoredMessage
from .emoji_library import EmojiLibraryEntry, load_emoji_library, select_emoji_for_tags


@dataclass(frozen=True)
class EmojiReplyPlan:
    entry: EmojiLibraryEntry
    selected_tag: str
    marker: str
    reasoning: str
    mode: str = "text_with_emoji"


def _media_cfg(runtime):
    return getattr(getattr(runtime, "cfg", None), "media", None)


def _media_cfg_value(runtime, field: str, default):
    cfg = _media_cfg(runtime)
    if cfg is None:
        return default
    return getattr(cfg, field, default)


def _assistant_turns_since_last_emoji(history: Sequence[StoredMessage]) -> int | None:
    turns = 0
    for message in reversed(history):
        if message.role != "assistant":
            continue
        if "[表情包：" in (message.content or ""):
            return turns
        turns += 1
    return None


def _candidate_tags(entries: Sequence[EmojiLibraryEntry], *, max_tags: int = 12) -> list[str]:
    tags: list[str] = []
    for entry in entries:
        for tag in entry.emotion_tags:
            cleaned = str(tag or "").strip()
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
            if len(tags) >= max_tags:
                return tags
    return tags


def _extract_choice_json(output: str) -> dict[str, str]:
    text = (output or "").strip()
    if not text:
        return {}
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_mode(value: str) -> str:
    lowered = str(value or "").strip().lower()
    aliases = {
        "none": "none",
        "text": "none",
        "text_only": "none",
        "emoji_only": "emoji_only",
        "image_only": "emoji_only",
        "emoji": "emoji_only",
        "text_with_emoji": "text_with_emoji",
        "text+emoji": "text_with_emoji",
        "emoji_with_text": "text_with_emoji",
    }
    return aliases.get(lowered, "")


def _parse_tag_choice(output: str, candidates: Sequence[str]) -> tuple[str, str]:
    payload = _extract_choice_json(output)
    mode = _normalize_mode(str(payload.get("mode", "") or ""))
    tag_hint = str(payload.get("tag", "") or "").strip()

    text = (output or "").strip()
    lowered = text.lower()
    if not mode:
        if "emoji_only" in lowered or "image_only" in lowered or "只发表情" in text or "只发图片" in text:
            mode = "emoji_only"
        elif "text_with_emoji" in lowered or "文字+表情" in text or "文字加表情" in text:
            mode = "text_with_emoji"
        elif "none" in lowered or "不发" in text or "不用" in text or "纯文字" in text:
            mode = "none"

    selected_tag = ""
    search_text = tag_hint or text
    for candidate in candidates:
        if candidate and candidate in search_text:
            selected_tag = candidate
            break
    if not selected_tag:
        compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", search_text)
        for candidate in candidates:
            if candidate and compact == candidate:
                selected_tag = candidate
                break
    if not mode:
        mode = "text_with_emoji" if selected_tag else "none"
    return mode, selected_tag


async def plan_emoji_reply(
    *,
    context,
    runtime,
    history: Sequence[StoredMessage],
    user_text: str,
    reply_text: str,
    secrets: dict[str, str] | dict[str, object],
) -> EmojiReplyPlan | None:
    if not bool(_media_cfg_value(runtime, "enable_outbound_emoji_reply", False)):
        return None
    if not (reply_text or "").strip():
        return None
    if random.random() > float(_media_cfg_value(runtime, "emoji_reply_probability", 0.35)):
        return None

    cooldown_turns = max(0, int(_media_cfg_value(runtime, "emoji_cooldown_turns", 3)))
    turns_since_last = _assistant_turns_since_last_emoji(history)
    if turns_since_last is not None and turns_since_last < cooldown_turns:
        return None

    entries = await load_emoji_library(context, runtime)
    if not entries:
        return None

    candidate_count = max(1, int(_media_cfg_value(runtime, "emoji_candidate_count", 6)))
    if len(entries) > candidate_count:
        candidates = random.sample(entries, candidate_count)
    else:
        candidates = list(entries)

    tags = _candidate_tags(candidates)
    if not tags:
        return None

    api_base = str(secrets.get("api_base", "") or "")
    api_key = str(secrets.get("api_key", "") or "")
    model = str(secrets.get("model", "") or "")
    if not api_base or not api_key or not model:
        return None

    llm_cfg = _resolve_llm_config(runtime.cfg, secrets, foreground=False)
    recent_dialogue = build_dialogue_prompt(history[-6:], bot_name=(context.config or {}).get("bot_name", "小青"), truncate=False, max_chars=500)
    prompt = (
        "你要决定这次回复是否要使用表情包，以及是只发表情包还是文字后再发表情包。"
        '只输出 JSON，不要额外解释。格式: {"mode":"none|emoji_only|text_with_emoji","tag":"...","reason":"..."}。\n'
        f"最近对话\n{recent_dialogue or '（无）'}\n\n"
        f"对方刚发\n{user_text.strip()}\n\n"
        f"你准备回复\n{reply_text.strip()}\n\n"
        "可选标签\n"
        + "、".join(tags)
        + "\n\n选择规则："
        "\n1. 如果纯文字就够了，mode 选 none，tag 留空。"
        "\n2. 如果只发一张表情包更像真人，mode 选 emoji_only。"
        "\n3. 如果先发文字再补一张表情包更自然，mode 选 text_with_emoji。"
        "\n4. tag 必须从候选标签里选。"
    )

    output = await chat_completions(
        session=context.http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[
            {
                "role": "system",
                "content": "你是聊天回复模态选择器，只输出指定 JSON。mode 只能是 none、emoji_only、text_with_emoji。",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        top_p=float(getattr(runtime.cfg, "top_p", 0.9)),
        max_tokens=32,
        timeout_seconds=float(llm_cfg.timeout_seconds),
        max_retry=int(llm_cfg.max_retry),
        retry_interval_seconds=float(llm_cfg.retry_interval_seconds),
        proxy=str(llm_cfg.proxy or ""),
        endpoint_path=str(llm_cfg.endpoint_path or runtime.cfg.endpoint_path),
    )

    mode, selected_tag = _parse_tag_choice(output, tags)
    if mode == "none" or not selected_tag:
        return None

    entry = select_emoji_for_tags(entries, [selected_tag])
    if entry is None:
        return None

    return EmojiReplyPlan(
        entry=entry,
        selected_tag=selected_tag,
        marker=entry.marker,
        reasoning=f"emoji_mode:{mode};emoji_tag:{selected_tag}",
        mode=mode,
    )
