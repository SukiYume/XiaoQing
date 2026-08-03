from __future__ import annotations

import re
from collections.abc import Sequence

from ..config.config import ResponsePostProcessConfig, ResponseSplitterConfig

_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def _strip_prefix(text: str, bot_name: str) -> str:
    s = text.strip()
    if bot_name:
        for sep in (":", "：", "-", "—"):
            prefix = f"{bot_name}{sep}"
            if s.startswith(prefix):
                s = s[len(prefix) :].strip()
                break
    return s


def _normalize(text: str) -> str:
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks = s.split("```")
    for index in range(0, len(chunks), 2):
        chunks[index] = _RE_MULTI_SPACE.sub(" ", chunks[index])
        chunks[index] = re.sub(r"\n{3,}", "\n\n", chunks[index])
    return "```".join(chunks).strip()


def _truncate(text: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    if max_length == 1:
        return "…"
    return text[: max_length - 1].rstrip() + "…"


def _split_sentences(text: str) -> list[str]:
    s = text.strip()
    if not s:
        return []

    quote_pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    closing_quotes = set(quote_pairs.values())
    quote_stack: list[str] = []
    out: list[str] = []
    start = 0
    index = 0
    while index < len(s):
        char = s[index]
        closed_quote = False
        if char in quote_pairs:
            quote_stack.append(quote_pairs[char])
        elif quote_stack and char == quote_stack[-1]:
            quote_stack.pop()
            closed_quote = True
        elif char == '"':
            if quote_stack and quote_stack[-1] == '"':
                quote_stack.pop()
                closed_quote = True
            else:
                quote_stack.append('"')

        boundary = char in "。！？!?" and not quote_stack
        if closed_quote and not quote_stack and index > 0 and s[index - 1] in "。！？!?":
            boundary = True
        if not boundary:
            index += 1
            continue

        end = index + 1
        while end < len(s) and s[end] in closing_quotes | {'"'}:
            end += 1
        sentence = s[start:end].strip()
        if sentence:
            out.append(sentence)
        start = end
        index = end

    remainder = s[start:].strip()
    if remainder:
        out.append(remainder)
    return out or [s]


def process_llm_response(
    response_text: str,
    cfg: ResponsePostProcessConfig,
    *,
    bot_name: str,
    enable_splitter: bool = True,
) -> list[str]:
    text = response_text or ""
    if cfg.enable_response_post_process:
        text = _strip_prefix(text, bot_name=bot_name)
        text = text.strip().strip('"').strip("'").strip()
        text = _normalize(text)

    if not text:
        return []

    splitter: ResponseSplitterConfig = cfg.splitter
    if splitter.enable and enable_splitter:
        sentences = _split_sentences(text)
        out: list[str] = []
        for s in sentences:
            s = _truncate(s, splitter.max_length)
            if s:
                out.append(s)
            if len(out) >= splitter.max_sentence_num:
                break
        return out

    text = _truncate(text, splitter.max_length)
    return [text] if text else []


def join_reply(parts: Sequence[str]) -> str:
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return "\n".join(cleaned).strip()
