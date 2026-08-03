"""提供基础闲聊、分域问答和可选语音合成。"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, cast

from core.args import parse
from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import ensure_dir as _core_ensure_dir
from core.plugin_base import load_json as _core_load_json
from core.plugin_base import segments as _core_segments
from core.plugin_base import write_json as _core_write_json
from core.public_errors import public_error_message
from core.public_errors import public_error_response as _core_public_error_response

MessageSegment = dict[str, Any]
MessageSegments = list[MessageSegment]
OneBotEvent = dict[str, Any]


class _ChatReplyProvider(Protocol):
    async def reply(self, text: str, event: OneBotEvent) -> MessageSegments: ...


class _VoiceSynthesisProvider(Protocol):
    async def synthesize_text(self, text: str) -> MessageSegments | None: ...


class _Capabilities(Protocol):
    chat_reply: _ChatReplyProvider | None
    voice_synthesis: _VoiceSynthesisProvider | None


class Context(Protocol):
    """本插件实际读取的最小运行时上下文。"""

    data_dir: Path
    current_user_id: int | None
    current_group_id: int | None
    capabilities: _Capabilities

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...


segments = cast(Callable[[object], MessageSegments], _core_segments)
_ensure_dir = cast(Callable[[Path], None], _core_ensure_dir)
_load_json = cast(Callable[[Path, object], object], _core_load_json)
_write_json = cast(Callable[[Path, object], None], _core_write_json)
_public_error_response = cast(Callable[..., MessageSegments], _core_public_error_response)

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 2_000
MAX_ANSWERS_PER_QUESTION = 20
MAX_QUESTION_LENGTH = 128
MAX_ANSWER_LENGTH = 1_000
MAX_AUDIT_ENTRIES = 5_000
MAX_CUSTOM_RESPONSES = 200
MAX_RANDOM_RESPONSE_LENGTH = 1_000
MAX_QA_REPLY_LENGTH = 2_800
MAX_VOICE_TEXT_LENGTH = 3_000
DEFAULT_VOICE_PROBABILITY = 0.2

DEFAULT_RESPONSES = (
    "叫我干嘛",
    "嗯嗯，我就是小青",
    "我是小青，叫我有什么事情吗？",
    "在的在的",
    "嗯？",
    "有什么事吗？",
    "我在~",
    "叫我干啥",
    "干嘛干嘛~",
)

_HELP_ALIASES = {"help", "帮助", "?"}
_HELP_TEXTS = {
    "qa": (
        "💬 问答添加\n"
        "/记忆 <问题> <回答>\n"
        "/记住 <问题> <回答>\n"
        "/学习 <问题> <回答>\n\n"
        "问题是第一个非空白字段；回答可以包含空格。\n"
        "同一问题可以有多个回答，命中时随机选择。"
    ),
    "qa_list": ("📜 问答查询\n/对话 - 列出当前会话的问题\n/对话 <问题> - 精确查询一个问题的回答"),
    "qa_remove": (
        "🗑️ 问答删除\n"
        "/删除对话 <问题> - 删除问题及全部回答\n"
        "/删除对话 <问题> <回答> - 只删除指定回答"
    ),
}
_MISSING = object()


@dataclass
class _QaSnapshot:
    path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    data: dict[str, list[str]] | None = None


@dataclass
class _AuditSnapshot:
    path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    entries: list[dict[str, Any]] | None = None


@lru_cache(maxsize=2_048)
def _qa_snapshot(path_text: str) -> _QaSnapshot:
    """按会话文件共享锁与热数据，避免并发命令各自读写同一份旧快照。"""

    return _QaSnapshot(Path(path_text))


@lru_cache(maxsize=64)
def _audit_snapshot(path_text: str) -> _AuditSnapshot:
    """按审计文件共享串行写入状态；小上限覆盖活跃数据目录且约束常驻内存。"""

    return _AuditSnapshot(Path(path_text))


def init(context: Context | None = None) -> None:
    """记录插件初始化完成。"""

    logger.info("Smalltalk plugin initialized")


def _normalize_responses(value: object) -> list[str]:
    """清洗自定义随机回复，避免畸形 JSON 进入随机选择。"""

    if not isinstance(value, list):
        return []
    responses: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or len(text) > MAX_RANDOM_RESPONSE_LENGTH or text in seen:
            continue
        responses.append(text)
        seen.add(text)
        if len(responses) == MAX_CUSTOM_RESPONSES:
            break
    return responses


def _load_responses(context: Context) -> list[str]:
    """按优先级读取两种兼容格式，均无有效值时使用内置回复。"""

    for filename, key in (("小青.json", "小青"), ("responses.json", "responses")):
        path = context.data_dir / filename
        if not path.exists():
            continue
        payload = _load_json(path, {})
        if not isinstance(payload, Mapping):
            continue
        responses = _normalize_responses(payload.get(key))
        if responses:
            return responses
    return list(DEFAULT_RESPONSES)


def call_bot_name_only(context: Context) -> MessageSegments:
    """只喊机器人名字时，从有效回复中随机返回一条。"""

    return segments(random.choice(_load_responses(context)))


def _positive_id(value: object) -> int | None:
    """把运行时 ID 收窄为安全的正整数。"""

    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and len(value) <= 20 and value.isdecimal():
        normalized = int(value)
        return normalized if normalized > 0 else None
    return None


def _qa_scope(context: Context) -> str:
    group_id = _positive_id(getattr(context, "current_group_id", None))
    if group_id is not None:
        return f"group_{group_id}"
    user_id = _positive_id(getattr(context, "current_user_id", None))
    if user_id is not None:
        return f"private_{user_id}"
    return "legacy"


def _qa_file(context: Context) -> Path:
    scope = _qa_scope(context)
    filename = "QA.json" if scope == "legacy" else f"QA_{scope}.json"
    return context.data_dir / filename


def _normalize_answers(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    answers: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        answer = item.strip()
        if not answer or len(answer) > MAX_ANSWER_LENGTH or answer in seen:
            continue
        answers.append(answer)
        seen.add(answer)
        if len(answers) == MAX_ANSWERS_PER_QUESTION:
            break
    return answers


def _normalize_qa(value: object) -> dict[str, list[str]]:
    """把持久化内容收窄为有配额、可安全随机选择的问答表。"""

    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, list[str]] = {}
    for raw_question, raw_answers in value.items():
        if not isinstance(raw_question, str):
            continue
        question = raw_question.strip()
        if not question or len(question) > MAX_QUESTION_LENGTH:
            continue
        answers = _normalize_answers(raw_answers)
        if not answers:
            continue
        existing = normalized.get(question)
        if existing is not None:
            for answer in answers:
                if answer not in existing and len(existing) < MAX_ANSWERS_PER_QUESTION:
                    existing.append(answer)
            continue
        if len(normalized) == MAX_QUESTIONS:
            break
        normalized[question] = answers
    return normalized


def _qa_snapshot_for(context: Context) -> _QaSnapshot:
    return _qa_snapshot(str(_qa_file(context).absolute()))


def _audit_snapshot_for(context: Context) -> _AuditSnapshot:
    return _audit_snapshot(str((context.data_dir / "QA_audit.json").absolute()))


def _read_qa_file(path: Path) -> dict[str, list[str]]:
    return _normalize_qa(_load_json(path, {}))


def _write_qa_file(path: Path, data: Mapping[str, Sequence[str]]) -> None:
    _ensure_dir(path.parent)
    _write_json(path, data)


def _read_audit_file(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path, {})
    raw_entries = payload.get("entries") if isinstance(payload, Mapping) else None
    if not isinstance(raw_entries, list):
        return []
    return [dict(item) for item in raw_entries if isinstance(item, Mapping)]


async def _qa_data_locked(snapshot: _QaSnapshot) -> dict[str, list[str]]:
    """在调用方持有 snapshot 锁时惰性加载一次，防止并发首读覆盖新写入。"""

    if snapshot.data is None:
        snapshot.data = await asyncio.to_thread(_read_qa_file, snapshot.path)
    return snapshot.data


async def _load_qa(context: Context) -> dict[str, list[str]]:
    """返回当前会话内存快照的独立副本；首次读取才在线程中访问磁盘。"""

    snapshot = _qa_snapshot_for(context)
    async with snapshot.lock:
        data = await _qa_data_locked(snapshot)
        return {question: list(answers) for question, answers in data.items()}


async def _record_qa_audit(context: Context, operation: str, question: str) -> None:
    """从内存快照追加有界审计，并在线程中持久化；故障不回滚主数据。"""

    try:
        snapshot = _audit_snapshot_for(context)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "scope": _qa_scope(context),
            "owner": _positive_id(getattr(context, "current_user_id", None)),
            "question": question,
        }
        async with snapshot.lock:
            if snapshot.entries is None:
                snapshot.entries = await asyncio.to_thread(_read_audit_file, snapshot.path)
            keep = max(0, MAX_AUDIT_ENTRIES - 1)
            entries = snapshot.entries[-keep:] if keep else []
            entries = [*entries, entry]
            await asyncio.to_thread(
                _write_json,
                snapshot.path,
                {"entries": entries},
            )
            snapshot.entries = entries
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="smalltalk.qa_audit",
        )


async def get_qa_answer(context: Context, question: str) -> str | None:
    """从内存快照精确匹配问题，并随机选择一个非空回答。"""

    snapshot = _qa_snapshot_for(context)
    async with snapshot.lock:
        answers = (await _qa_data_locked(snapshot)).get(question)
        return random.choice(answers) if answers else None


def _bounded_lines(header: str, lines: Sequence[str]) -> str:
    """在 QQ 单条文本预算内拼接列表，并报告省略数量。"""

    output = header
    shown = 0
    for line in lines:
        candidate = f"{output}\n{line}"
        remaining = len(lines) - shown - 1
        suffix = f"\n… 还有 {remaining} 条未显示" if remaining else ""
        if len(candidate) + len(suffix) > MAX_QA_REPLY_LENGTH:
            break
        output = candidate
        shown += 1
    omitted = len(lines) - shown
    suffix = f"\n… 还有 {omitted} 条未显示" if omitted else ""
    return output + suffix


async def _add_qa(context: Context, args: str) -> MessageSegments:
    """校验并原子更新一个问题的回答集合。"""

    parts = args.split(None, 1)
    if len(parts) < 2:
        return segments("格式: 记忆 问题 回答")
    question, answer = parts[0].strip(), parts[1].strip()
    if not question or len(question) > MAX_QUESTION_LENGTH:
        return segments(f"问题不能为空且不能超过 {MAX_QUESTION_LENGTH} 个字符。")
    if not answer or len(answer) > MAX_ANSWER_LENGTH:
        return segments(f"回答不能为空且不能超过 {MAX_ANSWER_LENGTH} 个字符。")

    snapshot = _qa_snapshot_for(context)
    async with snapshot.lock:
        current = await _qa_data_locked(snapshot)
        data = {item: list(answers) for item, answers in current.items()}
        answers = data.get(question)
        if answers is not None:
            if answer in answers:
                return segments("这个我已经知道了。")
            if len(answers) == MAX_ANSWERS_PER_QUESTION:
                return segments("这个问题的回答数量已达上限。")
            answers.append(answer)
        else:
            if len(data) == MAX_QUESTIONS:
                return segments("当前会话的问答库已达上限。")
            data[question] = [answer]
        await asyncio.to_thread(_write_qa_file, snapshot.path, data)
        snapshot.data = data
    await _record_qa_audit(context, "add", question)
    return segments("对话添加成功了！")


async def _list_qa(context: Context, args: str) -> MessageSegments:
    """列出当前会话的问题，或精确查询一个问题。"""

    data = await _load_qa(context)
    question = args.strip()
    if question:
        answers = data.get(question)
        if answers is None:
            return segments("没有这个问题的回答")
        return segments(_bounded_lines(f"{question}：", answers))
    if not data:
        return segments("还没有任何问答对")
    return segments(_bounded_lines("问答列表：", list(data)))


async def _remove_qa(context: Context, args: str) -> MessageSegments:
    """删除指定回答，或删除问题及其全部回答。"""

    parts = args.split(None, 1)
    if not parts:
        return segments("要删除哪个对话？格式: 删除对话 问题 [回答]")
    question = parts[0].strip()
    answer = parts[1].strip() if len(parts) > 1 else ""

    snapshot = _qa_snapshot_for(context)
    operation: str
    result: MessageSegments
    async with snapshot.lock:
        current = await _qa_data_locked(snapshot)
        data = {item: list(answers) for item, answers in current.items()}
        answers = data.get(question)
        if answers is None:
            return segments("似乎没有这个对话呢")
        if answer:
            if answer not in answers:
                return segments("没有这个回答")
            answers.remove(answer)
            if not answers:
                del data[question]
            operation = "remove_answer"
            result = segments(f"对话“{question}”的指定回答已删除。")
        else:
            removed_count = len(data.pop(question))
            operation = "remove_question"
            result = segments(f"对话“{question}”及其 {removed_count} 个回答已删除。")
        await asyncio.to_thread(_write_qa_file, snapshot.path, data)
        snapshot.data = data
    await _record_qa_audit(context, operation, question)
    return result


async def handle(
    command: str,
    args: str,
    event: OneBotEvent,
    context: Context,
) -> MessageSegments:
    """处理管理员 QA 命令；权限由清单和 core 路由统一执行。"""

    try:
        parsed = parse(args)
        if command not in _HELP_TEXTS:
            return segments("未知命令")
        if (
            parsed
            and len(parsed) == 1
            and not parsed.options
            and parsed.first.casefold() in _HELP_ALIASES
        ):
            return segments(_HELP_TEXTS[command])
        if command == "qa":
            return await _add_qa(context, args)
        if command == "qa_list":
            return await _list_qa(context, args)
        return await _remove_qa(context, args)
    except Exception as exc:
        return _public_error_response(
            context,
            exc,
            logger=logger,
            component="smalltalk.handle",
        )


def _voice_probability(context: Context) -> float:
    """读取 0..1 概率；缺省沿用 20%，畸形显式配置则安全禁用。"""

    plugin_config = context.get_settings_snapshot().plugin_config("smalltalk")
    value = plugin_config.get("voice_probability", _MISSING)
    if value is _MISSING:
        return DEFAULT_VOICE_PROBABILITY
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("Invalid smalltalk voice_probability; voice conversion disabled")
        return 0.0
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        logger.warning("Invalid smalltalk voice_probability; voice conversion disabled")
        return 0.0
    return probability


def _coerce_segments(value: object) -> MessageSegments | None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return cast(MessageSegments, value)


def _voice_text(reply: Sequence[MessageSegment]) -> str | None:
    """提取有界纯文本；内容过长时不向语音 provider 提交截断文本。"""

    parts: list[str] = []
    length = 0
    for segment in reply:
        if segment.get("type") != "text":
            return None
        data = segment.get("data")
        text = data.get("text") if isinstance(data, Mapping) else None
        if not isinstance(text, str):
            continue
        length += len(text)
        if length > MAX_VOICE_TEXT_LENGTH:
            return None
        parts.append(text)
    combined = "".join(parts)
    return combined if combined.strip() else None


async def _maybe_convert_to_voice(
    reply: MessageSegments,
    context: Context,
) -> MessageSegments:
    """按配置尝试语音合成，任何不可用或失败都保留原文字。"""

    probability = _voice_probability(context)
    if probability <= 0.0 or random.random() >= probability:
        return reply
    capabilities = getattr(context, "capabilities", None)
    provider = getattr(capabilities, "voice_synthesis", None)
    if provider is None:
        logger.debug("Voice synthesis provider unavailable")
        return reply
    text_content = _voice_text(reply)
    if text_content is None:
        return reply
    try:
        voice_reply = _coerce_segments(await provider.synthesize_text(text_content))
        if voice_reply:
            logger.info("Converted smalltalk response to voice")
            return voice_reply
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="smalltalk.voice",
        )
    return reply


async def _call_chat_api(text_content: str, context: Context) -> MessageSegments:
    """通过 core 签发的能力调用 chat，并验证 provider 返回消息段。"""

    try:
        capabilities = getattr(context, "capabilities", None)
        provider = getattr(capabilities, "chat_reply", None)
        if provider is None:
            raise RuntimeError("chat reply provider unavailable")
        actor = {
            "user_id": _positive_id(getattr(context, "current_user_id", None)),
            "group_id": _positive_id(getattr(context, "current_group_id", None)),
        }
        reply = _coerce_segments(await provider.reply(text_content, actor))
        if reply is None:
            raise RuntimeError("chat reply provider returned invalid segments")
        return reply
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="smalltalk.chat",
        )
        return segments("暂时无法回复，请稍后再试~")


async def handle_smalltalk(
    text_content: str,
    event: OneBotEvent,
    context: Context,
) -> MessageSegments:
    """优先精确匹配 QA，未命中时转交 chat 能力。"""

    if not text_content.strip():
        return []
    answer = await get_qa_answer(context, text_content)
    reply = segments(answer) if answer is not None else await _call_chat_api(text_content, context)
    return await _maybe_convert_to_voice(reply, context)
