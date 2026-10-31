"""保留模型回复内容，并按自然边界组织发送段落。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from ..config.config import ResponsePostProcessConfig, ResponseSplitterConfig

_RE_MULTI_SPACE      = re.compile(r"[ \t]{2,}")
_SENTENCE_TAIL_CHARS = frozenset("。！？!?～~…")
# 链接、邮箱、代码和列表这类要原样看清的内容不按分句拆气泡。
_CLAUSE_SPLIT_BLOCK_RE = re.compile(
    r"```|https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+|^\s*(?:[-*•]|\d+[.、)])\s*\S", re.M
)
_OPENING_PAIRS = {
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "【": "】",
    "《": "》",
}
_CLAUSE_TRAILING_COMMAS = "，,、"
_BRACKET_SPAN           = 60


def _pairs_nearby(text: str, index: int, closer: str) -> bool:
    """括号或引号在附近配上了才算数；“:(”这类表情和落单的半个括号不算。"""

    span = text[index + 1 : index + 1 + _BRACKET_SPAN].split("\n", 1)[0]
    end  = span.find(closer)
    return end >= 0 and text[index] not in span[:end]


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

        if in_code_block or stripped:
            current_message.append(line)
        elif current_message:
            # 普通文本，遇到空行则拆分
            messages.append("\n".join(current_message).strip())
            current_message = []

    # 处理未闭合的代码块或普通最后一段
    if current_message:
        messages.append("\n".join(current_message).strip())

    return [m for m in messages if m]


def _strip_prefix(text: str, bot_name: str) -> str:
    s = text.strip()
    # 示例标签偶尔会被带入单条回复；完整对话稿保留各人的标签。
    if s.startswith(("你：", "你:")) and not re.search(r"\n\s*[^\n:：]{1,20}[:：]", s):
        return s[2:].strip()
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

    quote_pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    # 书名号和括号里的感叹号、问号不是句末（《孤独摇滚！》）；只在后文确实有配对时才保护。
    bracket_pairs          = {"《": "》", "（": "）", "(": ")", "【": "】"}
    closing_quotes         = set(quote_pairs.values()) | set(bracket_pairs.values())
    quote_stack: list[str] = []
    out        : list[str] = []
    start                  = 0
    index                  = 0
    while index < len(s):
        char         = s[index]
        closed_quote = False
        if char in quote_pairs:
            quote_stack.append(quote_pairs[char])
        elif char in bracket_pairs and _pairs_nearby(s, index, bracket_pairs[char]):
            quote_stack.append(bracket_pairs[char])
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


def _clause_cuts(sentence: str) -> list[tuple[int, int]]:
    """分句位置：逗号之后，以及省略号、波浪号之后；括号与引号里面不切，数字之间的逗号不切。

    返回 (片段结束位置, 下一片段开始位置)；两者之间是被切掉的逗号。
    """

    cuts : list[tuple[int, int]] = []
    stack: list[str]             = []
    index                        = 0
    while index < len(sentence):
        char = sentence[index]
        if char in _OPENING_PAIRS and _pairs_nearby(sentence, index, _OPENING_PAIRS[char]):
            stack.append(_OPENING_PAIRS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in "，,":
            before = sentence[index - 1] if index else ""
            after  = sentence[index + 1] if index + 1 < len(sentence) else ""
            if not (before.isdigit() and after.isdigit()):
                cuts.append((index, index + 1))
        elif not stack and char in "…～~":
            end = index
            while end + 1 < len(sentence) and sentence[end + 1] in "…～~":
                end += 1
            if end + 1 < len(sentence):
                # 省略号和波浪号留在前一个气泡末尾，是语气的一部分。
                cuts.append((end + 1, end + 1))
            index = end
        index += 1
    return cuts


def _split_clauses(sentence: str, max_pieces: int) -> list[str]:
    """把一句按分句拆成至多 ``max_pieces`` 段；相邻两段合起来最短的先合回去。"""

    if max_pieces <= 1:
        return [sentence]
    pieces: list[str] = []
    joins : list[str] = []
    start             = 0
    for end, resume in _clause_cuts(sentence):
        piece = sentence[start:end].strip()
        if piece:
            pieces.append(piece)
            joins.append(sentence[end:resume])
        start = resume
    tail = sentence[start:].strip()
    if tail:
        pieces.append(tail)
    joins = joins[: max(0, len(pieces) - 1)]
    while len(pieces) > max_pieces:
        pair = min(range(len(pieces) - 1), key=lambda i: len(pieces[i]) + len(pieces[i + 1]))
        pieces[pair : pair + 2] = [pieces[pair] + joins[pair] + pieces[pair + 1]]
        del joins[pair]
    return [piece.rstrip(_CLAUSE_TRAILING_COMMAS).strip() for piece in pieces if piece.strip()]


def _spread_clauses(
    sentences: list[str], *, limit: int, min_chars: int, max_chars: int
) -> list[str]:
    """气泡数还没用完时，把较长的句子再按分句拆开，像在 QQ 上一句一句打出来。"""

    if len("".join(sentences)) > max_chars or _CLAUSE_SPLIT_BLOCK_RE.search("\n".join(sentences)):
        return sentences
    result            = list(sentences)
    settled: set[str] = set()
    while len(result) < limit:
        candidates = [
            index
            for index, sentence in enumerate(result)
            if len(sentence) >= min_chars and sentence not in settled
        ]
        if not candidates:
            break
        index = max(candidates, key=lambda i: len(result[i]))
        pieces = _split_clauses(result[index], limit - len(result) + 1)
        if len(pieces) <= 1:
            settled.add(result[index])
            continue
        result[index : index + 1] = pieces
    return result


def process_llm_response(
    response_text: str,
    cfg: ResponsePostProcessConfig,
    *,
    bot_name: str,
    enable_splitter: bool = True,
    clause_split: bool    = True,
) -> list[str]:
    """清理回复并分成气泡。

    先按句末标点分句；气泡数没用完时再按逗号、省略号把较长的句子拆开（``clause_split``，
    对方要求了句数或篇幅时由调用方关闭）。超出气泡数的内容收进最后一条，正文不截断。
    """
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
        if clause_split and splitter.clause_split:
            sentences = _spread_clauses(
                sentences,
                limit     = limit,
                min_chars = splitter.clause_split_min_chars,
                max_chars = splitter.clause_split_max_chars,
            )
        if len(sentences) > limit:
            sentences = sentences[: limit - 1] + ["\n".join(sentences[limit - 1 :])]
        return sentences

    return [text]


def _without_blank_lines(part: str) -> str:
    """去掉代码块外的空行：气泡边界只由分段结果决定，不由正文里的空行决定。"""

    kept: list[str] = []
    in_code_block   = False
    for line in part.split("\n"):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
        if in_code_block or line.strip() or line.strip().startswith("```"):
            kept.append(line)
    return "\n".join(kept).strip()


def join_reply(parts: Sequence[str]) -> str:
    """把分段结果合成一段文本，段与段之间用空行分隔。

    最终发送按空行（并保护代码块）拆成气泡，所以分段边界必须是空行、段内不能再有空行：
    关闭分段时整段回复仍是一个气泡，开启分句时每句各成一个气泡。
    """

    cleaned = [_without_blank_lines(p) for p in parts if p and p.strip()]
    return "\n\n".join(part for part in cleaned if part).strip()
