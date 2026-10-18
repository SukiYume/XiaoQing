"""保留模型回复内容，并按自然边界组织发送段落。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from ..config.config import ResponsePostProcessConfig, ResponseSplitterConfig

_RE_MULTI_SPACE      = re.compile(r"[ \t]{2,}")
_SENTENCE_TAIL_CHARS = frozenset("。！？!?～~…")


def split_chat_reply(text: str) -> list[str]:
    """
    将聊天回复按连贯文本块拆分成多条消息，同时保护代码块不被拆分

    Args:
        text: 完整的聊天回复文本

    Returns:
        拆分后的消息列表
    """
    if not text:
        return []

    lines           = text.split("\n")
    messages        = []
    current_message = []
    in_code_block   = False

    for line in lines:
        stripped = line.strip()

        # 处理代码块标记
        if stripped.startswith("```"):
            if in_code_block:
                # 代码块结束
                current_message.append(line)
                messages.append("\n".join(current_message).strip())
                current_message = []
                in_code_block   = False
            else:
                # 代码块开始
                if current_message:
                    messages.append("\n".join(current_message).strip())
                    current_message = []
                in_code_block = True
                current_message.append(line)
            continue

        if in_code_block:
            current_message.append(line)
        else:
            # 普通文本，遇到空行则拆分
            if not stripped:
                if current_message:
                    messages.append("\n".join(current_message).strip())
                    current_message = []
            else:
                current_message.append(line)

    # 处理未闭合的代码块或普通最后一段
    if current_message:
        messages.append("\n".join(current_message).strip())

    return [m for m in messages if m]


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
    s      = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks = s.split("```")
    for index in range(0, len(chunks), 2):
        chunks[index] = _RE_MULTI_SPACE.sub(" ", chunks[index])
        chunks[index] = re.sub(r"\n{3,}", "\n\n", chunks[index])
    return "```".join(chunks).strip()


def split_sentences(text: str) -> list[str]:
    s = text.strip()
    if not s:
        return []
    if "```" in s:
        return split_chat_reply(s)

    quote_pairs            = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    closing_quotes         = set(quote_pairs.values())
    quote_stack: list[str] = []
    out: list[str]         = []
    start                  = 0
    index                  = 0
    while index < len(s):
        char         = s[index]
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
        # 连用的句末标点和语气尾巴属于同一句，例如“！！！”“？！”“。～”。
        while end < len(s) and s[end] in _SENTENCE_TAIL_CHARS | closing_quotes | {'"'}:
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
    text            = response_text or ""
    structured_text = (
        _strip_prefix(text, bot_name=bot_name) if cfg.enable_response_post_process else text.strip()
    )
    if structured_text.startswith(("{", "[")):
        try:
            json.loads(structured_text)
        except ValueError:
            pass
        else:
            return [structured_text]
    if cfg.enable_response_post_process:
        text = _strip_prefix(text, bot_name=bot_name)
        text = text.strip().strip('"').strip("'").strip()
        text = _normalize(text)

    if not text:
        return []

    splitter: ResponseSplitterConfig = cfg.splitter
    if splitter.enable and enable_splitter:
        sentences = []
        for sentence in split_sentences(text):
            while "```" not in sentence and len(sentence) > splitter.max_length:
                boundary = max(sentence.rfind(mark, 0, splitter.max_length) for mark in "，,；;\n ")
                if boundary < splitter.max_length // 2:
                    break
                sentences.append(sentence[: boundary + 1].strip())
                sentence = sentence[boundary + 1 :].strip()
            if sentence:
                sentences.append(sentence)
        # 分段数量限制消息气泡数；剩余内容完整收进最后一条。
        limit = max(1, splitter.max_sentence_num)
        if len(sentences) > limit:
            sentences = sentences[: limit - 1] + ["\n".join(sentences[limit - 1 :])]
        return sentences

    return [text]


def join_reply(parts: Sequence[str]) -> str:
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return "\n".join(cleaned).strip()
