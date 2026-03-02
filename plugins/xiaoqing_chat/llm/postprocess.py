from __future__ import annotations

import random
import re
from typing import Sequence

from ..config.config import ChineseTypoConfig, ResponsePostProcessConfig, ResponseSplitterConfig

_RE_CHINESE_BRACKETS = re.compile(r"（[^）]*）")
_RE_ASCII_PARENS = re.compile(r"\([^)]*\)")
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")

def _strip_brackets(text: str) -> str:
    out = _RE_CHINESE_BRACKETS.sub("", text)
    out = _RE_ASCII_PARENS.sub("", out)
    return out

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
    # 将 LLM 输出的字面 \n（两字符转义序列）转换为真正的换行符
    s = s.replace("\\n", "\n")
    s = _RE_MULTI_SPACE.sub(" ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

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

_HOMOGLYPH_REPLACE = {
    "你": ["妳", "妮"],
    "的": ["得", "地"],
    "吗": ["嘛"],
    "吧": ["叭"],
    "这": ["這"],
    # "那" → "哪" removed: semantically different ("那个人" vs "哪个人")
}

def _apply_chinese_typo(text: str, cfg: ChineseTypoConfig) -> str:
    if not cfg.enable:
        return text
    if not text:
        return text
    out_chars = list(text)
    for i, ch in enumerate(out_chars):
        if ch in _HOMOGLYPH_REPLACE and random.random() < cfg.word_replace_rate:
            out_chars[i] = random.choice(_HOMOGLYPH_REPLACE[ch])
    return "".join(out_chars)

def process_llm_response(
    response_text: str,
    cfg: ResponsePostProcessConfig,
    *,
    bot_name: str,
    enable_splitter: bool = True,
    enable_chinese_typo: bool = True,
) -> list[str]:
    text = response_text or ""
    if cfg.enable_response_post_process:
        text = _strip_brackets(text)
        text = _strip_prefix(text, bot_name=bot_name)
        text = text.strip().strip('"').strip("'").strip()
        text = _normalize(text)

    if not text:
        return []

    splitter: ResponseSplitterConfig = cfg.splitter
    typo_cfg: ChineseTypoConfig = cfg.chinese_typo

    if splitter.enable and enable_splitter:
        sentences = _split_sentences(text)
        out: list[str] = []
        for s in sentences:
            s = _truncate(s, splitter.max_length)
            if s:
                out.append(s)
            if len(out) >= splitter.max_sentence_num:
                break
        if enable_chinese_typo and typo_cfg.enable:
            return [_apply_chinese_typo(s, typo_cfg) for s in out]
        return out

    text = _truncate(text, splitter.max_length)
    if enable_chinese_typo and typo_cfg.enable:
        text = _apply_chinese_typo(text, typo_cfg)
    return [text] if text else []

def join_reply(parts: Sequence[str]) -> str:
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return "\n".join(cleaned).strip()
