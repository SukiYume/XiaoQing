from __future__ import annotations

import json
import random
import re
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from ..helper_utils import _resolve_llm_config
from ..llm.llm_client import chat_completions
from ..llm.prompt_builder import build_dialogue_prompt
from ..media_registry import message_has_media_kind
from ..memory.memory import StoredMessage

T = TypeVar("T")

_QQ_FACE_MARKER_RE = re.compile(r"\[QQ表情：([^\]]+)\]")
_EMOJI_MARKER_RE = re.compile(r"\[表情包：([^\]]+)\]")


def assistant_turns_since_last_media(
    history: Sequence[StoredMessage],
    *media_kinds: str,
) -> int | None:
    turns = 0
    for message in reversed(history):
        if message.role != "assistant":
            continue
        if message_has_media_kind(message, *media_kinds):
            return turns
        turns += 1
    return None


def extract_choice_json(output: str) -> dict[str, Any]:
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


def normalize_mode(value: str, aliases: dict[str, str]) -> str:
    lowered = str(value or "").strip().lower()
    return aliases.get(lowered, "")


def tokenize_media_text(value: str, *, pattern: str) -> list[str]:
    raw = str(value or "").lower()
    if not raw:
        return []
    tokens = re.findall(pattern, raw)
    ordered: list[str] = []
    for token in tokens:
        cleaned = token.strip()
        if len(cleaned) <= 1 and not re.search(r"[\u4e00-\u9fff]", cleaned):
            continue
        if cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def extract_inbound_marker_labels(value: str, media_kind: str) -> list[str]:
    text = str(value or "")
    if not text:
        return []
    pattern = _QQ_FACE_MARKER_RE if media_kind == "qq_face" else _EMOJI_MARKER_RE
    labels: list[str] = []
    for match in pattern.finditer(text):
        label = str(match.group(1) or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def should_force_media_consideration(value: str, media_kind: str) -> bool:
    return bool(extract_inbound_marker_labels(value, media_kind))


def pick_ranked_candidates(
    entries: Sequence[T],
    *,
    user_text: str,
    reply_text: str,
    max_items: int,
    token_pattern: str,
    score_fn: Callable[[T, Sequence[str]], tuple[float, float, float]],
) -> list[T]:
    if not entries or max_items <= 0:
        return []
    tokens = tokenize_media_text(f"{user_text}\n{reply_text}", pattern=token_pattern)
    scored = sorted(
        entries,
        key=lambda item: score_fn(item, tokens),
        reverse=True,
    )
    if tokens:
        return scored[:max_items]
    shuffled = list(entries)
    random.shuffle(shuffled)
    return shuffled[:max_items]


def find_candidate_by_hint(
    candidates: Sequence[T],
    hint: str,
    *,
    key_fn: Callable[[T], Sequence[str]],
) -> T | None:
    normalized_hint = str(hint or "").strip()
    if not normalized_hint:
        return None

    lowered_hint = normalized_hint.lower()
    for entry in candidates:
        keys = [str(value or "").strip() for value in key_fn(entry)]
        if any(lowered_hint == key.lower() for key in keys if key):
            return entry

    if normalized_hint.isdigit():
        idx = int(normalized_hint)
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]

    compact_hint = re.sub(r"\s+", "", lowered_hint)
    for entry in candidates:
        for key in key_fn(entry):
            candidate_text = re.sub(r"\s+", "", str(key or "").strip().lower())
            if candidate_text and (compact_hint == candidate_text or compact_hint in candidate_text):
                return entry
    return None


def parse_candidate_choice(
    output: str,
    candidates: Sequence[T],
    *,
    mode_aliases: dict[str, str],
    text_mode_hints: Sequence[tuple[str, Sequence[str]]],
    hint_keys: Sequence[str],
    resolve_candidate: Callable[[str], T | None],
    default_mode: str,
    digit_pattern: str,
) -> tuple[str, T | None]:
    payload = extract_choice_json(output)
    mode = normalize_mode(str(payload.get("mode", "") or ""), mode_aliases)

    text = (output or "").strip()
    lowered = text.lower()
    if not mode:
        for resolved_mode, hints in text_mode_hints:
            if any(hint in lowered or hint in text for hint in hints):
                mode = resolved_mode
                break

    for key in hint_keys:
        selected = resolve_candidate(str(payload.get(key, "") or ""))
        if selected is not None:
            return mode or default_mode, selected

    digit_match = re.search(digit_pattern, text)
    if digit_match:
        selected = resolve_candidate(digit_match.group(1))
        if selected is not None:
            return mode or default_mode, selected

    return mode or "none", None


def build_recent_dialogue(
    history: Sequence[StoredMessage],
    *,
    context,
    max_items: int = 6,
    max_chars: int = 500,
) -> str:
    return build_dialogue_prompt(
        history[-max_items:],
        bot_name=(context.config or {}).get("bot_name", "小青"),
        truncate=False,
        max_chars=max_chars,
    )


async def run_selector_llm(
    *,
    context,
    runtime,
    secrets: dict[str, str] | dict[str, object],
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 64,
    chat_func: Callable[..., Awaitable[str]] | None = None,
) -> str | None:
    api_base = str(secrets.get("api_base", "") or "")
    api_key = str(secrets.get("api_key", "") or "")
    model = str(secrets.get("model", "") or "")
    if not api_base or not api_key or not model:
        return None

    llm_cfg = _resolve_llm_config(runtime.cfg, secrets, foreground=False)
    invoke = chat_func or chat_completions
    return await invoke(
        session=context.http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        top_p=float(getattr(runtime.cfg, "top_p", 0.9)),
        max_tokens=max_tokens,
        timeout_seconds=float(llm_cfg.timeout_seconds),
        max_retry=int(llm_cfg.max_retry),
        retry_interval_seconds=float(llm_cfg.retry_interval_seconds),
        proxy=str(llm_cfg.proxy or ""),
        endpoint_path=str(llm_cfg.endpoint_path or runtime.cfg.endpoint_path),
    )
