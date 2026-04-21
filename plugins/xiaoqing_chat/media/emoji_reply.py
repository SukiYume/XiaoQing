from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Sequence

from ..llm.llm_client import chat_completions
from ..logging_utils import _log_step
from ..memory.memory import StoredMessage
from .emoji_library import EmojiLibraryEntry, load_emoji_library
from .reply_planner_common import (
    assistant_turns_since_last_media,
    build_recent_dialogue,
    extract_choice_json,
    extract_inbound_marker_labels,
    find_candidate_by_hint,
    parse_candidate_choice,
    pick_ranked_candidates,
    run_selector_llm,
    should_force_media_consideration,
)


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

_MODE_ALIASES = {
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
_TEXT_MODE_HINTS = (
    ("emoji_only", ("emoji_only", "image_only", "只发表情", "只发图片")),
    ("text_with_emoji", ("text_with_emoji", "文字+表情", "文字加表情")),
    ("none", ("none", "不发", "不用", "纯文字")),
)
_TOKEN_PATTERN = r"[\u4e00-\u9fff]{1,4}|[a-z0-9_]+"
_DIGIT_PATTERN = r"(?<!\d)(\d{1,2})(?!\d)"


def _entry_relevance(entry: EmojiLibraryEntry, tokens: Sequence[str]) -> tuple[float, float, float]:
    haystack = " ".join(
        [
            str(entry.description or ""),
            " ".join(entry.emotion_tags),
            str(entry.marker or ""),
        ]
    ).lower()
    score = 0.0
    for token in tokens:
        if not token:
            continue
        if token in haystack:
            score += 1.8
        elif any(token in tag.lower() or tag.lower() in token for tag in entry.emotion_tags):
            score += 1.0
    if entry.description:
        score += 0.2
    score -= entry.usage_count * 0.05
    if time.time() - entry.last_used_ts < 300:
        score -= 1.0
    return score, -entry.last_used_ts, -float(entry.usage_count)


def _pick_candidate_entries(
    entries: Sequence[EmojiLibraryEntry],
    *,
    user_text: str,
    reply_text: str,
    max_items: int,
) -> list[EmojiLibraryEntry]:
    return pick_ranked_candidates(
        entries,
        user_text=user_text,
        reply_text=reply_text,
        max_items=max_items,
        token_pattern=_TOKEN_PATTERN,
        score_fn=_entry_relevance,
    )


def _render_candidate_block(candidates: Sequence[EmojiLibraryEntry]) -> str:
    lines: list[str] = []
    for index, entry in enumerate(candidates, start=1):
        tags = "、".join(entry.emotion_tags[:4]) if entry.emotion_tags else "无"
        lines.append(
            f"{index}. marker={entry.marker} | 描述={entry.description or '无'} | 标签={tags} | 最近使用={entry.usage_count}次"
        )
    return "\n".join(lines)


def _find_entry_by_hint(candidates: Sequence[EmojiLibraryEntry], hint: str) -> EmojiLibraryEntry | None:
    return find_candidate_by_hint(
        candidates,
        hint,
        key_fn=lambda entry: (
            entry.marker,
            entry.description,
            *entry.emotion_tags,
        ),
    )


def _parse_candidate_choice(output: str, candidates: Sequence[EmojiLibraryEntry]) -> tuple[str, EmojiLibraryEntry | None]:
    return parse_candidate_choice(
        output,
        candidates,
        mode_aliases=_MODE_ALIASES,
        text_mode_hints=_TEXT_MODE_HINTS,
        hint_keys=("candidate", "index", "option", "tag", "description", "marker"),
        resolve_candidate=lambda hint: _find_entry_by_hint(candidates, hint),
        default_mode="text_with_emoji",
        digit_pattern=_DIGIT_PATTERN,
    )


async def _validate_text_with_emoji_plan(
    *,
    context,
    runtime,
    history: Sequence[StoredMessage],
    user_text: str,
    reply_text: str,
    entry: EmojiLibraryEntry,
    secrets: dict[str, str] | dict[str, object],
    chat_id: str = "",
) -> bool:
    recent_dialogue = build_recent_dialogue(history, context=context, current_text=user_text)
    prompt = (
        "你要判断这次“文字后补一张表情包”的选择是否真的有必要。"
        '只输出 JSON，不要额外解释。格式: {"allow":true|false,"reason":"..."}。\n'
        f"最近对话\n{recent_dialogue or '（无）'}\n\n"
        f"对方刚发\n{user_text.strip()}\n\n"
        f"你准备回复\n{reply_text.strip()}\n\n"
        f"计划补的表情包\nmarker={entry.marker} | 描述={entry.description or '无'} | 标签={'、'.join(entry.emotion_tags[:6]) or '无'}\n\n"
        "判断规则："
        "\n1. 只有当这张表情包为当前回复增加新的交流功能时，allow 才能为 true。"
        "\n2. 新的交流功能包括：补充语气、制造反差、增加幽默、补充潜台词、形成收尾。"
        "\n3. 如果这张表情包只是重复对方刚发媒体的情绪或内容，或者只是把文字已经表达清楚的意思再说一遍，allow 应为 false。"
        "\n4. 如果拿不准、效果勉强、只是“也能发”，allow 应为 false。"
    )
    output = await run_selector_llm(
        context=context,
        runtime=runtime,
        secrets=secrets,
        system_prompt="你是聊天回复媒体增益检查器，只输出 allow/reason JSON。",
        user_prompt=prompt,
        chat_func=chat_completions,
    )
    payload = extract_choice_json(output or "")
    allow = payload.get("allow")
    if isinstance(allow, bool):
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.validate",
                fields={
                    "allow": allow,
                    "reason": str(payload.get("reason", "") or ""),
                },
            )
        return allow
    return True


async def plan_emoji_reply(
    *,
    context,
    runtime,
    history: Sequence[StoredMessage],
    user_text: str,
    reply_text: str,
    secrets: dict[str, str] | dict[str, object],
    chat_id: str = "",
    force_consider: bool = False,
) -> EmojiReplyPlan | None:
    inbound_labels = extract_inbound_marker_labels(user_text, "emoji")
    forced = bool(force_consider or should_force_media_consideration(user_text, "emoji"))
    if chat_id:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.emoji.plan.start",
            fields={
                "forced": forced,
                "reply_text": reply_text,
                "inbound_marker": "，".join(inbound_labels),
            },
        )

    if not bool(_media_cfg_value(runtime, "enable_outbound_emoji_reply", False)):
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={"reason": "disabled"},
            )
        return None
    if not (reply_text or "").strip():
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={"reason": "empty_reply"},
            )
        return None
    probability = float(_media_cfg_value(runtime, "emoji_reply_probability", 0.35))
    roll = random.random()
    if not forced and roll > probability:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={"reason": "probability", "roll": round(roll, 4), "threshold": probability},
            )
        return None

    cooldown_turns = max(0, int(_media_cfg_value(runtime, "emoji_cooldown_turns", 3)))
    turns_since_last = assistant_turns_since_last_media(history, "emoji")
    if turns_since_last is not None and turns_since_last < cooldown_turns:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={
                    "reason": "cooldown",
                    "turns_since_last": turns_since_last,
                    "cooldown_turns": cooldown_turns,
                },
            )
        return None

    entries = await load_emoji_library(context, runtime, repair_invalid=True)
    if not entries:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={"reason": "no_entries"},
            )
        return None

    candidate_count = max(1, int(_media_cfg_value(runtime, "emoji_candidate_count", 6)))
    candidates = _pick_candidate_entries(
        entries,
        user_text=user_text,
        reply_text=reply_text,
        max_items=candidate_count,
    )
    if not candidates:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={"reason": "no_candidates"},
            )
        return None

    recent_dialogue = build_recent_dialogue(history, context=context, current_text=user_text)
    candidate_block = _render_candidate_block(candidates)
    prompt = (
        "你要决定这次回复要不要带一张表情包，并且判断是只发一张表情包，还是发文字后再补一张表情包。"
        '只输出 JSON，不要额外解释。格式: {"mode":"none|emoji_only|text_with_emoji","candidate":"候选编号","reason":"..."}。\n'
        f"最近对话\n{recent_dialogue or '（无）'}\n\n"
        f"对方刚发\n{user_text.strip()}\n\n"
        f"你准备回复\n{reply_text.strip()}\n\n"
        f"可选表情候选\n{candidate_block}\n\n"
        "选择规则："
        "\n1. 如果纯文字已经最自然，或者候选都不够贴切、不够好笑、不够机灵，mode 选 none。"
        "\n2. 如果只发一张表情包会更像真人、更有梗、更有回味，mode 选 emoji_only。"
        "\n3. 如果文字已经合适，但补一张表情包能增加语气、反讽、幽默或潜台词，mode 选 text_with_emoji。"
        "\n4. candidate 必须从候选编号里选；如果 mode 是 none，candidate 留空。"
        "\n5. 重点看候选表情的具体内容和标签是否真能贴合当前语境，不要只看某个情绪词。"
        "\n6. 只有当表情包为当前回复增加新的交流功能时，才考虑 emoji_only 或 text_with_emoji。"
        "\n7. 如果表情包只是重复对方刚发媒体的情绪/内容，或者只是把文字已经表达清楚的意思再说一遍，优先选 none。"
    )

    output = await run_selector_llm(
        context=context,
        runtime=runtime,
        secrets=secrets,
        system_prompt="你是聊天回复模态选择器，只输出指定 JSON。mode 只能是 none、emoji_only、text_with_emoji。",
        user_prompt=prompt,
        chat_func=chat_completions,
    )
    if not output:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={"reason": "empty_selector"},
            )
        return None

    mode, entry = _parse_candidate_choice(output, candidates)
    if mode == "none" or entry is None:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.emoji.plan.skip",
                fields={"reason": "selector_none", "mode": mode or "none"},
            )
        return None
    if mode == "text_with_emoji":
        allow = await _validate_text_with_emoji_plan(
            context=context,
            runtime=runtime,
            history=history,
            user_text=user_text,
            reply_text=reply_text,
            entry=entry,
            secrets=secrets,
            chat_id=chat_id,
        )
        if not allow:
            if chat_id:
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.emoji.plan.skip",
                    fields={"reason": "validator_reject", "mode": mode},
                )
            return None

    selected_tag = entry.emotion_tags[0] if entry.emotion_tags else (entry.description or entry.marker)
    plan = EmojiReplyPlan(
        entry=entry,
        selected_tag=selected_tag,
        marker=entry.marker,
        reasoning=f"emoji_mode:{mode};emoji_hint:{selected_tag}",
        mode=mode,
    )
    if chat_id:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.emoji.plan.result",
            fields={
                "mode": plan.mode,
                "emoji_hash": plan.entry.media_hash,
                "emoji_marker": plan.marker,
                "forced": forced,
            },
        )
    return plan
