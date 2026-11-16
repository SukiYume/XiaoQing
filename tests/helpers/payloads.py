"""跨插件测试共享的图片与消息载荷构造器。"""

from __future__ import annotations

import io
import struct
import zlib


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """构造带长度和 CRC 的单个 PNG 数据块。"""

    crc = zlib.crc32(data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def valid_png(width: int = 1, height: int = 1) -> bytes:
    """构造结构和 CRC 完整的最小 RGBA PNG，避免把残缺文件当作测试夹具。"""

    header   = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanline = (b"\x00" + b"\x00\x00\x00\x00" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(scanline))
        + png_chunk(b"IEND", b"")
    )


def image_bytes(
    image_format: str           = "PNG",
    color: tuple[int, int, int] = (1, 2, 3),
) -> bytes:
    """生成一个最小 RGB 图片，供格式识别和下载边界测试使用。"""

    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format=image_format)
    return output.getvalue()
