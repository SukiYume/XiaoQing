"""加载、验证并原子维护内置与会话作用域颜色数据。"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict, TypeVar, cast

from core.interfaces import PluginContextProtocol
from core.plugin_base import ensure_dir, has_control_characters, load_json, run_sync, write_json
from core.public_errors import public_error_message

from .convert import hex_to_rgb, validate_cmyk, validate_rgb

MAX_CUSTOM_COLORS_PER_SCOPE  = 200
MAX_CUSTOM_COLOR_FILE_BYTES  = 256 * 1024
MAX_BUILTIN_COLORS           = 1_000
MAX_BUILTIN_COLOR_FILE_BYTES = 2 * 1024 * 1024
MAX_COLOR_NAME_CHARS         = 64
MAX_PINYIN_CHARS             = 128
MAX_SCOPE_ID                 = 2**63 - 1
_HEX_PATTERN                 = re.compile(r"#[0-9a-fA-F]{6}")
_CUSTOM_COLORS_LOCK          = threading.RLock()
_MutationResult              = TypeVar("_MutationResult")


class ColorRecord(TypedDict):
    name: str
    pinyin: str
    RGB: list[int]
    hex: str
    CMYK: list[int]


def _positive_scope_id(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= MAX_SCOPE_ID:
        raise ValueError(f"{label} must be a positive 64-bit integer")
    return value


def _custom_file(context: PluginContextProtocol) -> Path:
    """从核心已认证的当前身份派生独立的会话作用域文件名。"""

    data_dir = getattr(context, "data_dir", None)
    if not isinstance(data_dir, Path):
        raise ValueError("color data_dir must be a Path")
    group_id = _positive_scope_id(getattr(context, "current_group_id", None), label="group_id")
    if group_id is not None:
        return data_dir / f"custom_colors_group_{group_id}.json"
    user_id = _positive_scope_id(getattr(context, "current_user_id", None), label="user_id")
    if user_id is None:
        raise ValueError("color custom storage requires a group or user scope")
    return data_dir / f"custom_colors_private_{user_id}.json"


def _clean_text(value: Any, *, label: str, max_chars: int, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    cleaned = value.strip()
    if (not cleaned and not allow_empty) or len(cleaned) > max_chars:
        raise ValueError(f"{label} is empty or exceeds its length budget")
    if has_control_characters(cleaned):
        raise ValueError(f"{label} contains control characters")
    return cleaned


def _normalize_color_record(value: Any) -> ColorRecord:
    if not isinstance(value, Mapping):
        raise ValueError("color record must be an object")
    name = _clean_text(
        value.get("name"),
        label       = "color name",
        max_chars   = MAX_COLOR_NAME_CHARS,
        allow_empty = False,
    )
    pinyin = _clean_text(
        value.get("pinyin", ""),
        label       = "color pinyin",
        max_chars   = MAX_PINYIN_CHARS,
        allow_empty = True,
    )
    rgb_value  = value.get("RGB")
    cmyk_value = value.get("CMYK")
    if not isinstance(rgb_value, list) or not validate_rgb(rgb_value)[0]:
        raise ValueError("color record has invalid RGB channels")
    if not isinstance(cmyk_value, list) or not validate_cmyk(cmyk_value)[0]:
        raise ValueError("color record has invalid CMYK channels")
    hex_value = value.get("hex")
    if not isinstance(hex_value, str) or _HEX_PATTERN.fullmatch(hex_value) is None:
        raise ValueError("color record has invalid HEX value")
    normalized_hex = hex_value.casefold()
    if hex_to_rgb(normalized_hex) != rgb_value:
        raise ValueError("color record RGB and HEX disagree")
    return {
        "name": name,
        "pinyin": pinyin,
        "RGB": list(rgb_value),
        "hex": normalized_hex,
        "CMYK": list(cmyk_value),
    }


def _bounded_json_file(path: Path, *, max_bytes: int) -> Any:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError("color data path must be a regular non-link file")
    if path.stat().st_size > max_bytes:
        raise ValueError("color data file exceeds byte budget")
    return load_json(path, [], raise_on_error=True)


def _normalize_color_list(value: Any, *, max_items: int) -> list[ColorRecord]:
    if not isinstance(value, list):
        raise ValueError("color data must contain an array")
    if len(value) > max_items:
        raise ValueError("color count exceeds item budget")
    colors = [_normalize_color_record(item) for item in value]
    names  = [color["name"] for color in colors]
    if len(names) != len(set(names)):
        raise ValueError("color names must be unique within one library")
    return colors


def _encoded_custom_colors(colors: list[ColorRecord]) -> bytes:
    if len(colors) > MAX_CUSTOM_COLORS_PER_SCOPE:
        raise ValueError("custom color count exceeds scope budget")
    payload = json.dumps(colors, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_CUSTOM_COLOR_FILE_BYTES:
        raise ValueError("custom color file exceeds byte budget")
    return payload


@lru_cache(maxsize=4)
def _load_builtin_colors_cached(
    color_file: str,
    mtime_ns: int,
    size: int,
) -> tuple[ColorRecord, ...]:
    del mtime_ns, size  # 两项只参与缓存身份，读取逻辑使用已经授权的固定路径。
    data = _bounded_json_file(Path(color_file), max_bytes=MAX_BUILTIN_COLOR_FILE_BYTES)
    return tuple(_normalize_color_list(data, max_items=MAX_BUILTIN_COLORS))


def _load_builtin_colors(context: PluginContextProtocol) -> tuple[ColorRecord, ...]:
    plugin_dir = getattr(context, "plugin_dir", None)
    if not isinstance(plugin_dir, Path):
        raise ValueError("color plugin_dir must be a Path")
    builtin_file = plugin_dir / "color.json"
    info         = builtin_file.stat()
    return _load_builtin_colors_cached(str(builtin_file), info.st_mtime_ns, info.st_size)


def _read_custom_colors(custom_file: Path) -> list[ColorRecord]:
    data = _bounded_json_file(custom_file, max_bytes=MAX_CUSTOM_COLOR_FILE_BYTES)
    return _normalize_color_list(data, max_items=MAX_CUSTOM_COLORS_PER_SCOPE)


def load_colors(context: PluginContextProtocol) -> list[ColorRecord]:
    """加载内置库，并在当前身份作用域可用时追加自定义颜色。"""

    colors: list[ColorRecord] = []
    try:
        builtin = _load_builtin_colors(context)
        colors.extend(builtin)
        context.logger.debug("加载内置颜色库: count=%d", len(builtin))
    except Exception as exc:
        public_error_message(context, exc, logger=context.logger, component="color.load_builtin")
        return []

    try:
        custom = load_custom_colors(context)
        colors.extend(custom)
        context.logger.debug("加载自定义颜色: count=%d", len(custom))
    except Exception as exc:
        public_error_message(context, exc, logger=context.logger, component="color.load_custom")
    return colors


async def load_colors_async(context: PluginContextProtocol) -> list[ColorRecord]:
    """通过当前插件的有界执行池加载内置库和作用域自定义颜色。"""

    return cast(list[ColorRecord], await run_sync(load_colors, context))


def load_custom_colors(context: PluginContextProtocol) -> list[ColorRecord]:
    """在进程内锁保护下加载当前会话或私聊作用域的自定义颜色。"""

    custom_file = _custom_file(context)
    with _CUSTOM_COLORS_LOCK:
        return _read_custom_colors(custom_file)


def mutate_custom_colors(
    context: PluginContextProtocol,
    callback: Callable[[list[ColorRecord]], _MutationResult],
) -> _MutationResult:
    """原子读取、修改并仅在内容变化时写回当前作用域颜色库。"""

    custom_file = _custom_file(context)
    with _CUSTOM_COLORS_LOCK:
        colors   = _read_custom_colors(custom_file)
        original = copy.deepcopy(colors)
        result   = callback(colors)
        normalized = _normalize_color_list(colors, max_items=MAX_CUSTOM_COLORS_PER_SCOPE)
        _encoded_custom_colors(normalized)
        if normalized != original:
            ensure_dir(custom_file.parent)
            write_json(custom_file, normalized)
        return result


async def mutate_custom_colors_async(
    context: PluginContextProtocol,
    callback: Callable[[list[ColorRecord]], _MutationResult],
) -> _MutationResult:
    """在有界工作线程中保持完整的读取、修改、验证和写回事务。"""

    return cast(_MutationResult, await run_sync(mutate_custom_colors, context, callback))


def format_color_info(color: ColorRecord) -> str:
    """按稳定字段顺序生成用户可读的颜色信息。"""

    lines = [f"名称：{color['name']}"]
    if pinyin := color.get("pinyin", ""):
        lines.append(f"拼音：{pinyin}")
    lines.extend(
        (
            f"RGB：{', '.join(str(channel) for channel in color['RGB'])}",
            f"HEX：{color['hex']}",
            f"CMYK：{', '.join(str(channel) for channel in color['CMYK'])}",
        )
    )
    return "\n".join(lines)
