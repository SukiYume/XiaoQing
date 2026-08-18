"""路由中国传统色、会话自定义色和恒星光谱色查询。"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from core.args import FLAG_VALUE, ParsedArgs, parse, parse_int, quote_token, tokenize
from core.interfaces import PluginContextProtocol
from core.plugin_base import has_control_characters, image, segments, text
from core.public_errors import public_error_response

from . import convert, data_manager, image_gen, query, stellar
from .data_manager import ColorRecord

logger = logging.getLogger(__name__)
Messages = list[dict[str, Any]]
ColorAction = Literal[
    "help",
    "auto",
    "catalog",
    "random",
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
LIST_PAGE_SIZE = 20
MAX_PAGE = 1_000
MAX_CUSTOM_COLORS_PER_SCOPE = data_manager.MAX_CUSTOM_COLORS_PER_SCOPE
MAX_CUSTOM_COLOR_NAME_LENGTH = data_manager.MAX_COLOR_NAME_CHARS

_PRIMARY_OPTIONS: dict[ColorAction, tuple[str, ...]] = {
    "help": ("h", "help"),
    "catalog": ("l", "list"),
    "name": ("n", "name"),
    "rgb": ("r", "rgb"),
    "hex": ("x", "hex"),
    "cmyk": ("c", "cmyk"),
    "keyword": ("a", "accord", "q", "search"),
    "write": ("w", "write"),
    "delete": ("d", "delete"),
    "stellar": ("s", "stellar"),
    "spectype": ("t", "spectype"),
}
_PICTURE_OPTIONS = ("p", "picture")
_PAGE_OPTIONS = ("page",)
_HELP_WORDS = frozenset({"help", "h", "帮助"})
_CATALOG_WORDS = frozenset({"list", "ls", "colors", "列表", "色表"})
_SEARCH_WORDS = frozenset({"search", "find", "搜索", "查找", "搜"})
_RANDOM_WORDS = frozenset({"random", "rand", "随机"})
_STELLAR_WORDS = frozenset({"star", "stellar", "恒星"})
_SPECTYPE_WORDS = frozenset({"stars", "types", "spectypes", "光谱型"})
_WRITE_WORDS = frozenset({"add", "write", "添加", "新增"})
_DELETE_WORDS = frozenset({"delete", "del", "remove", "删除"})
_NAME_WORDS = frozenset({"name", "名称"})
_RGB_WORDS = frozenset({"rgb"})
_HEX_WORDS = frozenset({"hex"})
_CMYK_WORDS = frozenset({"cmyk"})
_PICTURE_ACTIONS = frozenset({"auto", "name", "rgb", "hex", "cmyk", "random"})
_REST_ACTIONS = frozenset({"rgb", "cmyk", "keyword", "write"})
_ASCII_INTEGER = re.compile(r"-?[0-9]+")
_DIRECT_HEX_PATTERN = re.compile(r"#?(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})\Z")

HELP_TEXT = """🎨 颜色工具｜526 种中国传统色

直接输入名称、拼音或颜色值
/color 乳白
/color rubai
/color #f9f4dc
/color 249 244 220

不知道名称时
/color list  浏览颜色目录
/color search 红  按名称或拼音搜索
/color random  随机发现一种颜色

恒星颜色
/color star G2V  查询光谱型颜色
/color stars G  浏览 G 型光谱

查询后加 --picture 可生成色卡，例如 /color 乳白 --picture
管理员可用 /color add 和 /color delete 管理当前聊天的自定义色。
短选项同样可用，包括用于生成色卡的 -p。"""


class ColorInputError(ValueError):
    """可以直接安全返回给用户的颜色命令错误。"""


@dataclass(frozen=True)
class ColorRequest:
    action: ColorAction
    value: str = ""
    picture: bool = False
    page: int = 1


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

    try:
        tokenize(args, strict=True)
    except ValueError as exc:
        raise ColorInputError("命令中的引号没有闭合") from exc
    parsed = parse(args)
    known_options = (
        {alias for aliases in _PRIMARY_OPTIONS.values() for alias in aliases}
        | set(_PICTURE_OPTIONS)
        | set(_PAGE_OPTIONS)
    )
    unknown = sorted(set(parsed.options) - known_options)
    if unknown:
        raise ColorInputError(f"未知选项: --{unknown[0]}")
    return parsed


def _parse_page(value: str) -> int:
    page = parse_int(value, minimum=1, maximum=MAX_PAGE)
    if page is None:
        raise ColorInputError(f"页码必须是 1 到 {MAX_PAGE} 的 ASCII 整数")
    return page


def _resolve_page(
    positional: str | None,
    page_option: tuple[str, str] | None,
) -> int:
    if positional is not None and page_option is not None:
        raise ColorInputError("页码不能同时使用位置参数和 --page")
    raw_value = positional if positional is not None else page_option[1] if page_option else None
    return 1 if raw_value is None else _parse_page(raw_value)


def _clean_request_value(value: str, *, maximum: int, allow_empty: bool = False) -> str:
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ColorInputError(f"参数不能超过 {maximum} 个字符")
    if not cleaned and not allow_empty:
        raise ColorInputError("缺少操作参数")
    return cleaned


def _request_from_option(
    parsed: ParsedArgs,
    action_option: tuple[ColorAction, str],
    *,
    picture: bool,
    page_option: tuple[str, str] | None,
) -> ColorRequest:
    action, raw_value = action_option
    if action == "help":
        if raw_value != FLAG_VALUE or parsed.tokens or picture or page_option is not None:
            raise ColorInputError("帮助选项不接受参数或其他选项")
        return ColorRequest("help")

    if action == "catalog":
        if parsed.tokens:
            raise ColorInputError("颜色目录只接受页码")
        if picture:
            raise ColorInputError("颜色目录不支持 -p/--picture")
        positional_page = None if raw_value == FLAG_VALUE else raw_value
        return ColorRequest("catalog", page=_resolve_page(positional_page, page_option))

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
    if page_option is not None and action not in {"keyword", "spectype"}:
        raise ColorInputError("--page 只能用于颜色目录、搜索结果或光谱型列表")
    page = _resolve_page(None, page_option) if action in {"keyword", "spectype"} else 1
    maximum = MAX_DEFINITION_CHARS if action == "write" else MAX_QUERY_CHARS
    cleaned_value = _clean_request_value(
        raw_value,
        maximum=maximum,
        allow_empty=action == "spectype",
    )
    return ColorRequest(
        action=action,
        value=cleaned_value,
        picture=picture,
        page=page,
    )


def _request_from_words(
    parsed: ParsedArgs,
    *,
    picture: bool,
    page_option: tuple[str, str] | None,
) -> ColorRequest:
    tokens = parsed.tokens
    if not tokens:
        if picture:
            raise ColorInputError("-p/--picture 必须跟在具体颜色查询后")
        if page_option is not None:
            raise ColorInputError("--page 必须配合颜色目录、搜索结果或光谱型列表")
        return ColorRequest("help")

    command_word = tokens[0].casefold()
    values = tokens[1:]
    if command_word in _HELP_WORDS:
        if values or picture or page_option is not None:
            raise ColorInputError("帮助命令不接受额外参数")
        return ColorRequest("help")
    if command_word in _CATALOG_WORDS:
        if picture:
            raise ColorInputError("颜色目录不支持 -p/--picture")
        if len(values) > 1:
            raise ColorInputError("颜色目录只接受一个页码")
        positional_page = values[0] if values else None
        return ColorRequest("catalog", page=_resolve_page(positional_page, page_option))
    if command_word in _SEARCH_WORDS:
        if picture:
            raise ColorInputError("搜索结果列表不支持 -p/--picture")
        value = _clean_request_value(" ".join(values), maximum=MAX_QUERY_CHARS)
        return ColorRequest(
            "keyword",
            value=value,
            page=_resolve_page(None, page_option),
        )
    if command_word in _RANDOM_WORDS:
        if values or page_option is not None:
            raise ColorInputError("随机颜色不接受额外参数")
        return ColorRequest("random", picture=picture)
    if command_word in _STELLAR_WORDS:
        if picture or page_option is not None:
            raise ColorInputError("恒星颜色查询不接受图片或页码选项")
        value = _clean_request_value(" ".join(values), maximum=MAX_QUERY_CHARS)
        return ColorRequest("stellar", value=value)
    if command_word in _SPECTYPE_WORDS:
        if picture:
            raise ColorInputError("光谱型列表不支持 -p/--picture")
        value = _clean_request_value(
            " ".join(values),
            maximum=MAX_QUERY_CHARS,
            allow_empty=True,
        )
        return ColorRequest(
            "spectype",
            value=value,
            page=_resolve_page(None, page_option),
        )
    if command_word in _WRITE_WORDS:
        if picture or page_option is not None:
            raise ColorInputError("添加颜色不接受图片或页码选项")
        value = _clean_request_value(" ".join(values), maximum=MAX_DEFINITION_CHARS)
        return ColorRequest("write", value=value)
    if command_word in _DELETE_WORDS:
        if picture or page_option is not None:
            raise ColorInputError("删除颜色不接受图片或页码选项")
        value = _clean_request_value(" ".join(values), maximum=MAX_QUERY_CHARS)
        return ColorRequest("delete", value=value)

    explicit_actions = (
        (_NAME_WORDS, "name"),
        (_RGB_WORDS, "rgb"),
        (_HEX_WORDS, "hex"),
        (_CMYK_WORDS, "cmyk"),
    )
    for words, action in explicit_actions:
        if command_word in words:
            if page_option is not None:
                raise ColorInputError("精确颜色查询不支持 --page")
            value = _clean_request_value(" ".join(values), maximum=MAX_QUERY_CHARS)
            return ColorRequest(cast(ColorAction, action), value=value, picture=picture)

    value = _clean_request_value(" ".join(tokens), maximum=MAX_QUERY_CHARS)
    return ColorRequest(
        "auto",
        value=value,
        picture=picture,
        page=_resolve_page(None, page_option),
    )


def _parse_request(args: str) -> ColorRequest:
    parsed = _parse_args(args)
    picture_option = _selected_option(parsed, _PICTURE_OPTIONS)
    if picture_option is not None and picture_option[1] != FLAG_VALUE:
        raise ColorInputError("-p/--picture 不接受参数")
    picture = picture_option is not None
    page_option = _selected_option(parsed, _PAGE_OPTIONS)
    action_option = _selected_action(parsed)
    if action_option is None:
        return _request_from_words(
            parsed,
            picture=picture,
            page_option=page_option,
        )
    return _request_from_option(
        parsed,
        action_option,
        picture=picture,
        page_option=page_option,
    )


def _can_manage_custom_colors(
    event: Mapping[str, Any],
    context: PluginContextProtocol,
) -> bool:
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


def _image_dir(context: PluginContextProtocol) -> Path:
    data_dir = getattr(context, "data_dir", None)
    if not isinstance(data_dir, Path):
        raise ValueError("color data_dir must be a Path")
    return data_dir / "images"


async def _found_color_response(
    color: ColorRecord,
    *,
    picture: bool,
    context: PluginContextProtocol,
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


def _color_row(color: ColorRecord, index: int) -> str:
    pinyin = f"（{color['pinyin']}）" if color.get("pinyin") else ""
    return f"{index}. {color['name']}{pinyin} · {color['hex']}"


def _format_color_page(
    colors: Sequence[ColorRecord],
    *,
    title: str,
    page: int,
    page_command: Callable[[int], str],
) -> Messages:
    total = len(colors)
    if total == 0:
        return segments(f"❌ {title}：没有结果\n可用 /color list 浏览全部颜色")
    total_pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
    if page > total_pages:
        return segments(f"❌ 第 {page} 页超出范围（共 {total_pages} 页）")
    start = (page - 1) * LIST_PAGE_SIZE
    displayed = colors[start : start + LIST_PAGE_SIZE]
    lines = [f"🎨 {title}", ""]
    lines.extend(_color_row(color, start + offset) for offset, color in enumerate(displayed, 1))
    lines.extend(("", f"第 {page}/{total_pages} 页｜共 {total} 种"))
    if page > 1:
        lines.append(f"上一页：{page_command(page - 1)}")
    if page < total_pages:
        lines.append(f"下一页：{page_command(page + 1)}")
    lines.append("查看详情：/color <名称>")
    return segments("\n".join(lines))


def _handle_catalog(request: ColorRequest, colors: Sequence[ColorRecord]) -> Messages:
    logger.info("浏览颜色目录: page=%d count=%d", request.page, len(colors))
    return _format_color_page(
        colors,
        title="颜色目录",
        page=request.page,
        page_command=lambda page: f"/color list {page}",
    )


def _handle_keyword(request: ColorRequest, colors: Sequence[ColorRecord]) -> Messages:
    matches = query.find_by_keyword(colors, request.value)
    logger.info(
        "颜色关键词查询: query_chars=%d page=%d count=%d",
        len(request.value),
        request.page,
        len(matches),
    )
    quoted = quote_token(request.value)
    return _format_color_page(
        matches,
        title=f"“{request.value}”的名称/拼音搜索结果",
        page=request.page,
        page_command=lambda page: f"/color search {quoted} --page {page}",
    )


async def _handle_name(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: PluginContextProtocol,
) -> Messages:
    color = query.find_by_name(colors, request.value)
    logger.info("颜色名称查询: found=%s", color is not None)
    if color is None:
        matches = query.find_by_keyword(colors, request.value)
        if matches:
            quoted = quote_token(request.value)
            return _format_color_page(
                matches,
                title=f"没有精确名称“{request.value}”，相近结果",
                page=1,
                page_command=lambda page: f"/color search {quoted} --page {page}",
            )
        return segments(f"❌ 没有收录名称“{request.value}”\n可用 /color list 浏览全部颜色")
    return await _found_color_response(color, picture=request.picture, context=context)


async def _nearest_color_response(
    color: ColorRecord,
    *,
    input_label: str,
    input_rgb: list[int],
    distance: float,
    picture: bool,
    context: PluginContextProtocol,
) -> Messages:
    result = [
        text(
            "🎯 最接近的收录色（近似匹配）\n"
            f"输入：{input_label}\n"
            f"CIE76 色差：{distance:.2f}\n\n"
            f"{data_manager.format_color_info(color)}"
        )
    ]
    if picture:
        image_path = await image_gen.generate_color_image(
            "输入颜色",
            input_rgb,
            _image_dir(context),
            context,
        )
        if image_path:
            result.append(text("色卡显示输入颜色："))
            result.append(image(image_path))
    return result


async def _handle_rgb(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: PluginContextProtocol,
) -> Messages:
    rgb = _parse_channels(request.value, label="RGB", count=3, maximum=convert.RGB_MAX)
    exact = query.find_by_rgb(colors, rgb)
    logger.info("RGB 查询: exact=%s", exact is not None)
    if exact is not None:
        return await _found_color_response(exact, picture=request.picture, context=context)
    nearest = query.find_nearest_by_rgb(colors, rgb)
    if nearest is None:
        raise RuntimeError("color palette is empty")
    color, distance = nearest
    return await _nearest_color_response(
        color,
        input_label=f"RGB {', '.join(str(channel) for channel in rgb)}",
        input_rgb=rgb,
        distance=distance,
        picture=request.picture,
        context=context,
    )


async def _handle_hex(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: PluginContextProtocol,
) -> Messages:
    try:
        rgb = convert.hex_to_rgb(request.value)
    except ValueError as exc:
        raise ColorInputError(str(exc)) from exc
    normalized = convert.rgb_to_hex(rgb)
    color = query.find_by_hex(colors, normalized)
    logger.info("HEX 查询: exact=%s", color is not None)
    if color is not None:
        return await _found_color_response(color, picture=request.picture, context=context)
    nearest = query.find_nearest_by_rgb(colors, rgb)
    if nearest is None:
        raise RuntimeError("color palette is empty")
    color, distance = nearest
    return await _nearest_color_response(
        color,
        input_label=f"HEX {normalized}",
        input_rgb=rgb,
        distance=distance,
        picture=request.picture,
        context=context,
    )


async def _handle_cmyk(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: PluginContextProtocol,
) -> Messages:
    cmyk = _parse_channels(request.value, label="CMYK", count=4, maximum=convert.CMYK_MAX)
    exact = query.find_by_cmyk(colors, cmyk)
    logger.info("CMYK 查询: exact=%s", exact is not None)
    if exact is not None:
        return await _found_color_response(exact, picture=request.picture, context=context)
    rgb = convert.cmyk_to_rgb(cmyk)
    nearest = query.find_nearest_by_rgb(colors, rgb)
    if nearest is None:
        raise RuntimeError("color palette is empty")
    color, distance = nearest
    return await _nearest_color_response(
        color,
        input_label=f"CMYK {', '.join(str(channel) for channel in cmyk)}",
        input_rgb=rgb,
        distance=distance,
        picture=request.picture,
        context=context,
    )


async def _handle_auto(
    request: ColorRequest,
    colors: Sequence[ColorRecord],
    context: PluginContextProtocol,
) -> Messages:
    value = request.value
    if _DIRECT_HEX_PATTERN.fullmatch(value) is not None:
        return await _handle_hex(
            ColorRequest("hex", value=value, picture=request.picture),
            colors,
            context,
        )

    channel_parts = [part for part in re.split(r"[,\s]+", value) if part]
    if len(channel_parts) in {3, 4} and all(
        _ASCII_INTEGER.fullmatch(part) is not None for part in channel_parts
    ):
        action: ColorAction = "rgb" if len(channel_parts) == 3 else "cmyk"
        handler = _handle_rgb if action == "rgb" else _handle_cmyk
        return await handler(
            ColorRequest(action, value=value, picture=request.picture),
            colors,
            context,
        )

    exact_name = query.find_by_name(colors, value)
    if exact_name is not None:
        if request.page > 1:
            return segments("❌ 精确名称只有 1 页")
        return await _found_color_response(exact_name, picture=request.picture, context=context)

    exact_pinyin = query.find_by_pinyin(colors, value)
    matches = exact_pinyin or query.find_by_keyword(colors, value)
    logger.info(
        "自动颜色查询: query_chars=%d page=%d matches=%d",
        len(value),
        request.page,
        len(matches),
    )
    if not matches:
        return segments(f"❌ 没有找到“{value}”\n可用 /color list 浏览全部颜色")
    if len(matches) == 1:
        if request.page > 1:
            return segments("❌ 查询结果只有 1 页")
        return await _found_color_response(matches[0], picture=request.picture, context=context)
    if request.picture:
        raise ColorInputError("找到多个颜色，请先选择具体名称再加 -p 生成色卡")
    quoted = quote_token(value)
    return _format_color_page(
        matches,
        title=f"“{value}”的名称/拼音搜索结果",
        page=request.page,
        page_command=lambda page: f"/color search {quoted} --page {page}",
    )


def _parse_custom_color(definition: str) -> ColorRecord:
    parts = [part for part in re.split(r"[\s,，]+", definition) if part]
    if len(parts) < 2:
        raise ColorInputError(
            "格式错误；请使用“颜色名 R G B”或“颜色名 #HEX”，例如 /color add 我的红 255 0 0"
        )
    if _DIRECT_HEX_PATTERN.fullmatch(parts[-1]) is not None:
        name_parts = parts[:-1]
        try:
            rgb = convert.hex_to_rgb(parts[-1])
        except ValueError as exc:
            raise ColorInputError(str(exc)) from exc
    elif len(parts) >= 4:
        name_parts = parts[:-3]
        rgb = _parse_channels(" ".join(parts[-3:]), label="RGB", count=3, maximum=convert.RGB_MAX)
    else:
        raise ColorInputError(
            "格式错误；请使用“颜色名 R G B”或“颜色名 #HEX”，例如 /color add 我的红 255 0 0"
        )
    name = " ".join(name_parts).strip()
    if not name or len(name) > MAX_CUSTOM_COLOR_NAME_LENGTH:
        raise ColorInputError(f"颜色名不能为空且不能超过 {MAX_CUSTOM_COLOR_NAME_LENGTH} 个字符")
    if has_control_characters(name):
        raise ColorInputError("颜色名包含控制字符")
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
    context: PluginContextProtocol,
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


async def _delete_custom_color(name: str, context: PluginContextProtocol) -> Messages:
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
    context: PluginContextProtocol,
) -> Messages:
    if request.action == "catalog":
        return _handle_catalog(request, colors)
    if request.action == "auto":
        return await _handle_auto(request, colors, context)
    if request.action == "random":
        selected = colors[secrets.randbelow(len(colors))]
        logger.info("随机颜色: palette_count=%d", len(colors))
        return await _found_color_response(selected, picture=request.picture, context=context)
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
    context: PluginContextProtocol,
) -> Messages:
    """解析一次命令，并只加载当前操作真正需要的数据。"""

    try:
        request = _parse_request(args)
        logger.info(
            "颜色命令: action=%s picture=%s page=%d",
            request.action,
            request.picture,
            request.page,
        )
        if request.action == "help":
            return segments(HELP_TEXT)
        if request.action == "spectype":
            return stellar.list_spectral_types(request.value, context, page=request.page)
        if request.action == "stellar":
            return await stellar.query_stellar_color(request.value, context, _image_dir(context))
        if request.action == "auto" and stellar.is_spectral_type(request.value):
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
