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
from .qq_face_catalog import QQFaceEntry, load_qq_face_catalog, select_qq_face_for_labels


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


def _assistant_turns_since_last_face(history: Sequence[StoredMessage]) -> int | None:
    turns = 0
    for message in reversed(history):
        if message.role != "assistant":
            continue
        if "[QQ表情：" in (message.content or ""):
            return turns
        turns += 1
    return None


def _candidate_labels(entries: Sequence[QQFaceEntry], *, max_labels: int = 12) -> list[str]:
    labels: list[str] = []
    for entry in entries:
        for label in entry.aliases:
            cleaned = str(label or "").strip()
            if cleaned and cleaned not in labels:
                labels.append(cleaned)
            if len(labels) >= max_labels:
                return labels
    return labels


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
        "face_only": "face_only",
        "qq_face_only": "face_only",
        "emoji_only": "face_only",
        "face": "face_only",
        "text_with_face": "text_with_face",
        "text+face": "text_with_face",
        "face_with_text": "text_with_face",
    }
    return aliases.get(lowered, "")


def _parse_face_choice(output: str, candidates: Sequence[str]) -> tuple[str, str]:
    payload = _extract_choice_json(output)
    mode = _normalize_mode(str(payload.get("mode", "") or ""))
    label_hint = str(payload.get("face", "") or payload.get("label", "") or "").strip()

    text = (output or "").strip()
    lowered = text.lower()
    if not mode:
        if "face_only" in lowered or "只发face" in text or "只发表情" in text:
            mode = "face_only"
        elif "text_with_face" in lowered or "文字+face" in text or "文字加face" in text:
            mode = "text_with_face"
        elif "none" in lowered or "不发" in text or "纯文字" in text:
            mode = "none"

    selected = ""
    search_text = label_hint or text
    for candidate in candidates:
        if candidate and candidate in search_text:
            selected = candidate
            break
    if not mode:
        mode = "text_with_face" if selected else "none"
    return mode, selected


async def plan_qq_face_reply(
    *,
    context,
    runtime,
    history: Sequence[StoredMessage],
    user_text: str,
    reply_text: str,
    secrets: dict[str, str] | dict[str, object],
) -> QQFaceReplyPlan | None:
    if not bool(_media_cfg_value(runtime, "enable_outbound_face_reply", False)):
        return None
    if not (reply_text or "").strip():
        return None
    if random.random() > float(_media_cfg_value(runtime, "face_reply_probability", 0.18)):
        return None

    cooldown_turns = max(0, int(_media_cfg_value(runtime, "face_cooldown_turns", 2)))
    turns_since_last = _assistant_turns_since_last_face(history)
    if turns_since_last is not None and turns_since_last < cooldown_turns:
        return None

    entries = await load_qq_face_catalog(context, runtime)
    if not entries:
        return None

    candidate_count = max(1, int(_media_cfg_value(runtime, "face_candidate_count", 8)))
    if len(entries) > candidate_count:
        candidates = random.sample(entries, candidate_count)
    else:
        candidates = list(entries)

    labels = _candidate_labels(candidates)
    if not labels:
        return None

    api_base = str(secrets.get("api_base", "") or "")
    api_key = str(secrets.get("api_key", "") or "")
    model = str(secrets.get("model", "") or "")
    if not api_base or not api_key or not model:
        return None

    llm_cfg = _resolve_llm_config(runtime.cfg, secrets, foreground=False)
    recent_dialogue = build_dialogue_prompt(
        history[-6:],
        bot_name=(context.config or {}).get("bot_name", "小青"),
        truncate=False,
        max_chars=500,
    )
    prompt = (
        "你要决定这次回复是否要补一个 QQ 系统 face 表情，以及是只发 face 还是文字后再发 face。"
        '只输出 JSON，不要额外解释。格式: {"mode":"none|face_only|text_with_face","face":"...","reason":"..."}。\n'
        f"最近对话\n{recent_dialogue or '（无）'}\n\n"
        f"对方刚发\n{user_text.strip()}\n\n"
        f"你准备回复\n{reply_text.strip()}\n\n"
        "可选 QQ face\n"
        + "、".join(labels)
        + "\n\n选择规则："
        "\n1. 如果纯文字就够了，mode 选 none，face 留空。"
        "\n2. 如果只发一个 QQ face 更像真人，mode 选 face_only。"
        "\n3. 如果先发文字再补一个 QQ face 更自然，mode 选 text_with_face。"
        "\n4. face 必须从候选里选。"
    )

    output = await chat_completions(
        session=context.http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[
            {
                "role": "system",
                "content": "你是 QQ face 回复模态选择器，只输出指定 JSON。mode 只能是 none、face_only、text_with_face。",
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

    mode, selected_label = _parse_face_choice(output, labels)
    if mode == "none" or not selected_label:
        return None

    entry = select_qq_face_for_labels(entries, [selected_label])
    if entry is None:
        return None

    return QQFaceReplyPlan(
        entry=entry,
        selected_label=selected_label,
        marker=entry.marker,
        reasoning=f"face_mode:{mode};face_label:{selected_label}",
        mode=mode,
    )
