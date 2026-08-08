"""提供有界参数解析、加权抽样和不重复抽样的随机选择插件。"""

from __future__ import annotations

import random
from typing import Any

from core.args import tokenize
from core.interfaces import PluginContextProtocol
from core.plugin_base import Segments, segments
from core.public_errors import public_error_response

MIN_OPTIONS = 2
MAX_OPTIONS = 50
MAX_CHOICES = 10
DEFAULT_CHOICES = 1
MAX_ARGUMENT_CHARS = 4_096
MAX_QUESTION_CHARS = 100
MAX_OPTION_CHARS = 200

CHOICE_EMOJIS = ("🎲", "🎯", "✨", "🌟", "💫", "🎰", "🔮", "🎪")
_HELP_WORDS = frozenset({"help", "帮助"})
_RNG = random.SystemRandom()

HELP_TEXT = """
🎲 随机选择助手

用法
/选择 <问题> <选项1> <选项2> ...
/选择 <问题> <选项1> <选项2> -n 3
/选择 <问题> <选项1> <选项2> -u

问题或选项含空格时请使用引号，例如：
/选择 "今天吃什么" "ice cream" 火锅

规则
• 默认有放回；重复选项会增加该文本的权重
• -u / --unique：按文本去重后不放回抽样
• -n：只能指定一次，数量为 1–10 的 ASCII 整数
• --：其后的 -n、-u 等文本会被当作普通选项

问题最长 100 字，单个选项最长 200 字，支持 2–50 个选项。
""".strip()


class ChoiceArgumentError(ValueError):
    """可安全返回给用户的选择参数错误。"""


def _is_bounded_text(value: object, max_chars: int) -> bool:
    return (
        isinstance(value, str) and bool(value) and len(value) <= max_chars and value.isprintable()
    )


def _parse_choice_tokens(tokens: list[str]) -> tuple[list[str], int, bool]:
    """扫描位置参数及 `-n`、`-u`、`--` 状态，不再执行第二次分词。"""
    positional: list[str] = []
    choice_count = DEFAULT_CHOICES
    count_seen = False
    unique = False
    parse_flags = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if parse_flags and token == "--":
            parse_flags = False
            index += 1
            continue
        if parse_flags and token in {"-u", "--unique"}:
            unique = True
            index += 1
            continue
        if parse_flags and token == "-n":
            if count_seen:
                raise ChoiceArgumentError("-n 只能指定一次")
            if index + 1 >= len(tokens):
                raise ChoiceArgumentError("-n 后必须提供选择数量")
            raw_count = tokens[index + 1]
            if not raw_count.isascii() or not raw_count.isdecimal() or len(raw_count) > 2:
                raise ChoiceArgumentError("选择数量必须是 1–10 的 ASCII 整数")
            choice_count = int(raw_count)
            count_seen = True
            index += 2
            continue
        if parse_flags and token.startswith("-"):
            raise ChoiceArgumentError("存在不支持的命令选项；如需选择该文本，请先使用 --")
        positional.append(token.strip())
        index += 1
    return positional, choice_count, unique


def parse_choice_args(args: object) -> tuple[str | None, list[str], int, bool]:
    """一次性解析命令参数；空参数或帮助命令返回空请求。"""
    if not isinstance(args, str):
        raise ChoiceArgumentError("参数必须是文本")
    if len(args) > MAX_ARGUMENT_CHARS:
        raise ChoiceArgumentError(f"参数过长，最多支持 {MAX_ARGUMENT_CHARS} 个字符")
    try:
        tokens = tokenize(args, strict=True)
    except ValueError as exc:
        raise ChoiceArgumentError("参数中的引号未正确闭合") from exc
    if not tokens or (len(tokens) == 1 and tokens[0].casefold() in _HELP_WORDS):
        return None, [], DEFAULT_CHOICES, False

    positional, choice_count, unique = _parse_choice_tokens(tokens)
    if not positional:
        raise ChoiceArgumentError("请提供问题和至少两个选项")
    question = positional[0]
    if not _is_bounded_text(question, MAX_QUESTION_CHARS):
        raise ChoiceArgumentError(f"问题必须为 1–{MAX_QUESTION_CHARS} 个可显示字符")
    return question, positional[1:], choice_count, unique


def make_choice(
    options: list[str], count: int = DEFAULT_CHOICES, unique: bool = False
) -> list[str]:
    """按请求执行有放回或按文本去重后的不放回抽样。"""
    if not isinstance(options, list):
        raise ChoiceArgumentError("选项必须是列表")
    if len(options) < MIN_OPTIONS:
        raise ChoiceArgumentError(f"至少需要 {MIN_OPTIONS} 个选项")
    if len(options) > MAX_OPTIONS:
        raise ChoiceArgumentError(f"选项过多，最多支持 {MAX_OPTIONS} 个选项")
    if any(not _is_bounded_text(option, MAX_OPTION_CHARS) for option in options):
        raise ChoiceArgumentError(f"每个选项必须为 1–{MAX_OPTION_CHARS} 个可显示字符")
    if type(count) is not int or not 1 <= count <= MAX_CHOICES:
        raise ChoiceArgumentError(f"选择数量必须是 1–{MAX_CHOICES} 的整数")
    if type(unique) is not bool:
        raise ChoiceArgumentError("unique 必须是布尔值")

    if not unique:
        # 重复文本保留为多个候选位置，因此可以自然表达权重。
        return list(_RNG.choices(options, k=count))

    distinct_options = list(dict.fromkeys(options))
    if count > len(distinct_options):
        raise ChoiceArgumentError(
            f"去重模式下，选择数量不能超过不同选项数量（{count} > {len(distinct_options)}）"
        )
    return list(_RNG.sample(distinct_options, k=count))


def format_choice_result(question: str, choices: list[str], total_options: int) -> str:
    """把已验证的抽样结果格式化为一段消息。"""
    emoji = _RNG.choice(CHOICE_EMOJIS)
    if len(choices) == 1:
        return f"{emoji} {question}：{choices[0]}"

    lines = [f"{emoji} {question}："]
    lines.extend(f"  {index}. {choice}" for index, choice in enumerate(choices, 1))
    lines.append(f"\n已从 {total_options} 个选项中选择 {len(choices)} 个")
    return "\n".join(lines)


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """插件命令入口；统一入口形参保留，正文只回显已验证的文本。"""
    try:
        question, options, choice_count, unique = parse_choice_args(args)
        if question is None:
            return segments(HELP_TEXT)

        choices = make_choice(options, choice_count, unique)
        distinct_count = len(dict.fromkeys(options))
        context.logger.info(
            "随机选择：问题长度=%d，候选位置=%d，不同选项=%d，选择数=%d，去重=%s",
            len(question),
            len(options),
            distinct_count,
            choice_count,
            unique,
        )
        total_options = distinct_count if unique else len(options)
        result = format_choice_result(question, choices, total_options)
        context.logger.debug("随机选择完成：结果数=%d", len(choices))
        return segments(result)
    except ChoiceArgumentError as exc:
        return segments(f"❌ {exc}")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=context.logger,
            component="choice.handle",
        )
