from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Sequence

from ..llm.llm_client import chat_completions
from ..logging_utils import _log_step
from ..memory.memory import StoredMessage
from .qq_face_catalog import QQFaceEntry, load_qq_face_catalog
from .reply_planner_common import (
    assistant_turns_since_last_media,
    build_recent_dialogue,
    extract_inbound_marker_labels,
    find_candidate_by_hint,
    parse_candidate_choice,
    pick_ranked_candidates,
    run_selector_llm,
    should_force_media_consideration,
)


@dataclass(frozen=True)
class QQFaceReplyPlan:
    entry: QQFaceEntry
    selected_label: str
    marker: str
    reasoning: str
    mode: str = "text_with_face"


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
    "face_only": "face_only",
    "qq_face_only": "face_only",
    "emoji_only": "face_only",
    "face": "face_only",
    "text_with_face": "text_with_face",
    "text+face": "text_with_face",
    "face_with_text": "text_with_face",
}
_TEXT_MODE_HINTS = (
    ("face_only", ("face_only", "只发face", "只发表情")),
    ("text_with_face", ("text_with_face", "文字+face", "文字加face")),
    ("none", ("none", "不发", "纯文字")),
)
_TOKEN_PATTERN = r"[\u4e00-\u9fff]{1,4}|[a-z0-9_#=]+"
_DIGIT_PATTERN = r"(?<!\d)(\d{1,4})(?!\d)"


def _entry_relevance(entry: QQFaceEntry, tokens: Sequence[str]) -> tuple[float, float, float]:
    haystack = " ".join(
        [
            entry.face_id,
            entry.label,
            entry.marker,
            *entry.aliases,
        ]
    ).lower()
    score = 0.0
    for token in tokens:
        if not token:
            continue
        if token == entry.face_id:
            score += 2.5
            continue
        if token in haystack:
            score += 1.6
            continue
        if any(token in alias.lower() or alias.lower() in token for alias in entry.aliases):
            score += 1.0
    if not entry.label.startswith("系统表情#"):
        score += 0.2
    score -= entry.usage_count * 0.05
    if time.time() - entry.last_used_ts < 300:
        score -= 1.0
    return score, -entry.last_used_ts, -float(entry.usage_count)


def _pick_candidate_entries(
    entries: Sequence[QQFaceEntry],
    *,
    user_text: str,
    reply_text: str,
    max_items: int,
) -> list[QQFaceEntry]:
    return pick_ranked_candidates(
        entries,
        user_text=user_text,
        reply_text=reply_text,
        max_items=max_items,
        token_pattern=_TOKEN_PATTERN,
        score_fn=_entry_relevance,
    )


def _render_candidate_block(candidates: Sequence[QQFaceEntry]) -> str:
    lines: list[str] = []
    for index, entry in enumerate(candidates, start=1):
        labels = "、".join(entry.aliases[:4]) if entry.aliases else entry.label
        lines.append(
            f"{index}. id={entry.face_id} | marker={entry.marker} | 标签={labels or '无'} | 最近使用={entry.usage_count}次"
        )
    return "\n".join(lines)


def _find_entry_by_hint(candidates: Sequence[QQFaceEntry], hint: str) -> QQFaceEntry | None:
    return find_candidate_by_hint(
        candidates,
        hint,
        key_fn=lambda entry: (
            entry.face_id,
            entry.label,
            entry.marker,
            *entry.aliases,
        ),
    )


def _parse_face_choice(output: str, candidates: Sequence[QQFaceEntry]) -> tuple[str, QQFaceEntry | None]:
    return parse_candidate_choice(
        output,
        candidates,
        mode_aliases=_MODE_ALIASES,
        text_mode_hints=_TEXT_MODE_HINTS,
        hint_keys=("candidate", "index", "option", "face", "label", "marker", "id"),
        resolve_candidate=lambda hint: _find_entry_by_hint(candidates, hint),
        default_mode="text_with_face",
        digit_pattern=_DIGIT_PATTERN,
    )


def _forced_face_fallback(
    candidates: Sequence[QQFaceEntry],
    inbound_labels: Sequence[str],
) -> QQFaceEntry | None:
    for label in inbound_labels:
        selected = _find_entry_by_hint(candidates, label)
        if selected is not None:
            return selected
    return None


async def plan_qq_face_reply(
    *,
    context,
    runtime,
    history: Sequence[StoredMessage],
    user_text: str,
    reply_text: str,
    secrets: dict[str, str] | dict[str, object],
    chat_id: str = "",
    force_consider: bool = False,
) -> QQFaceReplyPlan | None:
    inbound_labels = extract_inbound_marker_labels(user_text, "qq_face")
    forced = bool(force_consider or should_force_media_consideration(user_text, "qq_face"))
    if chat_id:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.face.plan.start",
            fields={
                "forced": forced,
                "reply_text": reply_text,
                "inbound_marker": "，".join(inbound_labels),
            },
        )

    if not bool(_media_cfg_value(runtime, "enable_outbound_face_reply", False)):
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.face.plan.skip",
                fields={"reason": "disabled"},
            )
        return None
    if not (reply_text or "").strip():
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.face.plan.skip",
                fields={"reason": "empty_reply"},
            )
        return None
    probability = float(_media_cfg_value(runtime, "face_reply_probability", 0.18))
    roll = random.random()
    if not forced and roll > probability:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.face.plan.skip",
                fields={"reason": "probability", "roll": round(roll, 4), "threshold": probability},
            )
        return None

    cooldown_turns = max(0, int(_media_cfg_value(runtime, "face_cooldown_turns", 2)))
    turns_since_last = assistant_turns_since_last_media(history, "qq_face")
    if turns_since_last is not None and turns_since_last < cooldown_turns:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.face.plan.skip",
                fields={
                    "reason": "cooldown",
                    "turns_since_last": turns_since_last,
                    "cooldown_turns": cooldown_turns,
                },
            )
        return None

    entries = await load_qq_face_catalog(context, runtime)
    if not entries:
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.face.plan.skip",
                fields={"reason": "no_entries"},
            )
        return None

    candidate_count = max(1, int(_media_cfg_value(runtime, "face_candidate_count", 8)))
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
                step="reply.face.plan.skip",
                fields={"reason": "no_candidates"},
            )
        return None

    recent_dialogue = build_recent_dialogue(history, context=context, current_text=user_text)
    candidate_block = _render_candidate_block(candidates)
    prompt = (
        "你要决定这次回复要不要带一个 QQ 系统 face，并且判断是只发 face，还是发文字后再补一个 face。"
        '只输出 JSON，不要额外解释。格式: {"mode":"none|face_only|text_with_face","candidate":"候选编号","reason":"..."}。\n'
        f"最近对话\n{recent_dialogue or '（无）'}\n\n"
        f"对方刚发\n{user_text.strip()}\n\n"
        f"你准备回复\n{reply_text.strip()}\n\n"
        f"可选 QQ face 候选\n{candidate_block}\n\n"
        "选择规则："
        "\n1. 如果纯文字已经最自然，或者这些 face 都不够贴切，mode 选 none。"
        "\n2. 如果只发一个 face 更像真人、更有后劲，mode 选 face_only。"
        "\n3. 如果文字已经合适，但补一个 face 能增加语气、打趣、阴阳怪气或收尾效果，mode 选 text_with_face。"
        "\n4. candidate 必须从候选编号里选；如果 mode 是 none，candidate 留空。"
        "\n5. 优先看候选 face 的具体标签和 id 是否贴合语境，不要机械地只看情绪词。"
    )

    output = await run_selector_llm(
        context=context,
        runtime=runtime,
        secrets=secrets,
        system_prompt="你是 QQ face 回复模态选择器，只输出指定 JSON。mode 只能是 none、face_only、text_with_face。",
        user_prompt=prompt,
        chat_func=chat_completions,
    )
    mode = "none"
    entry: QQFaceEntry | None = None
    if output:
        mode, entry = _parse_face_choice(output, candidates)

    if mode == "none" or entry is None:
        fallback_entry = _forced_face_fallback(candidates, inbound_labels) if forced else None
        if fallback_entry is None:
            if chat_id:
                _log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="reply.face.plan.skip",
                    fields={
                        "reason": "selector_none" if output else "empty_selector",
                        "mode": mode or "none",
                    },
                )
            return None
        mode = "text_with_face"
        entry = fallback_entry
        if chat_id:
            _log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="reply.face.plan.fallback",
                fields={"reason": "inbound_marker_exact_match", "face_id": entry.face_id},
            )

    plan = QQFaceReplyPlan(
        entry=entry,
        selected_label=entry.label,
        marker=entry.marker,
        reasoning=f"face_mode:{mode};face_label:{entry.label}",
        mode=mode,
    )
    if chat_id:
        _log_step(
            context,
            runtime,
            chat_id=chat_id,
            step="reply.face.plan.result",
            fields={
                "mode": plan.mode,
                "face_id": plan.entry.face_id,
                "face_marker": plan.marker,
                "forced": forced,
            },
        )
    return plan
