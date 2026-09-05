"""
插件基础模块

提供插件开发所需的通用工具函数和类型定义。
所有插件应该从这个模块导入基础功能。
"""

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar, cast, overload

from .atomic_store import AtomicJsonStore, atomic_write_bytes, atomic_write_text
from .constants import MAX_MESSAGE_TEXT_LENGTH
from .interfaces import PluginContextProtocol
from .plugin_execution import offload_plugin_sync

# 类型别名
Segments = list[dict[str, Any]]
Event    = dict[str, Any]
T        = TypeVar("T")

logger = logging.getLogger(__name__)

_EXTERNAL_ANSI_PATTERN    = re.compile(r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~])")
_EXTERNAL_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def has_control_characters(
    value: str,
    *,
    allow_formatting_whitespace: bool = False,
    include_c1: bool                  = False,
) -> bool:
    """判断文本是否含有调用方策略禁止的 C0、DEL 或 C1 控制字符。

    普通命令和 HTTP 字段默认拒绝 C0 与 DEL；允许正文排版时可放行制表、换行和回车，
    需要覆盖完整不可见控制区时再显式启用 C1，避免各插件复制略有差异的 ``ord`` 判定。
    """

    allowed = "\t\n\r" if allow_formatting_whitespace else ""
    for character in value:
        codepoint = ord(character)
        if codepoint < 32 and character not in allowed:
            return True
        if codepoint == 127 or include_c1 and 128 <= codepoint <= 159:
            return True
    return False


def _utf8_prefix(value: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if len(value[:middle].encode("utf-8")) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return value[:low]


def _normalize_external_text(value: str, *, strip_ansi: bool, strip: bool) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if strip_ansi:
        normalized = _EXTERNAL_ANSI_PATTERN.sub("", normalized)
    normalized = _EXTERNAL_CONTROL_PATTERN.sub(" ", normalized)
    normalized = normalized.encode("utf-8", errors="replace").decode("utf-8")
    return normalized.strip() if strip else normalized


def bounded_external_text(
    value: object,
    *,
    max_chars: int,
    max_bytes: int,
    default: str     = "",
    suffix: str      = "…",
    strip_ansi: bool = True,
    strip: bool      = True,
    truncate: bool   = True,
) -> str:
    """Turn an untrusted scalar into visible text under character and UTF-8 budgets.

    Containers, booleans and non-finite floats are rejected instead of stringified.
    ANSI terminal sequences and C0/C1 controls are removed before either budget is
    measured. Set ``truncate=False`` for protocol fields where a partial value would
    be misleading; those fields fall back to ``default`` instead.
    """

    for name, limit in (("max_chars", max_chars), ("max_bytes", max_bytes)):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(default, str) or not isinstance(suffix, str):
        raise TypeError("default and suffix must be strings")

    fallback = _normalize_external_text(default, strip_ansi=strip_ansi, strip=strip)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        candidate = fallback
    elif isinstance(value, float) and not math.isfinite(value):
        candidate = fallback
    else:
        candidate = _normalize_external_text(str(value), strip_ansi=strip_ansi, strip=strip)
        if not candidate:
            candidate = fallback

    if len(candidate) <= max_chars and len(candidate.encode("utf-8")) <= max_bytes:
        return candidate
    if not truncate:
        candidate = fallback
        if len(candidate) <= max_chars and len(candidate.encode("utf-8")) <= max_bytes:
            return candidate

    bounded_suffix = _normalize_external_text(suffix, strip_ansi=True, strip=False)
    bounded_suffix = _utf8_prefix(bounded_suffix[:max_chars], max_bytes)
    char_budget    = max(0, max_chars - len(bounded_suffix))
    byte_budget    = max(0, max_bytes - len(bounded_suffix.encode("utf-8")))
    prefix         = _utf8_prefix(candidate[:char_budget], byte_budget)
    return prefix + bounded_suffix


def head_tail_preview(text: str, max_chars: int, *, marker: str) -> str:
    """Keep a bounded text preview with a marker between its head and tail."""

    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    remaining = max_chars - len(marker)
    if remaining <= 0:
        return text[:max_chars]
    head = (remaining + 1) // 2
    tail = remaining - head
    return text[:head] + marker + (text[-tail:] if tail else "")


# ============================================================
# 消息段构建
# ============================================================


def text(content: str) -> dict[str, Any]:
    """创建文本消息段"""
    return {"type": "text", "data": {"text": content}}


def _to_file_uri(file_path: str) -> str:
    """将本地文件路径转换为标准 file:// URI"""
    p = Path(file_path)
    if not p.is_absolute():
        p = p.resolve()
    return p.as_uri()


def image(file_path: str) -> dict[str, Any]:
    """创建图片消息段（本地文件）"""
    return {"type": "image", "data": {"file": _to_file_uri(file_path)}}


def emoji(file_path: str, *, summary: str = "") -> dict[str, Any]:
    """创建表情图片消息段。

    内部使用显式 `emoji` 段，发送到 OneBot 时再降级为兼容的 `image`
    + `sub_type=emoji`，避免在插件内部把表情包和普通图片折叠成同一语义。
    """
    data: dict[str, Any] = {"file": _to_file_uri(file_path)}
    summary_text         = str(summary or "").strip()
    if summary_text:
        data["summary"] = summary_text
    return {"type": "emoji", "data": data}


def image_url(url: str) -> dict[str, Any]:
    """创建图片消息段（网络URL）"""
    return {"type": "image", "data": {"file": url}}


def face(face_id: str | int) -> dict[str, Any]:
    """创建 QQ face 消息段"""
    return {"type": "face", "data": {"id": str(face_id)}}


def record(file_path: str) -> dict[str, Any]:
    """创建语音消息段（本地文件）"""
    return {"type": "record", "data": {"file": _to_file_uri(file_path)}}


def record_url(url: str) -> dict[str, Any]:
    """创建语音消息段（网络URL）"""
    return {"type": "record", "data": {"file": url}}


def segments(payload: Any) -> Segments:
    """
    将任意返回值转换为标准消息段列表。

    支持:
    - str: 转换为单个文本消息段
    - list: 直接返回
    - None: 返回空列表
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        return [text(payload)]
    return []


def build_action(
    segs: Segments, user_id: int | None, group_id: int | None
) -> dict[str, Any] | None:
    """
    构建 OneBot action。

    根据 group_id 或 user_id 决定发送群消息还是私聊消息。
    """
    if not segs:
        return None
    if group_id is not None:
        return {"action": "send_group_msg", "params": {"group_id": group_id, "message": segs}}
    if user_id is not None:
        return {"action": "send_private_msg", "params": {"user_id": user_id, "message": segs}}
    return None


# ============================================================
# 异步工具
# ============================================================


async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    通过当前插件的有界执行 bulkhead 运行同步阻塞函数。

    用于包装 requests 等同步库的调用，避免阻塞事件循环，同时保留
    每插件并发/排队上限、跨插件公平性和卸载隔离语义。
    """
    return cast(T, await offload_plugin_sync(func, *args, **kwargs))


async def gather_bounded(
    awaitables: Iterable[Awaitable[T]],
    *,
    limit: int,
) -> list[T]:
    """并发等待一组任务，同时把实际运行数限制在 ``limit`` 内。

    调用方仍然得到与 ``asyncio.gather`` 相同的输入顺序和异常传播语义；
    这个 helper 只收敛插件反复实现的信号量包装，不负责吞掉或重排业务错误。
    """

    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")

    semaphore = asyncio.Semaphore(limit)

    async def bounded(awaitable: Awaitable[T]) -> T:
        async with semaphore:
            return await awaitable

    results = await asyncio.gather(*(bounded(awaitable) for awaitable in awaitables))
    return list(results)


# ============================================================
# 文件工具
# ============================================================


def ensure_dir(path: Path) -> None:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)


@overload
def load_json(
    path: Path,
    default: None = None,
    *,
    raise_on_error: bool = False,
) -> dict[str, Any]: ...


@overload
def load_json(
    path: Path,
    default: T,
    *,
    raise_on_error: bool = False,
) -> T: ...


def load_json(
    path: Path,
    default: T | None = None,
    *,
    raise_on_error: bool = False,
) -> T | dict[str, Any]:
    """加载 JSON 文件"""
    fallback: T | dict[str, Any] = default if default is not None else {}
    try:
        return cast(
            T | dict[str, Any],
            AtomicJsonStore(path).read(fallback, raise_on_error=raise_on_error),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("Failed to parse JSON %s: %s", path, exc)
        if raise_on_error:
            raise
        return fallback


def write_json(path: Path, data: Any) -> None:
    """写入 JSON 文件"""
    AtomicJsonStore(path).write(data)


# ============================================================
# 消息分段（长消息自动拆分）
# ============================================================


def split_message_segments(
    segs: Segments,
    max_length: int = MAX_MESSAGE_TEXT_LENGTH,
) -> list[Segments]:
    """将过长文本拆成有界消息，同时保留混合消息段的顺序和唯一性。

    文本优先在当前长度范围内的最后一个换行符后切分，找不到换行符时按字符
    强制切分。图片、语音等非文本段不会复制或丢弃，而是按原顺序附着在相邻
    文本块中。所有文本拼回后与输入严格一致，包括边界处的换行符。

    Args:
        segs: 原始消息段列表
        max_length: 单条消息最大文本长度

    Returns:
        拆分后的消息段列表的列表
    """
    if type(max_length) is not int or max_length <= 0:
        raise ValueError("max_length must be a positive integer")
    if not segs:
        return [segs]

    total_len = sum(
        len(text)
        for seg in segs
        if isinstance(seg, dict)
        and seg.get("type") == "text"
        and isinstance(text := seg.get("data", {}).get("text"), str)
    )
    if total_len <= max_length:
        return [segs]

    chunks: list[Segments] = []
    current: Segments      = []
    current_text_length    = 0

    def flush() -> None:
        nonlocal current, current_text_length
        if current:
            chunks.append(current)
        current             = []
        current_text_length = 0

    for segment in segs:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            current.append(segment)
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            current.append(segment)
            continue
        text = data.get("text")
        if not isinstance(text, str):
            current.append(segment)
            continue
        if not text:
            current.append(segment)
            continue

        remaining = text
        while remaining:
            if current_text_length >= max_length:
                flush()
            available = max_length - current_text_length
            if len(remaining) <= available:
                split_at = len(remaining)
            else:
                newline  = remaining.rfind("\n", 0, available)
                split_at = newline + 1 if newline >= 0 else available

            piece               = remaining[:split_at]
            cloned              = dict(segment)
            cloned_data         = dict(data)
            cloned_data["text"] = piece
            cloned["data"]      = cloned_data
            current.append(cloned)
            current_text_length += len(piece)
            remaining = remaining[split_at:]
            if remaining:
                flush()

    flush()
    return chunks or [segs]


__all__ = [
    # 消息段
    "text",
    "image",
    "emoji",
    "image_url",
    "face",
    "record",
    "record_url",
    "segments",
    "build_action",
    "split_message_segments",
    "head_tail_preview",
    # 异步工具
    "run_sync",
    # 文件工具
    "ensure_dir",
    "load_json",
    "atomic_write_bytes",
    "atomic_write_text",
    "write_json",
    # 类型
    "Segments",
    "Event",
    "PluginContextProtocol",
]
