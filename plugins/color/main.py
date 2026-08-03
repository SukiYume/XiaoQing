"""路由中国传统色、会话自定义色和恒星光谱色查询。"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from core.args import FLAG_VALUE, ParsedArgs, parse
from core.plugin_base import has_control_characters, image, segments, text
from core.public_errors import public_error_response

from . import convert, data_manager, image_gen, query, stellar
from .data_manager import ColorRecord

logger = logging.getLogger(__name__)
Messages = list[dict[str, Any]]
ColorAction = Literal[
    "help",
    "name",
    "rgb",
    "hex",
    "cmyk",
    "keyword",
    "write",
    "delete",
    "stellar",
    "spectype",
]

MAX_ARGS_CHARS = 2_048
MAX_QUERY_CHARS = 64
MAX_DEFINITION_CHARS = 512
MAX_SEARCH_RESULTS = 20
MAX_CUSTOM_COLORS_PER_SCOPE = data_manager.MAX_CUSTOM_COLORS_PER_SCOPE
MAX_CUSTOM_COLOR_NAME_LENGTH = data_manager.MAX_COLOR_NAME_CHARS

_PRIMARY_OPTIONS: dict[ColorAction, tuple[str, ...]] = {
    "help": ("h", "help", "l", "list"),
    "name": ("n", "name"),
    "rgb": ("r", "rgb"),
    "hex": ("x", "hex"),
    "cmyk": ("c", "cmyk"),
    "keyword": ("a", "accord"),
    "write": ("w", "write"),
    "delete": ("d", "delete"),
    "stellar": ("s", "stellar"),
    "spectype": ("t", "spectype"),
}
_PICTURE_OPTIONS = ("p", "picture")
_HELP_WORDS = frozenset({"help", "h", "list", "l", "帮助"})
_PICTURE_ACTIONS = frozenset({"name", "rgb", "hex", "cmyk"})
_REST_ACTIONS = frozenset({"rgb", "cmyk", "write"})
_ASCII_INTEGER = re.compile(r"-?[0-9]+")

HELP_TEXT = """
🎨 **中国传统色彩查询**

**基础查询:**
• /color -n <名称> [-p] - 按名称查询，可选色卡
• /color -r <R,G,B> [-p] - 按 RGB 查询
• /color -x <HEX> [-p] - 按 HEX 查询
• /color -c <C,M,Y,K> [-p] - 按 CMYK 查询
• /color -a <关键词> - 按名称子串搜索

**自定义颜色（仅 Bot 全局管理员）:**
• /color -w <名称> <R> <G> <B>
• /color -w <名称> <#HEX>
• /color -d <名称>

**恒星颜色:**
• /color -s <光谱型> - 查询恒星颜色
• /color -t [前缀] - 列出不重复的光谱型

输入 /color help 查看此帮助
""".strip()


class ColorInputError(ValueError):
    """可以直接安全返回给用户的颜色命令错误。"""


@dataclass(frozen=True)
class ColorRequest:
    action: ColorAction
    value: str = ""
    picture: bool = False


def _selected_option(parsed: ParsedArgs, aliases: Sequence[str]) -> tuple[str, str] | None:
    selected = [(alias, parsed.opt(alias)) for alias in aliases if parsed.has(alias)]
    if len(selected) > 1:
        raise ColorInputError("同一参数不能同时使用短别名和长别名")
    return selected[0] if selected else None


def _selected_action(parsed: ParsedArgs) -> tuple[ColorAction, str] | None:
    selected: list[tuple[ColorAction, str]] = []
    for action, aliases in _PRIMARY_OPTIONS.items():
        option = _selected_option(parsed, aliases)
        if option is not None:
            selected.append((action, option[1]))
    if len(selected) > 1:
        raise ColorInputError("一次只能执行一种颜色操作")
    return selected[0] if selected else None


def _parse_args(args: str) -> ParsedArgs:
    if not isinstance(args, str):
        raise ColorInputError("命令参数必须是文本")
    if len(args) > MAX_ARGS_CHARS:
        raise ColorInputError(f"命令参数不能超过 {MAX_ARGS_CHARS} 个字符")
    if has_control_characters(args):
        raise ColorInputError("命令参数包含控制字符")

    parsed = parse(args)
    known_options = {alias for aliases in _PRIMARY_OPTIONS.values() for alias in aliases} | set(
        _PICTURE_OPTIONS
    )
    unknown = sorted(set(parsed.options) - known_options)
    if unknown:
        raise ColorInputError(f"未知选项: --{unknown[0]}")
    return parsed


def _request_without_action(parsed: ParsedArgs, *, picture: bool) -> ColorRequest:
    if not parsed:
        return ColorRequest("help")
    if not parsed.options and len(parsed.tokens) == 1 and parsed.first.casefold() in _HELP_WORDS:
        return ColorRequest("help")
    if picture:
        raise ColorInputError("-p/--picture 必须配合颜色查询操作")
    raise ColorInputError("缺少操作选项；使用 /color help 查看用法")


def _request_from_option(
    parsed: ParsedArgs,
    action_option: tuple[ColorAction, str],
    *,
    picture: bool,
) -> ColorRequest:
    action, raw_value = action_option
    if action == "help":
        if raw_value != FLAG_VALUE or parsed.tokens or picture:
            raise ColorInputError("帮助选项不接受参数或其他选项")
        return ColorRequest("help")

    if action in _REST_ACTIONS:
        raw_value = " ".join(part for part in (raw_value, parsed.rest()) if part)
    elif parsed.tokens:
        raise ColorInputError("存在多余的位置参数")
    if raw_value == FLAG_VALUE:
        if action != "spectype":
            raise ColorInputError("缺少操作参数")
        raw_value = ""
    if picture and action not in _PICTURE_ACTIONS:
        raise ColorInputError("当前操作不支持 -p/--picture")
    cleaned_value = raw_value.strip()
    maximum = MAX_DEFINITION_CHARS if action == "write" else MAX_QUERY_CHARS
    if len(cleaned_value) > maximum:
        raise ColorInputError(f"参数不能超过 {maximum} 个字符")
    if action != "spectype" and not cleaned_value:
        raise ColorInputError("缺少操作参数")
    return ColorRequest(
        action=action,
        value=cleaned_value,
        picture=picture,
    )


def _parse_request(args: str) -> ColorRequest:
    parsed = _parse_args(args)
    picture_option = _selected_option(parsed, _PICTURE_OPTIONS)
    if picture_option is not None and picture_option[1] != FLAG_VALUE:
        raise ColorInputError("-p/--picture 不接受参数")
    picture = picture_option is not None
    action_option = _selected_action(parsed)
    if action_option is None:
        return _request_without_action(parsed, picture=picture)
    return _request_from_option(parsed, action_option, picture=picture)


def _can_manage_custom_colors(event: Mapping[str, Any], context: Any) -> bool:
    user_id = event.get("user_id")
    if type(user_id) is not int or user_id <= 0:
        return False
    checker = getattr(context, "is_global_admin", None)
    if not callable(checker):
        return False
    try:
        return checker(user_id) is True
    except Exception:
        return False


def _image_dir(context: Any) -> Path:
    data_dir = getattr(context, "data_dir", None)
    if not isinstance(data_dir, Path):
        raise ValueError("color data_dir must be a Path")
    return data_dir / "images"


async def _found_color_response(
    color: ColorRecord,
    *,
    picture: bool,
    context: Any,
) -> Messages:
    result = [text(data_manager.format_color_info(color))]
    if picture:
        image_path = await image_gen.generate_color_image(
            color["name"],
            color["RGB"],
            _image_dir(context),
            context,
        )
        if image_path:
            result.append(image(image_path))
    return result


def _parse_channels(value: str, *, label: str, count: int, maximum: int) -> list[int]:
    parts = [part for part in re.split(r"[,\s]+", value) if part]
    if len(parts) != count:
        raise ColorInputError(f"{label} 需要 {count} 个整数")
    if any(_ASCII_INTEGER.fullmatch(part) is None for part in parts):
        raise ColorInputError(f"{label} 只接受 ASCII 整数")
    channels = [int(part) for part in parts]
    if any(not 0 <= channel <= maximum for channel in channels):
        raise ColorInputError(f"{label} 值必须在 0-{maximum} 范围内")
    return channels


async def _handle_name(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: Any,
) -> Messages:
    color = query.find_by_name(colors, request.value)
    logger.info("颜色名称查询: found=%s", color is not None)
    if color is None:
        return segments(f"中国色里没有收录「{request.value}」这个颜色哦。")
    return await _found_color_response(color, picture=request.picture, context=context)


async def _handle_rgb(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: Any,
) -> Messages:
    rgb = _parse_channels(request.value, label="RGB", count=3, maximum=convert.RGB_MAX)
    color = query.find_by_rgb(colors, rgb)
    logger.info("RGB 查询: found=%s", color is not None)
    if color is not None:
        return await _found_color_response(color, picture=request.picture, context=context)

    result = [text(f"中国色里没有收录这个颜色哦。\nRGB: {rgb}\nHEX: {convert.rgb_to_hex(rgb)}")]
    if request.picture:
        image_path = await image_gen.generate_color_image(
            "自定义颜色", rgb, _image_dir(context), context
        )
        if image_path:
            result.insert(0, text("虽然没有收录，这个颜色长这个样子："))
            result.append(image(image_path))
    return result


async def _handle_hex(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: Any,
) -> Messages:
    try:
        rgb = convert.hex_to_rgb(request.value)
    except ValueError as exc:
        raise ColorInputError(str(exc)) from exc
    normalized = convert.rgb_to_hex(rgb)
    color = query.find_by_hex(colors, normalized)
    logger.info("HEX 查询: found=%s", color is not None)
    if color is not None:
        return await _found_color_response(color, picture=request.picture, context=context)
    return segments(f"中国色里没有收录这个颜色哦。\nHEX: {normalized}\nRGB: {rgb}")


async def _handle_cmyk(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: Any,
) -> Messages:
    cmyk = _parse_channels(request.value, label="CMYK", count=4, maximum=convert.CMYK_MAX)
    color = query.find_by_cmyk(colors, cmyk)
    logger.info("CMYK 查询: found=%s", color is not None)
    if color is None:
        return segments("中国色里没有收录这个颜色哦。")
    return await _found_color_response(color, picture=request.picture, context=context)


def _handle_keyword(request: ColorRequest, colors: Sequence[ColorRecord]) -> Messages:
    matches = query.find_by_keyword(colors, request.value)
    logger.info("颜色关键词查询: count=%d", len(matches))
    if not matches:
        return segments(f"中国色里没有收录「{request.value}」色系哦。")
    names = [color["name"] for color in matches[:MAX_SEARCH_RESULTS]]
    suffix = f"\n... 共 {len(matches)} 个" if len(matches) > MAX_SEARCH_RESULTS else ""
    return segments("，".join(names) + suffix)


def _parse_custom_color(definition: str) -> ColorRecord:
    parts = [part for part in re.split(r"[\s,，]+", definition) if part]
    if len(parts) not in {2, 4}:
        raise ColorInputError(
            "格式错误；请使用“颜色名 R G B”或“颜色名 #HEX”，例如 /color -w 我的红 255 0 0"
        )
    name = parts[0].strip()
    if not name or len(name) > MAX_CUSTOM_COLOR_NAME_LENGTH:
        raise ColorInputError(f"颜色名不能为空且不能超过 {MAX_CUSTOM_COLOR_NAME_LENGTH} 个字符")
    if has_control_characters(name):
        raise ColorInputError("颜色名包含控制字符")

    if len(parts) == 2:
        try:
            rgb = convert.hex_to_rgb(parts[1])
        except ValueError as exc:
            raise ColorInputError(str(exc)) from exc
    else:
        rgb = _parse_channels(" ".join(parts[1:]), label="RGB", count=3, maximum=convert.RGB_MAX)
    return {
        "name": name,
        "pinyin": "",
        "RGB": rgb,
        "hex": convert.rgb_to_hex(rgb),
        "CMYK": convert.rgb_to_cmyk(rgb),
    }


async def _add_custom_color(
    definition: str,
    colors: Sequence[ColorRecord],
    context: Any,
) -> Messages:
    new_color = _parse_custom_color(definition)
    name = new_color["name"]
    if query.find_by_name(colors, name) is not None:
        return segments(f"❌ 「{name}」已经定义过了哦")

    def add(current: list[ColorRecord]) -> Literal["added", "duplicate", "full"]:
        if any(color["name"] == name for color in current):
            return "duplicate"
        if len(current) >= MAX_CUSTOM_COLORS_PER_SCOPE:
            return "full"
        current.append(new_color)
        return "added"

    outcome = await data_manager.mutate_custom_colors_async(context, add)
    if outcome == "duplicate":
        return segments(f"❌ 「{name}」已经定义过了哦")
    if outcome == "full":
        return segments("❌ 当前会话的自定义颜色数量已达上限")

    logger.info("添加自定义颜色: rgb_channels=3")
    image_path = await image_gen.generate_color_image(
        name,
        new_color["RGB"],
        _image_dir(context),
        context,
    )
    result = [text(f"✅ 颜色「{name}」添加成功！\n\n{data_manager.format_color_info(new_color)}")]
    if image_path:
        result.append(image(image_path))
    return result


async def _delete_custom_color(name: str, context: Any) -> Messages:
    def remove(colors: list[ColorRecord]) -> bool:
        original_count = len(colors)
        colors[:] = [color for color in colors if color["name"] != name]
        return len(colors) != original_count

    if not await data_manager.mutate_custom_colors_async(context, remove):
        return segments(f"❌ 自定义颜色中没有「{name}」")
    logger.info("删除自定义颜色: removed=true")
    return segments(f"✅ 颜色「{name}」已删除")


async def _handle_palette_request(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    event: Mapping[str, Any],
    context: Any,
) -> Messages:
    if request.action == "name":
        return await _handle_name(request, colors, context)
    if request.action == "rgb":
        return await _handle_rgb(request, colors, context)
    if request.action == "hex":
        return await _handle_hex(request, colors, context)
    if request.action == "cmyk":
        return await _handle_cmyk(request, colors, context)
    if request.action == "keyword":
        return _handle_keyword(request, colors)
    if request.action in {"write", "delete"} and not _can_manage_custom_colors(event, context):
        return segments("❌ 只有 Bot 全局管理员可以修改当前会话的自定义颜色库")
    if request.action == "write":
        return await _add_custom_color(request.value, colors, context)
    if request.action == "delete":
        return await _delete_custom_color(request.value, context)
    raise RuntimeError(f"unsupported palette action: {request.action}")


async def handle(
    _command: str,
    args: str,
    event: dict[str, Any],
    context: Any,
) -> Messages:
    """解析一次命令，并只加载当前操作真正需要的数据。"""

    try:
        request = _parse_request(args)
        logger.info("颜色命令: action=%s picture=%s", request.action, request.picture)
        if request.action == "help":
            return segments(HELP_TEXT)
        if request.action == "spectype":
            return stellar.list_spectral_types(request.value, context)
        if request.action == "stellar":
            return await stellar.query_stellar_color(request.value, context, _image_dir(context))

        colors = await data_manager.load_colors_async(context)
        if not colors:
            return segments("❌ 颜色数据加载失败，请检查插件资源")
        return await _handle_palette_request(request, colors, event, context)
    except ColorInputError as exc:
        return segments(f"❌ {exc}")
    except Exception as exc:
        return cast(
            Messages,
            public_error_response(context, exc, logger=logger, component="color.handle"),
        )
