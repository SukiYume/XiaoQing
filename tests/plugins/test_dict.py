"""dict 插件的解析、资源完整性和查询回归测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.plugin_base import has_control_characters
from plugins.dict import main as dict_plugin
from tests.helpers.assertions import text_segments_text

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "dict"


@pytest.fixture
def context() -> SimpleNamespace:
    return SimpleNamespace(plugin_dir=PLUGIN_DIR, logger=MagicMock(), request_id="dict-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "help", "H", "帮助"])
async def test_help_does_not_load_dictionary(
    args: str,
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dict_plugin,
        "_load_direction",
        MagicMock(side_effect=AssertionError("help must not load assets")),
    )

    response = await dict_plugin.handle("dict", args, {}, context)

    assert text_segments_text(response) == dict_plugin.HELP_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("-e galaxy", "galaxy → 星系"),
        ("-e 星系", "星系 → galaxy"),
        ('-e "AB magnitude"', "AB magnitude → AB星等"),
    ],
)
async def test_real_assets_support_both_directions_and_current_terms(
    query: str,
    expected: str,
    context: SimpleNamespace,
) -> None:
    response = await dict_plugin.handle("dict", query, {}, context)
    rendered = text_segments_text(response)

    assert expected in rendered
    assert "共找到" in rendered


@pytest.mark.asyncio
async def test_fuzzy_query_counts_all_matches_but_respects_display_limit(
    context: SimpleNamespace,
) -> None:
    response = await dict_plugin.handle("dict", "-n 1 galaxy", {}, context)
    rendered = text_segments_text(response)

    assert "仅显示前 1 条" in rendered
    assert rendered.count(" → ") == 1


@pytest.mark.asyncio
async def test_exact_and_limit_options_preserve_the_complete_query(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_query(
        query: object,
        _context: object,
        exact_match: bool = False,
        max_results: int = 10,
    ) -> str:
        captured.update(
            query=query,
            exact_match=exact_match,
            max_results=max_results,
        )
        return "ok"

    monkeypatch.setattr(dict_plugin, "query_astrodict", fake_query)

    response = await dict_plugin.handle(
        "dict",
        '-n 20 "fast radio burst" --exact',
        {},
        context,
    )

    assert text_segments_text(response) == "ok"
    assert captured == {
        "query": "fast radio burst",
        "exact_match": True,
        "max_results": 20,
    }


@pytest.mark.asyncio
async def test_option_terminator_allows_option_like_query(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_query(query: object, _context: object, **kwargs: object) -> str:
        captured["query"] = query
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(dict_plugin, "query_astrodict", fake_query)

    response = await dict_plugin.handle("dict", "-e -- --nova", {}, context)

    assert text_segments_text(response) == "ok"
    assert captured == {"query": "--nova", "exact_match": True, "max_results": 10}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("--unknown galaxy", "未知选项"),
        ("-e --exact galaxy", "精确匹配选项不能重复"),
        ("-n 2 --num 3 galaxy", "显示数量选项不能重复"),
        ("-n", "显示数量选项缺少数值"),
        ("-n 0 galaxy", "1 到 100"),
        ("-n 101 galaxy", "1 到 100"),
        ("-n ２０ galaxy", "ASCII 整数"),
        ('"galaxy', "引号没有闭合"),
        ("help extra", "不接受额外参数"),
        ("galaxy\nstar", "控制字符"),
        ("a" * (dict_plugin.MAX_QUERY_CHARS + 1), "查询词不能超过"),
        ("a" * (dict_plugin.MAX_ARGUMENT_CHARS + 1), "命令参数不能超过"),
    ],
)
async def test_malformed_commands_fail_closed(
    args: str,
    expected: str,
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = MagicMock(side_effect=AssertionError("invalid command must not search"))
    monkeypatch.setattr(dict_plugin, "query_astrodict", query)

    response = await dict_plugin.handle("dict", args, {}, context)

    assert expected in text_segments_text(response)
    query.assert_not_called()


def test_parser_rejects_non_string_arguments() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        dict_plugin._parse_request(True)


@pytest.mark.asyncio
async def test_existing_200_character_query_remains_valid(context: SimpleNamespace) -> None:
    response = await dict_plugin.handle("dict", "a" * 200, {}, context)

    assert text_segments_text(response) == "在天文学词典（英译中）中未找到相关词条"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "exact_match", "max_results", "expected"),
    [
        (True, False, 10, "查询词必须是字符串"),
        (" ", False, 10, "请提供要查询的词汇"),
        ("galaxy\x00", False, 10, "查询词不能包含控制字符"),
        ("galaxy", 1, 10, "精确匹配参数必须是布尔值"),
        ("galaxy", False, True, "显示数量必须是 1 到 100 的整数"),
        ("galaxy", False, 101, "显示数量必须是 1 到 100 的整数"),
    ],
)
async def test_public_query_api_validates_exact_types(
    query: object,
    exact_match: object,
    max_results: object,
    expected: str,
    context: SimpleNamespace,
) -> None:
    result = await dict_plugin.query_astrodict(
        query,
        context,
        exact_match=exact_match,  # type: ignore[arg-type]
        max_results=max_results,  # type: ignore[arg-type]
    )

    assert expected in result


@pytest.mark.asyncio
async def test_public_query_api_hides_missing_context_details() -> None:
    context = SimpleNamespace(logger=MagicMock(), request_id="dict-missing-context")

    result = await dict_plugin.query_astrodict("galaxy", context)

    assert "XQ-PLUGIN-UNEXPECTED" in result
    assert "dict-missing-context" in result
    assert "dictionary plugin directory" not in result


@pytest.mark.asyncio
async def test_logs_do_not_include_query_text(context: SimpleNamespace) -> None:
    canary = "DICT-PRIVATE-CANARY-2d84f7"

    await dict_plugin.handle("dict", canary, {}, context)

    assert canary not in repr(context.logger.mock_calls)
    context.logger.info.assert_called_once_with(
        "天文词典查询: query_chars=%d exact=%s max=%d",
        len(canary),
        False,
        10,
    )


def test_manifest_and_every_packaged_row_are_consistent() -> None:
    asset_dir = PLUGIN_DIR / "assets"
    manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["source_version"] == "r241020"
    assert manifest["source_archive"].endswith("astrodict_241020.zip")
    assert manifest["source_archive_sha256"] == (
        "2520bb8bd4d3382b560199a708506bead5f26b167e904b9b87465efd3cc55e2e"
    )
    assert manifest["ownership"] == "中国天文学会"
    assert "MIT" not in manifest["license"]

    expected_counts = {
        "english_to_chinese": 30_094,
        "chinese_to_english": 26_770,
    }
    for direction, spec in manifest["files"].items():
        path = asset_dir / spec["filename"]
        payload = path.read_bytes()
        assert not payload.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in payload
        assert payload.endswith(b"\n")
        assert len(payload) == spec["bytes"]
        assert hashlib.sha256(payload).hexdigest() == spec["sha256"]

        lines = payload.decode("utf-8").splitlines()
        assert len(lines) == spec["entries"] == expected_counts[direction]
        pairs: set[tuple[str, str]] = set()
        for line in lines:
            fields = line.split("\t")
            assert len(fields) == 2
            source, destination = fields
            assert source and destination
            assert source == source.strip()
            assert destination == destination.strip()
            assert len(source) <= dict_plugin.MAX_SOURCE_CHARS
            assert len(destination) <= dict_plugin.MAX_DESTINATION_CHARS
            assert not has_control_characters(source + destination)
            assert (source, destination) not in pairs
            pairs.add((source, destination))


def _resource_for_payload(path: Path, payload: bytes, entries: int) -> dict_plugin.ResourceSpec:
    path.write_bytes(payload)
    return dict_plugin.ResourceSpec(
        path.name,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        entries,
    )


@pytest.mark.parametrize(
    ("payload", "entries"),
    [
        (b"one\ttwo\tthree\n", 1),
        (b"\ttwo\n", 1),
        (b" one\ttwo\n", 1),
        (b"one\ttwo\x00\n", 1),
        (b"one\ttwo\none\ttwo\n", 2),
        (b"\xff\n", 1),
        (b"\n", 1),
        (("a" * (dict_plugin.MAX_DICTIONARY_LINE_CHARS + 1) + "\tb\n").encode(), 1),
    ],
    ids=["columns", "empty", "outer", "control", "duplicate", "utf8", "blank", "overlong"],
)
def test_dictionary_parser_rejects_malformed_payloads(
    tmp_path: Path,
    payload: bytes,
    entries: int,
) -> None:
    path = tmp_path / "dictionary.txt"
    spec = _resource_for_payload(path, payload, entries)
    info = path.stat()

    with pytest.raises(dict_plugin.DictionaryDataError, match="数据校验失败"):
        dict_plugin._load_dictionary(
            path,
            spec,
            (info.st_mtime_ns, info.st_ctime_ns, info.st_size),
        )


def test_dictionary_loader_rejects_missing_digest_and_count_mismatches(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    missing_spec = dict_plugin.ResourceSpec("missing.txt", "0" * 64, 1, 1)
    with pytest.raises(dict_plugin.DictionaryDataError, match="数据校验失败"):
        dict_plugin._load_dictionary(missing, missing_spec, (0, 0, 1))

    path = tmp_path / "dictionary.txt"
    payload = b"one\ttwo\n"
    valid_spec = _resource_for_payload(path, payload, 1)
    info = path.stat()
    fingerprint = (info.st_mtime_ns, info.st_ctime_ns, info.st_size)

    wrong_digest = dict_plugin.ResourceSpec(path.name, "0" * 64, len(payload), 1)
    with pytest.raises(dict_plugin.DictionaryDataError, match="数据校验失败"):
        dict_plugin._load_dictionary(path, wrong_digest, fingerprint)

    wrong_count = dict_plugin.ResourceSpec(
        path.name,
        valid_spec.sha256,
        len(payload),
        2,
    )
    with pytest.raises(dict_plugin.DictionaryDataError, match="数据校验失败"):
        dict_plugin._load_dictionary(path, wrong_count, fingerprint)


def test_resource_manifest_rejects_paths_and_unbounded_sizes() -> None:
    base = {
        "schema_version": 1,
        "files": {
            "english_to_chinese": {
                "filename": "../outside.txt",
                "sha256": "0" * 64,
                "bytes": 1,
                "entries": 1,
            }
        },
    }
    with pytest.raises(dict_plugin.DictionaryDataError, match="资源清单无效"):
        dict_plugin._resource_spec(base, "english_to_chinese")

    base["files"]["english_to_chinese"]["filename"] = "dictionary.txt"
    base["files"]["english_to_chinese"]["bytes"] = dict_plugin.MAX_DICTIONARY_BYTES + 1
    with pytest.raises(dict_plugin.DictionaryDataError, match="资源清单无效"):
        dict_plugin._resource_spec(base, "english_to_chinese")

    base["schema_version"] = True
    base["files"]["english_to_chinese"]["bytes"] = 1
    with pytest.raises(dict_plugin.DictionaryDataError, match="资源清单无效"):
        dict_plugin._resource_spec(base, "english_to_chinese")


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{",
        b"x" * (dict_plugin.MAX_MANIFEST_BYTES + 1),
    ],
    ids=["non-object", "invalid-json", "oversized"],
)
def test_invalid_manifest_files_fail_closed(tmp_path: Path, payload: bytes) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "manifest.json").write_bytes(payload)

    result = dict_plugin._query_astrodict_sync("galaxy", tmp_path, False, 10)

    assert result == "天文学词典资源清单无效；请重新安装完整发行包"


def test_missing_resource_returns_installation_message(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    manifest = {
        "schema_version": 1,
        "files": {
            "english_to_chinese": {
                "filename": "missing.txt",
                "sha256": "0" * 64,
                "bytes": 1,
                "entries": 1,
            }
        },
    }
    (assets / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = dict_plugin._query_astrodict_sync("galaxy", tmp_path, False, 10)

    assert result == ("天文学词典数据文件不存在: missing.txt；请重新安装包含 package data 的发行包")


def test_resource_with_wrong_size_returns_validation_message(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "dictionary.txt").write_bytes(b"x")
    manifest = {
        "schema_version": 1,
        "files": {
            "english_to_chinese": {
                "filename": "dictionary.txt",
                "sha256": hashlib.sha256(b"xx").hexdigest(),
                "bytes": 2,
                "entries": 1,
            }
        },
    }
    (assets / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = dict_plugin._query_astrodict_sync("galaxy", tmp_path, False, 10)

    assert result == "天文学词典数据校验失败: dictionary.txt"
