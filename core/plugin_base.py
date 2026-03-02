"""
插件基础模块

提供插件开发所需的通用工具函数和类型定义。
所有插件应该从这个模块导入基础功能。
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from .constants import MAX_MESSAGE_TEXT_LENGTH
from .interfaces import PluginContextProtocol

# 类型别名
Segments = list[dict[str, Any]]
Event = dict[str, Any]
T = TypeVar("T")

logger = logging.getLogger(__name__)

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

def image_url(url: str) -> dict[str, Any]:
    """创建图片消息段（网络URL）"""
    return {"type": "image", "data": {"file": url}}

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

def build_action(segs: Segments, user_id: Optional[int], group_id: Optional[int]) -> Optional[dict[str, Any]]:
    """
    构建 OneBot action。

    根据 group_id 或 user_id 决定发送群消息还是私聊消息。
    """
    if not segs:
        return None
    if group_id:
        return {"action": "send_group_msg", "params": {"group_id": group_id, "message": segs}}
    if user_id:
        return {"action": "send_private_msg", "params": {"user_id": user_id, "message": segs}}
    return None

# ============================================================
# 异步工具
# ============================================================

async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    在线程池中运行同步阻塞函数。

    用于包装 requests 等同步库的调用，避免阻塞事件循环。
    """
    return await asyncio.to_thread(func, *args, **kwargs)

# ============================================================
# 文件工具
# ============================================================

def ensure_dir(path: Path) -> None:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)

def load_json(path: Path, default: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """加载 JSON 文件"""
    if not path.exists():
        return default or {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON %s: %s", path, exc)
        return default or {}

def atomic_write_text(path: Path, payload: str) -> None:
    ensure_dir(path.parent)
    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass  # cleanup 失败不掩盖原始异常
        raise

def write_json(path: Path, data: dict[str, Any]) -> None:
    """写入 JSON 文件"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(path, payload)

# ============================================================
# 消息分段（长消息自动拆分）
# ============================================================

def _total_text_length(segs: Segments) -> int:
    """计算消息段列表中所有文本的总长度"""
    total = 0
    for seg in segs:
        if isinstance(seg, dict) and seg.get("type") == "text":
            total += len(seg.get("data", {}).get("text", ""))
    return total


def _is_text_only(segs: Segments) -> bool:
    """判断消息段列表是否只包含文本段"""
    return all(
        isinstance(seg, dict) and seg.get("type") == "text"
        for seg in segs
    )


def split_message_segments(
    segs: Segments,
    max_length: int = MAX_MESSAGE_TEXT_LENGTH,
) -> list[Segments]:
    """
    将过长的消息段列表拆分为多个消息段列表。

    规则:
    - 只对纯文本消息进行拆分（包含图片等混合内容不拆分）
    - 优先按换行符拆分，保持输出可读性
    - 单行超长时按字符强制拆分

    Args:
        segs: 原始消息段列表
        max_length: 单条消息最大文本长度

    Returns:
        拆分后的消息段列表的列表
    """
    if not segs:
        return [segs]

    total_len = _total_text_length(segs)

    # 未超长或包含非文本段，不拆分
    if total_len <= max_length or not _is_text_only(segs):
        return [segs]

    # 合并所有文本
    full_text = "".join(
        seg.get("data", {}).get("text", "")
        for seg in segs
        if isinstance(seg, dict) and seg.get("type") == "text"
    )

    # 按换行符拆分
    lines = full_text.split("\n")
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0

    for line in lines:
        line_with_nl = line + "\n"
        line_len = len(line_with_nl)

        # 单行超长 → 先提交当前块，然后按字符强制拆分这一行
        if line_len > max_length:
            if current_chunk:
                chunks.append("".join(current_chunk).rstrip("\n"))
                current_chunk = []
                current_length = 0
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
            continue

        # 加上这行会超长 → 提交当前块
        if current_length + line_len > max_length:
            chunks.append("".join(current_chunk).rstrip("\n"))
            current_chunk = [line_with_nl]
            current_length = line_len
        else:
            current_chunk.append(line_with_nl)
            current_length += line_len

    # 剩余内容
    if current_chunk:
        chunks.append("".join(current_chunk).rstrip("\n"))

    # 转为消息段列表
    return [segments(chunk) for chunk in chunks if chunk]

__all__ = [
    # 消息段
    "text",
    "image",
    "image_url",
    "record",
    "record_url",
    "segments",
    "build_action",
    "split_message_segments",
    # 异步工具
    "run_sync",
    # 文件工具
    "ensure_dir",
    "load_json",
    "atomic_write_text",
    "write_json",
    # 类型
    "Segments",
    "Event",
    "PluginContextProtocol",
]
