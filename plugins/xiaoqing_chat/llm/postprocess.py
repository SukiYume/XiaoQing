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
                s = s[len(prefix):].strip()
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
    parts = re.split(r"(?<=[。！？!?])", s)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out or ([s] if s else [])

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
