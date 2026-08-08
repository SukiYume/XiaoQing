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

from core.args import tokenize
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
MAX_DICTIONARY_LINE_CHARS = 2_048
MAX_SOURCE_CHARS = 256
MAX_DESTINATION_CHARS = 1_024

_HELP_ALIASES = frozenset({"help", "h", "list", "l", "帮助"})
_EXACT_OPTIONS = frozenset({"-e", "--exact"})
_LIMIT_OPTIONS = frozenset({"-n", "--num"})
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_POSITIVE_INTEGER_PATTERN = re.compile(r"[1-9][0-9]{0,2}\Z")
_CHINESE_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")

Messages = list[dict[str, Any]]
QueryDirection = Literal["chinese_to_english", "english_to_chinese"]

HELP_TEXT = """📖 天文学词典

查询中国天文学会天文学名词审定委员会发布的中英天文学名词。

用法
/dict <词汇>  模糊查询；多词按“全部包含”匹配
/dict -e <词汇>  精确匹配
/dict -n <1-100> <词汇>  指定最多显示条数
/dict -- <以连字符开头的词汇>  停止解析选项
/dict help  显示帮助

示例
/dict galaxy
/dict 星系
/dict -e "fast radio burst"
/dict -n 20 star"""


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
    if _POSITIVE_INTEGER_PATTERN.fullmatch(value) is None:
        raise ValueError("显示数量必须是 1 到 100 的 ASCII 整数")
    parsed = int(value)
    if parsed > MAX_RESULTS:
        raise ValueError("显示数量必须是 1 到 100 的 ASCII 整数")
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
    max_results = DEFAULT_RESULTS
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
                raise ValueError("显示数量选项不能重复")
            index += 1
            if index >= len(tokens):
                raise ValueError("显示数量选项缺少数值")
            max_results = _parse_limit(tokens[index])
            limit_seen = True
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


def _query_astrodict_sync(
    query: str,
    plugin_dir: Path,
    exact_match: bool,
    max_results: int,
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
    matches: list[DictionaryEntry] = []
    total_found = 0
    for entry in entries:
        matched = (
            entry.source_folded == folded
            if exact_match
            else all(keyword in entry.source_folded for keyword in keywords)
        )
        if not matched:
            continue
        total_found += 1
        if len(matches) < max_results:
            matches.append(entry)
    if not matches:
        return f"在天文学词典（{direction_label}）中未找到相关词条"

    lines = [
        f"{index}. {entry.source} → {entry.destination}" for index, entry in enumerate(matches, 1)
    ]
    suffix = f"，仅显示前 {max_results} 条" if total_found > max_results else ""
    lines.append(f"\n共找到 {total_found} 条结果{suffix}")
    return "\n".join(lines)


async def query_astrodict(
    query: object,
    context: PluginContextProtocol,
    exact_match: bool = False,
    max_results: int = DEFAULT_RESULTS,
) -> str:
    """校验公开调用参数，并在线程池中完成文件读取与词条扫描。"""

    try:
        cleaned_query = _clean_query(query)
        if type(exact_match) is not bool:
            raise ValueError("精确匹配参数必须是布尔值")
        if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS:
            raise ValueError("显示数量必须是 1 到 100 的整数")
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
            "天文词典查询: query_chars=%d exact=%s max=%d",
            len(request.query),
            request.exact_match,
            request.max_results,
        )
        result = await query_astrodict(
            request.query,
            context,
            exact_match=request.exact_match,
            max_results=request.max_results,
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
