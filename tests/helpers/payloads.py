"""跨插件测试共享的图片与消息载荷构造器。"""

from __future__ import annotations

import io
import struct
import zlib
from types import SimpleNamespace


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """构造带长度和 CRC 的单个 PNG 数据块。"""

    crc = zlib.crc32(data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def image_bytes(
    image_format: str = "PNG",
    color: tuple[int, int, int] = (1, 2, 3),
) -> bytes:
    """生成一个最小 RGB 图片，供格式识别和下载边界测试使用。"""

    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format=image_format)
    return output.getvalue()


def text_reply_draft(text: str) -> SimpleNamespace:
    """按 xiaoqing_chat 当前回复草稿契约构造纯文本载荷。"""

    from plugins.xiaoqing_chat.message_parts import build_text_message_parts

    return SimpleNamespace(text=text, parts=build_text_message_parts(text))
