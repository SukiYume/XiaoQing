"""提供有界、可校验的天文学中英词典查询。"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast

from core.args import quote_token, tokenize
from core.interfaces import PluginContextProtocol
from core.plugin_base import has_control_characters, run_sync, segments
from core.public_errors import public_error_message, public_error_response

MAX_ARGUMENT_CHARS = 512
MAX_QUERY_CHARS = 256
MAX_RESULTS = 100
DEFAULT_RESULTS = 10
MAX_MANIFEST_BYTES = 32 * 1024
MAX_DICTIONARY_BYTES = 4 * 1024 * 1024
MAX_DICTIONARY_ENTRIES = 50_000
MAX_PAGE = MAX_DICTIONARY_ENTRIES
MAX_DICTIONARY_LINE_CHARS = 2_048
MAX_SOURCE_CHARS = 256
MAX_DESTINATION_CHARS = 1_024

_HELP_ALIASES = frozenset({"help", "帮助"})
_EXACT_OPTIONS = frozenset({"-e", "--exact"})
_LIMIT_OPTIONS = frozenset({"-n", "--num", "--size", "--page-size"})
_PAGE_OPTIONS = frozenset({"-p", "--page"})
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_LIMIT_PATTERN = re.compile(r"[1-9][0-9]{0,2}\Z")
_PAGE_PATTERN = re.compile(r"[1-9][0-9]{0,4}\Z")
_CHINESE_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")

Messages = list[dict[str, Any]]
QueryDirection = Literal["chinese_to_english", "english_to_chinese"]

HELP_TEXT = """📖 天文学词典

查询中国天文学会天文学名词审定委员会发布的中英天文学名词。

用法
/dict <词汇>  直接查询；最相关词条优先
/dict <词汇> --exact  只看完整匹配
/dict <词汇> --page <页码>  查看指定页
/dict <词汇> --size <1-100>  指定每页条数
/dict help  显示完整帮助

示例
/dict galaxy
/dict 星系
/dict "fast radio burst" --exact
/dict galaxy --page 2
/dict star --size 20 --page 3

直接查询会自动判断中英方向；多词按“全部包含”匹配。
结果末尾会给出可直接复制的上一页、下一页命令。"""


class DictionaryDataError(RuntimeError):
    """表示可向用户稳定说明的内置资源错误。"""


class DictionaryEntry(NamedTuple):
    """保存原始词条及用于不区分大小写查询的源词。"""

    source: str
    destination: str
    source_folded: str


@dataclass(frozen=True)
class ResourceSpec:
    """清单中一份词典资源的完整校验约束。"""

    filename: str
    sha256: str
    byte_count: int
    entry_count: int


@dataclass(frozen=True)
class DictionaryRequest:
    """一次已经消除选项歧义的词典请求。"""

    action: Literal["help", "query"]
    query: str = ""
    exact_match: bool = False
    max_results: int = DEFAULT_RESULTS
    page: int = 1


def _clean_query(value: object) -> str:
    """规范化用户查询，同时阻止巨型或不可显示的扫描请求。"""

    if not isinstance(value, str):
        raise ValueError("查询词必须是字符串")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("请提供要查询的词汇")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ValueError(f"查询词不能超过 {MAX_QUERY_CHARS} 个字符")
    if has_control_characters(cleaned):
        raise ValueError("查询词不能包含控制字符")
    return cleaned


def _parse_limit(value: str) -> int:
    if _LIMIT_PATTERN.fullmatch(value) is None:
        raise ValueError("每页条数必须是 1 到 100 的 ASCII 整数")
    parsed = int(value)
    if parsed > MAX_RESULTS:
        raise ValueError("每页条数必须是 1 到 100 的 ASCII 整数")
    return parsed


def _parse_page(value: str) -> int:
    if _PAGE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"页码必须是 1 到 {MAX_PAGE} 的 ASCII 整数")
    parsed = int(value)
    if parsed > MAX_PAGE:
        raise ValueError(f"页码必须是 1 到 {MAX_PAGE} 的 ASCII 整数")
    return parsed


def _tokenize_request(args: object) -> list[str]:
    """在进入选项状态机前完成类型、预算、控制字符和引号校验。"""

    if not isinstance(args, str):
        raise TypeError("dict arguments must be a string")
    if len(args) > MAX_ARGUMENT_CHARS:
        raise ValueError(f"命令参数不能超过 {MAX_ARGUMENT_CHARS} 个字符")
    if has_control_characters(args):
        raise ValueError("命令参数不能包含控制字符")
    try:
        tokens = tokenize(args, strict=True)
    except ValueError as exc:
        raise ValueError("命令中的引号没有闭合") from exc
    return tokens


def _parse_request(args: object) -> DictionaryRequest:
    """按单一状态机解析选项，避免通用解析器吞掉查询词。"""

    tokens = _tokenize_request(args)
    if not tokens:
        return DictionaryRequest("help")
    if tokens[0].casefold() in _HELP_ALIASES:
        if len(tokens) == 1:
            return DictionaryRequest("help")
        raise ValueError("help 子命令不接受额外参数；用法：/dict help")

    exact_match = False
    limit_seen = False
    page_seen = False
    max_results = DEFAULT_RESULTS
    page = 1
    query_tokens: list[str] = []
    options_enabled = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if options_enabled and token == "--":
            options_enabled = False
        elif options_enabled and token in _EXACT_OPTIONS:
            if exact_match:
                raise ValueError("精确匹配选项不能重复")
            exact_match = True
        elif options_enabled and token in _LIMIT_OPTIONS:
            if limit_seen:
                raise ValueError("每页条数选项不能重复")
            index += 1
            if index >= len(tokens):
                raise ValueError("每页条数选项缺少数值")
            max_results = _parse_limit(tokens[index])
            limit_seen = True
        elif options_enabled and token in _PAGE_OPTIONS:
            if page_seen:
                raise ValueError("页码选项不能重复")
            index += 1
            if index >= len(tokens):
                raise ValueError("页码选项缺少数值")
            page = _parse_page(tokens[index])
            page_seen = True
        elif options_enabled and token.startswith("-") and token != "-":
            raise ValueError(f"未知选项：{token}")
        else:
            query_tokens.append(token)
        index += 1

    return DictionaryRequest(
        "query",
        query=_clean_query(" ".join(query_tokens)),
        exact_match=exact_match,
        max_results=max_results,
        page=page,
    )


def _query_command(
    query: str,
    *,
    exact_match: bool,
    max_results: int,
    page: int | None = None,
) -> str:
    """构造查询词优先、可复制执行且无歧义的规范命令。"""

    parts = ["/dict"]
    options: list[str] = []
    if exact_match:
        options.append("--exact")
    if max_results != DEFAULT_RESULTS:
        options.extend(("--size", str(max_results)))
    if page is not None:
        options.extend(("--page", str(page)))
    if query.startswith("-"):
        parts.extend(options)
        parts.append("--")
        parts.append(quote_token(query))
    else:
        parts.append(quote_token(query))
        parts.extend(options)
    return " ".join(parts)


def _page_command(
    query: str,
    *,
    exact_match: bool,
    max_results: int,
    page: int,
) -> str:
    """构造无状态、可复制执行的规范翻页命令。"""

    return _query_command(
        query,
        exact_match=exact_match,
        max_results=max_results,
        page=page,
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    """读取小型 JSON 清单；链接、非常规文件和越界文件均失败关闭。"""

    try:
        file_info = path.lstat()
        if not stat.S_ISREG(file_info.st_mode) or file_info.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("invalid manifest file")
        payload = path.read_bytes()
        if len(payload) != file_info.st_size:
            raise ValueError("manifest changed while reading")
        decoded: object = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DictionaryDataError("天文学词典资源清单无效；请重新安装完整发行包") from exc
    if not isinstance(decoded, dict):
        raise DictionaryDataError("天文学词典资源清单无效；请重新安装完整发行包")
    return cast(dict[str, Any], decoded)


def _resource_spec(manifest: dict[str, Any], direction: QueryDirection) -> ResourceSpec:
    """提取一项资源约束，并同时施加独立于清单的硬上限。"""

    files = manifest.get("files")
    raw_spec = files.get(direction) if isinstance(files, dict) else None
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raw_spec = None
    if not isinstance(raw_spec, dict):
        raise DictionaryDataError("天文学词典资源清单无效；请重新安装完整发行包")

    filename = raw_spec.get("filename")
    expected_sha256 = raw_spec.get("sha256")
    expected_bytes = raw_spec.get("bytes")
    expected_entries = raw_spec.get("entries")
    valid_filename = (
        isinstance(filename, str)
        and 0 < len(filename) <= 64
        and "/" not in filename
        and "\\" not in filename
        and not has_control_characters(filename)
        and filename.endswith(".txt")
    )
    if (
        not valid_filename
        or not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
        or type(expected_bytes) is not int
        or not 0 < expected_bytes <= MAX_DICTIONARY_BYTES
        or type(expected_entries) is not int
        or not 0 < expected_entries <= MAX_DICTIONARY_ENTRIES
    ):
        raise DictionaryDataError("天文学词典资源清单无效；请重新安装完整发行包")
    return ResourceSpec(
        cast(str, filename),
        expected_sha256.casefold(),
        expected_bytes,
        expected_entries,
    )


@lru_cache(maxsize=4)
def _load_dictionary(
    dict_file: Path,
    spec: ResourceSpec,
    fingerprint: tuple[int, int, int],
) -> tuple[DictionaryEntry, ...]:
    """一次读取、校验并解析词典；缓存最多保留四代有界词库。"""

    del fingerprint  # 文件指纹只参与缓存代次，解析仍以内容哈希作为真实性依据。
    try:
        payload = dict_file.read_bytes()
    except OSError as exc:
        raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}") from exc
    if len(payload) != spec.byte_count or hashlib.sha256(payload).hexdigest() != spec.sha256:
        raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}") from exc
    if len(lines) != spec.entry_count:
        raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}")

    entries: list[DictionaryEntry] = []
    seen_pairs: set[tuple[str, str]] = set()
    for line in lines:
        if not line or len(line) > MAX_DICTIONARY_LINE_CHARS:
            raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}")
        fields = line.split("\t")
        if len(fields) != 2:
            raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}")
        source, destination = fields
        if (
            not source
            or not destination
            or source != source.strip()
            or destination != destination.strip()
            or len(source) > MAX_SOURCE_CHARS
            or len(destination) > MAX_DESTINATION_CHARS
            or has_control_characters(source)
            or has_control_characters(destination)
        ):
            raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}")
        pair = (source, destination)
        if pair in seen_pairs:
            raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}")
        seen_pairs.add(pair)
        entries.append(DictionaryEntry(source, destination, source.casefold()))
    return tuple(entries)


def _load_direction(plugin_dir: Path, direction: QueryDirection) -> tuple[DictionaryEntry, ...]:
    """根据清单定位一份发行资产，并以文件元数据刷新解析缓存。"""

    manifest = _read_manifest(plugin_dir / "assets" / "manifest.json")
    spec = _resource_spec(manifest, direction)
    dict_file = plugin_dir / "assets" / spec.filename
    try:
        file_info = dict_file.lstat()
    except FileNotFoundError as exc:
        raise DictionaryDataError(
            f"天文学词典数据文件不存在: {spec.filename}；请重新安装包含 package data 的发行包"
        ) from exc
    except OSError as exc:
        raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}") from exc
    if not stat.S_ISREG(file_info.st_mode) or file_info.st_size != spec.byte_count:
        raise DictionaryDataError(f"天文学词典数据校验失败: {spec.filename}")
    fingerprint = (file_info.st_mtime_ns, file_info.st_ctime_ns, file_info.st_size)
    return _load_dictionary(dict_file, spec, fingerprint)


def _contains_at_word_boundary(source: str, value: str) -> bool:
    """判断子串是否从字符串开头或非字母数字字符之后开始。"""

    start = 0
    while True:
        position = source.find(value, start)
        if position < 0:
            return False
        if position == 0 or not source[position - 1].isalnum():
            return True
        start = position + 1


def _relevance_key(
    entry: DictionaryEntry,
    query: str,
    folded: str,
    keywords: list[str],
) -> tuple[int, int]:
    """把原样精确、忽略大小写精确、前缀和词边界匹配依次前置。"""

    source = entry.source_folded
    if entry.source == query:
        rank = 0
    elif source == folded:
        rank = 1
    elif entry.source.startswith(query):
        rank = 2
    elif source.startswith(folded):
        rank = 3
    elif _contains_at_word_boundary(source, folded):
        rank = 4
    elif folded in source:
        rank = 5
    elif all(_contains_at_word_boundary(source, keyword) for keyword in keywords):
        rank = 6
    else:
        rank = 7
    return rank, len(entry.source)


def _sorted_fuzzy_matches(
    entries: tuple[DictionaryEntry, ...],
    query: str,
    folded: str,
    keywords: list[str],
) -> list[DictionaryEntry]:
    """返回满足全部关键词的词条，并按用户感知的相关度稳定排序。"""

    matches = [
        entry for entry in entries if all(keyword in entry.source_folded for keyword in keywords)
    ]
    matches.sort(key=lambda entry: _relevance_key(entry, query, folded, keywords))
    return matches


def _exact_miss_message(
    query: str,
    direction_label: str,
    suggestions: list[DictionaryEntry],
) -> str:
    """为没有完整匹配的查询提供少量模糊结果和可复制命令。"""

    lines = [f"❌ 没有完全匹配“{query}”的词条（{direction_label}）"]
    if not suggestions:
        lines.append("试试去掉 --exact、缩短查询词或检查拼写")
        return "\n".join(lines)

    shown = suggestions[:5]
    lines.extend(("", f"相近词条（模糊匹配共 {len(suggestions)} 条）"))
    lines.extend(
        f"{index}. {entry.source} → {entry.destination}" for index, entry in enumerate(shown, 1)
    )
    lines.append(
        "\n查看全部："
        + _query_command(
            query,
            exact_match=False,
            max_results=DEFAULT_RESULTS,
        )
    )
    return "\n".join(lines)


def _query_astrodict_sync(
    query: str,
    plugin_dir: Path,
    exact_match: bool,
    max_results: int,
    page: int = 1,
) -> str:
    """在已校验的内置词条中执行确定性的精确或多关键词查询。"""

    if _CHINESE_PATTERN.search(query) is not None:
        direction_key: QueryDirection = "chinese_to_english"
        direction_label = "中译英"
    else:
        direction_key = "english_to_chinese"
        direction_label = "英译中"
    try:
        entries = _load_direction(plugin_dir, direction_key)
    except DictionaryDataError as exc:
        return str(exc)

    folded = query.casefold()
    keywords = folded.split()
    if exact_match:
        all_matches = [entry for entry in entries if entry.source_folded == folded]
        all_matches.sort(key=lambda entry: _relevance_key(entry, query, folded, keywords))
        if not all_matches:
            suggestions = _sorted_fuzzy_matches(entries, query, folded, keywords)
            return _exact_miss_message(query, direction_label, suggestions)
    else:
        all_matches = _sorted_fuzzy_matches(entries, query, folded, keywords)
        if not all_matches:
            return (
                f"❌ 没有找到与“{query}”相关的词条（{direction_label}）\n"
                "试试缩短查询词或检查拼写；帮助：/dict help"
            )

    total_found = len(all_matches)

    total_pages = (total_found + max_results - 1) // max_results
    if page > total_pages:
        return f"❌ 第 {page} 页超出范围（共 {total_pages} 页）\n最后一页：" + _page_command(
            query,
            exact_match=exact_match,
            max_results=max_results,
            page=total_pages,
        )

    page_start = (page - 1) * max_results
    page_end = page_start + max_results
    matches = all_matches[page_start:page_end]
    mode = "｜精确匹配" if exact_match else ""
    lines = [f"📖 “{query}”｜{direction_label}{mode}", ""]
    lines.extend(
        f"{index}. {entry.source} → {entry.destination}"
        for index, entry in enumerate(matches, page_start + 1)
    )
    lines.append(
        f"\n共找到 {total_found} 条结果｜第 {page}/{total_pages} 页｜每页 {max_results} 条"
    )
    navigation: list[str] = []
    if page > 1:
        navigation.append(
            "上一页："
            + _page_command(
                query,
                exact_match=exact_match,
                max_results=max_results,
                page=page - 1,
            )
        )
    if page < total_pages:
        navigation.append(
            "下一页："
            + _page_command(
                query,
                exact_match=exact_match,
                max_results=max_results,
                page=page + 1,
            )
        )
    lines.extend(navigation)
    return "\n".join(lines)


async def query_astrodict(
    query: object,
    context: PluginContextProtocol,
    exact_match: bool = False,
    max_results: int = DEFAULT_RESULTS,
    page: int = 1,
) -> str:
    """校验公开调用参数，并在线程池中完成文件读取与词条扫描。"""

    try:
        cleaned_query = _clean_query(query)
        if type(exact_match) is not bool:
            raise ValueError("精确匹配参数必须是布尔值")
        if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS:
            raise ValueError("每页条数必须是 1 到 100 的整数")
        if type(page) is not int or not 1 <= page <= MAX_PAGE:
            raise ValueError(f"页码必须是 1 到 {MAX_PAGE} 的整数")
        plugin_dir = getattr(context, "plugin_dir", None)
        if not isinstance(plugin_dir, Path):
            raise RuntimeError("dictionary plugin directory is unavailable")
        return cast(
            str,
            await run_sync(
                _query_astrodict_sync,
                cleaned_query,
                plugin_dir,
                exact_match,
                max_results,
                page,
            ),
        )
    except ValueError as exc:
        return f"❌ {exc}"
    except Exception as exc:
        return cast(
            str,
            public_error_message(
                context,
                exc,
                logger=context.logger,
                component="dict.search",
            ),
        )


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Messages:
    """解析一条词典命令，并返回稳定的公开消息。"""

    del command, event
    try:
        request = _parse_request(args)
        if request.action == "help":
            return segments(HELP_TEXT)
        context.logger.info(
            "天文词典查询: query_chars=%d exact=%s page=%d page_size=%d",
            len(request.query),
            request.exact_match,
            request.page,
            request.max_results,
        )
        result = await query_astrodict(
            request.query,
            context,
            exact_match=request.exact_match,
            max_results=request.max_results,
            page=request.page,
        )
        return segments(result)
    except ValueError as exc:
        return segments(f"❌ {exc}")
    except Exception as exc:
        return cast(
            Messages,
            public_error_response(
                context,
                exc,
                logger=context.logger,
                component="dict.handle",
            ),
        )
