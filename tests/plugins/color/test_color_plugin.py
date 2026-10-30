"""
Color 插件单元测试
"""

import json
import logging
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from core.bounded_file_cache import FileCacheLimits
from core.router import build_command_catalog_node
from plugins.color import convert as color_convert
from plugins.color import data_manager as color_data
from plugins.color import image_gen as color_image_gen
from plugins.color import main as color_main
from plugins.color import query as color_query
from plugins.color import stellar as color_stellar
from tests.helpers.assertions import text_segments_text
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
# 和生产一样从 plugin.json 发布的命令目录；插件按它判断写入是不是走了 add、delete 节点。
COLOR_ROOT = build_command_catalog_node(
    "color",
    json.loads((ROOT / "plugins" / "color" / "plugin.json").read_text(encoding="utf-8"))[
        "commands"
    ][0],
    root=True,
)

# ============================================================
# convert 模块测试
# ============================================================


class TestColorConvert:
    """颜色转换测试"""

    def test_rgb_to_hex(self):
        """测试 RGB 转 HEX"""
        result = color_convert.rgb_to_hex([255, 128, 0])
        assert result.upper() == "#FF8000"

    def test_rgb_to_hex_normalized(self):
        """测试 RGB 转 HEX 规范化"""
        result = color_convert.rgb_to_hex([255, 255, 255])
        assert result.upper() == "#FFFFFF"

    def test_hex_to_rgb(self):
        """测试 HEX 转 RGB"""
        result = color_convert.hex_to_rgb("#FF8000")
        assert result == [255, 128, 0]

    def test_hex_to_rgb_without_hash(self):
        """测试无 # 前缀的 HEX 转 RGB"""
        result = color_convert.hex_to_rgb("FF8000")
        assert result == [255, 128, 0]

    def test_hex_to_rgb_lowercase(self):
        """测试小写 HEX 转 RGB"""
        result = color_convert.hex_to_rgb("#ff8000")
        assert result == [255, 128, 0]

    def test_rgb_to_cmyk(self):
        """测试 RGB 转 CMYK"""
        result = color_convert.rgb_to_cmyk([255, 0, 0])
        # 纯红色
        assert len(result) == 4
        assert all(isinstance(x, (int, float)) for x in result)

    def test_cmyk_to_rgb(self):
        assert color_convert.cmyk_to_rgb([0, 100, 100, 0]) == [255, 0, 0]

    def test_validate_rgb_valid(self):
        """测试有效 RGB 验证"""
        is_valid, error = color_convert.validate_rgb([255, 128, 0])
        assert is_valid is True
        assert error is None

    def test_validate_rgb_invalid_range(self):
        """测试无效 RGB 范围"""
        is_valid, error = color_convert.validate_rgb([300, 128, 0])
        assert is_valid is False
        assert error is not None

    def test_validate_rgb_wrong_length(self):
        """测试 RGB 长度错误"""
        is_valid, _error = color_convert.validate_rgb([255, 128])
        assert is_valid is False

    @pytest.mark.parametrize("value", [True, 1.0, "1"])
    def test_validate_rgb_rejects_implicit_integer_coercion(self, value):
        is_valid, _error = color_convert.validate_rgb([value, 2, 3])
        assert is_valid is False

    def test_validate_cmyk_valid(self):
        """测试有效 CMYK 验证"""
        is_valid, _error = color_convert.validate_cmyk([0, 100, 100, 0])
        assert is_valid is True

    def test_validate_cmyk_invalid_range(self):
        """测试无效 CMYK 范围"""
        is_valid, _error = color_convert.validate_cmyk([0, 150, 100, 0])
        assert is_valid is False

    @pytest.mark.parametrize("value", ["##fff", "#１２３", " #fff", "#ffff"])
    def test_hex_parser_requires_one_exact_ascii_form(self, value):
        with pytest.raises(ValueError):
            color_convert.hex_to_rgb(value)

    def test_converters_reject_unvalidated_rgb(self):
        with pytest.raises(ValueError):
            color_convert.rgb_to_hex([True, 2, 3])
        with pytest.raises(ValueError):
            color_convert.rgb_to_cmyk([256, 2, 3])


# ============================================================
# query 模块测试
# ============================================================


class TestColorQuery:
    """颜色查询测试"""

    @pytest.fixture
    def sample_colors(self):
        """示例颜色数据"""
        return [
            {"name": "胭脂", "RGB": [213, 69, 71], "CMYK": [15, 85, 75, 0], "hex": "#D54547"},
            {"name": "朱砂", "RGB": [255, 89, 86], "CMYK": [0, 75, 70, 0], "hex": "#FF5956"},
            {"name": "海棠红", "RGB": [231, 113, 86], "CMYK": [8, 65, 75, 0], "hex": "#E77156"},
        ]

    def test_find_by_name_found(self, sample_colors):
        """测试按名称查找（找到）"""
        result = color_query.find_by_name(sample_colors, "胭脂")
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_name_not_found(self, sample_colors):
        """测试按名称查找（未找到）"""
        result = color_query.find_by_name(sample_colors, "不存在")
        assert result is None

    def test_find_by_rgb_found(self, sample_colors):
        """测试按 RGB 查找（找到）"""
        result = color_query.find_by_rgb(sample_colors, [213, 69, 71])
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_rgb_not_found(self, sample_colors):
        """测试按 RGB 查找（未找到）"""
        result = color_query.find_by_rgb(sample_colors, [0, 0, 0])
        assert result is None

    def test_find_by_hex_found(self, sample_colors):
        """测试按 HEX 查找（找到）"""
        result = color_query.find_by_hex(sample_colors, "#D54547")
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_hex_not_found(self, sample_colors):
        """测试按 HEX 查找（未找到）"""
        result = color_query.find_by_hex(sample_colors, "#000000")
        assert result is None

    def test_find_by_cmyk_found(self, sample_colors):
        """测试按 CMYK 查找（找到）"""
        result = color_query.find_by_cmyk(sample_colors, [15, 85, 75, 0])
        assert result is not None
        assert result["name"] == "胭脂"

    def test_find_by_keyword(self, sample_colors):
        """测试按关键词搜索"""
        results = color_query.find_by_keyword(sample_colors, "红")
        assert len(results) >= 1
        assert "海棠红" in [r["name"] for r in results]

    def test_keyword_search_supports_pinyin_and_relevance_order(self):
        colors = [
            {
                "name"  : "中灰",
                "pinyin": "zhonghui",
                "RGB"   : [1, 1, 1],
                "CMYK"  : [0, 0, 0, 99],
                "hex"   : "#010101",
            },
            {
                "name"  : "红汞红",
                "pinyin": "honggonghong",
                "RGB"   : [2, 2, 2],
                "CMYK"  : [0, 0, 0, 98],
                "hex"   : "#020202",
            },
        ]

        assert [color["name"] for color in color_query.find_by_keyword(colors, "hong")] == [
            "红汞红",
            "中灰",
        ]

    def test_nearest_rgb_uses_perceptual_distance_and_stable_ties(self):
        # 第二项的 RGB 距离更大，CIE76 距离更小；同值副本验证稳定顺序。
        colors = [
            {"name": "green_shift", "RGB": [128, 110, 128]},
            {"name": "red_shift", "RGB": [148, 128, 128]},
            {"name": "same_red", "RGB": [148, 128, 128]},
        ]
        match = color_query.find_nearest_by_rgb(colors, [128, 128, 128])
        assert match is not None
        assert match[0]["name"] == "red_shift"
        assert match[1] == pytest.approx(8.3281490011)


# ============================================================
# data_manager 模块测试
# ============================================================


class TestColorDataManager:
    """颜色数据管理器测试"""

    def test_format_color_info(self):
        """测试格式化颜色信息"""
        color = {
            "name": "胭脂",
            "RGB" : [213, 69, 71],
            "CMYK": [15, 85, 75, 0],
            "hex" : "#D54547",
        }
        result = color_data.format_color_info(color)
        assert "胭脂" in result
        assert "#D54547" in result or "213, 69, 71" in result

    def test_load_colors_reuses_cached_builtin_palette(self, tmp_path, monkeypatch):
        calls      = {"builtin": 0}
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        builtin_file = plugin_dir / "color.json"
        builtin_file.write_text(
            json.dumps([{"name": "胭脂"}], ensure_ascii=False), encoding="utf-8"
        )

        def _fake_load_json(path, default, **_kwargs):
            if Path(path) == builtin_file:
                calls["builtin"] += 1
                return [
                    {
                        "name"  : "胭脂",
                        "pinyin": "yanzhi",
                        "RGB"   : [213, 69, 71],
                        "CMYK"  : [15, 85, 75, 0],
                        "hex"   : "#d54547",
                    }
                ]
            return default

        monkeypatch.setattr(color_data, "load_json", _fake_load_json)
        context = MagicMock(plugin_dir=plugin_dir, data_dir=data_dir, logger=MagicMock())

        color_data.load_colors(context)
        color_data.load_colors(context)

        assert calls["builtin"] == 1

    def test_bundled_palette_is_complete_and_rgb_hex_consistent(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir       = ROOT / "plugins" / "color",
            data_dir         = tmp_path,
            current_group_id = 1001,
            current_user_id  = 42,
            logger           = MagicMock(),
        )

        colors = color_data.load_colors(context)

        assert len(colors) == 526
        assert len({color["name"] for color in colors}) == 526
        assert all(color_convert.hex_to_rgb(color["hex"]) == color["RGB"] for color in colors)

    def test_builtin_load_failure_does_not_masquerade_as_custom_only_palette(
        self, monkeypatch, tmp_path
    ):
        context = SimpleNamespace(
            plugin_dir       = ROOT / "plugins" / "color",
            data_dir         = tmp_path,
            current_group_id = 1001,
            current_user_id  = 42,
            logger           = MagicMock(),
        )
        custom = MagicMock(
            return_value=[
                {
                    "name"  : "自定义色",
                    "pinyin": "",
                    "RGB"   : [1, 2, 3],
                    "hex"   : "#010203",
                    "CMYK"  : [67, 33, 0, 99],
                }
            ]
        )
        monkeypatch.setattr(
            color_data,
            "_load_builtin_colors",
            MagicMock(side_effect=ValueError("broken builtin")),
        )
        monkeypatch.setattr(color_data, "load_custom_colors", custom)

        assert color_data.load_colors(context) == []
        custom.assert_not_called()

    def test_custom_scope_fails_closed_without_authenticated_identity(self, tmp_path):
        context = SimpleNamespace(
            data_dir         = tmp_path,
            current_group_id = None,
            current_user_id  = None,
        )

        with pytest.raises(ValueError, match="requires a group or user scope"):
            color_data.load_custom_colors(context)

    def test_noop_mutation_does_not_create_or_rewrite_scope_file(self, tmp_path):
        context = SimpleNamespace(
            data_dir         = tmp_path,
            current_group_id = 1001,
            current_user_id  = 42,
        )
        custom_file = color_data._custom_file(context)

        assert color_data.mutate_custom_colors(context, lambda _colors: False) is False
        assert not custom_file.exists()

    def test_mutation_rejects_invalid_record_without_overwriting_file(self, tmp_path):
        context = SimpleNamespace(
            data_dir         = tmp_path,
            current_group_id = 1001,
            current_user_id  = 42,
        )
        custom_file = color_data._custom_file(context)
        custom_file.parent.mkdir(parents=True, exist_ok=True)
        original = [
            {
                "name"  : "合法色",
                "pinyin": "",
                "RGB"   : [1, 2, 3],
                "hex"   : "#010203",
                "CMYK"  : [67, 33, 0, 99],
            }
        ]
        custom_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(ValueError, match="RGB and HEX disagree"):
            color_data.mutate_custom_colors(
                context,
                lambda colors: colors.append(
                    {
                        "name"  : "坏色",
                        "pinyin": "",
                        "RGB"   : [1, 2, 3],
                        "hex"   : "#ffffff",
                        "CMYK"  : [0, 0, 0, 0],
                    }
                ),
            )

        assert json.loads(custom_file.read_text(encoding="utf-8")) == original


class TestColorHandleFixes:
    @pytest.mark.asyncio
    async def test_search_exposes_machine_pagination_separately_from_ui_hints(self, tmp_path):
        context = self._context(tmp_path)
        first   = await color_main.handle("color", "search 红", {}, context)
        assert first.command_outcome.next_command == "/color search 红 --page 2"
        assert "查看详情" in text_segments_text(first)
        assert "查看详情" not in first.command_outcome.result_text
        last = await color_main.handle("color", "search 红 --page 7", {}, context)
        assert last.command_outcome.state == "completed" and last.command_outcome.next_command == ""
        assert "第 7/7 页" in last.command_outcome.result_text
        empty = await color_main.handle("color", "search 绝不匹配的颜色", {}, context)
        assert (
            empty.command_outcome.state == "completed" and empty.command_outcome.next_command == ""
        )
        assert "没有结果" in empty.command_outcome.result_text

    @pytest.mark.asyncio
    async def test_color_bare_spectype_flag_lists_all_types(self, monkeypatch):
        context            = MagicMock()
        context.plugin_dir = ROOT / "plugins" / "color"
        context.data_dir   = ROOT / "plugins" / "color" / "data"
        context.logger     = MagicMock()

        called = {}

        def _fake_list(prefix, _context, *, page=1):
            called["prefix"] = prefix
            called["page"]   = page
            return [{"type": "text", "data": {"text": "ok"}}]

        monkeypatch.setattr(color_main.stellar, "list_spectral_types", _fake_list)
        result = await color_main.handle("color", "-t", {}, context)

        assert result == [{"type": "text", "data": {"text": "ok"}}]
        assert called["prefix"] == ""
        assert called["page"] == 1

    @staticmethod
    def _context(tmp_path, *, admin=False):
        del admin  # 谁能写由 Core 按 plugin.json 检查，插件里不再判断。
        return SimpleNamespace(
            plugin_dir          = ROOT / "plugins" / "color",
            data_dir            = tmp_path,
            current_group_id    = 1001,
            current_user_id     = 42,
            logger              = MagicMock(),
            get_command_catalog = lambda: (COLOR_ROOT,),
        )

    @pytest.mark.asyncio
    async def test_help_does_not_load_assets_or_create_cache(self, monkeypatch, tmp_path):
        context = self._context(tmp_path)
        load_colors = MagicMock(side_effect=AssertionError("help loaded palette"))
        monkeypatch.setattr(color_main.data_manager, "load_colors", load_colors)

        result = await color_main.handle("color", "", {}, context)

        assert "颜色工具｜526 种中国传统色" in text_segments_text(result)
        assert "/color list" in text_segments_text(result)
        load_colors.assert_not_called()
        assert not (tmp_path / "images").exists()

    @pytest.mark.asyncio
    async def test_rgb_space_form_and_picture_modifier_are_honored(self, monkeypatch, tmp_path):
        context = self._context(tmp_path)
        generate = AsyncMock(return_value=None)
        monkeypatch.setattr(color_main.image_gen, "generate_color_image", generate)

        plain    = await color_main.handle("color", "-r 1 2 3", {}, context)
        pictured = await color_main.handle("color", "-r 1 2 3 -p", {}, context)

        assert "输入：RGB 1, 2, 3" in text_segments_text(plain)
        assert "最接近的收录色" in text_segments_text(pictured)
        generate.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args",
        [
            "--unknown value",
            "-n 胭脂 --name 天青",
            "-n 胭脂 -r 1 2 3",
            "-n",
            "-p",
            "search",
            "list 0",
            "search 红 --page zero",
            '"乳白',
            "-n 胭脂 --picture=yes",
            "-r １２ 2 3",
        ],
    )
    async def test_ambiguous_or_malformed_commands_are_rejected(self, tmp_path, args):
        result = await color_main.handle("color", args, {}, self._context(tmp_path))
        assert text_segments_text(result).startswith("❌")

    @pytest.mark.asyncio
    async def test_query_text_is_not_logged(self, tmp_path, caplog):
        canary = "private-color-canary"
        with caplog.at_level(logging.INFO, logger="plugins.color.main"):
            result = await color_main.handle(
                "color",
                f"-n {canary}",
                {"user_id": 42},
                self._context(tmp_path),
            )

        assert canary in text_segments_text(result)
        assert canary not in caplog.text

    @pytest.mark.asyncio
    async def test_builtin_name_cannot_be_shadowed_by_custom_color(self, monkeypatch, tmp_path):
        context = self._context(tmp_path, admin=True)
        monkeypatch.setattr(
            color_main.image_gen,
            "generate_color_image",
            AsyncMock(return_value=None),
        )

        result = await color_main.handle(
            "color",
            "-w 乳白 1 2 3",
            {"user_id": 42},
            context,
        )

        assert "已经定义" in text_segments_text(result)
        assert not color_data._custom_file(context).exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ("-n 乳白", "#f9f4dc"),
            ("-x F9F4DC", "名称：乳白"),
            ("-c 4 5 18 0", "名称：乳白"),
            ("-a 乳", "乳白"),
            ("乳白", "名称：乳白"),
            ("rubai", "名称：乳白"),
            ("#f9f4dc", "名称：乳白"),
            ("249 244 220", "名称：乳白"),
            ("name 乳白", "名称：乳白"),
            ("hex f9f4dc", "名称：乳白"),
        ],
    )
    async def test_supported_color_queries(self, tmp_path, args, expected):
        result = await color_main.handle("color", args, {"user_id": 42}, self._context(tmp_path))
        assert expected in text_segments_text(result)

    @pytest.mark.asyncio
    async def test_catalog_is_discoverable_paginated_and_keeps_legacy_list_alias(self, tmp_path):
        context = self._context(tmp_path)

        first  = text_segments_text(await color_main.handle("color", "list", {}, context))
        second = text_segments_text(await color_main.handle("color", "-l 2", {}, context))

        assert first.startswith("🎨 颜色目录")
        assert "1. 乳白（rubai） · #f9f4dc" in first
        assert "第 1/27 页｜共 526 种" in first
        assert "下一页：/color list 2" in first
        assert "21. 蛋壳黄" in second
        assert "上一页：/color list 1" in second
        assert "下一页：/color list 3" in second

    @pytest.mark.asyncio
    async def test_name_and_pinyin_search_are_paginated_with_replayable_commands(self, tmp_path):
        context = self._context(tmp_path)

        names  = text_segments_text(await color_main.handle("color", "search 红", {}, context))
        pinyin = text_segments_text(
            await color_main.handle("color", "search hong --page 2", {}, context)
        )

        assert "第 1/7 页｜共 124 种" in names
        assert "下一页：/color search 红 --page 2" in names
        assert "第 2/7 页｜共 127 种" in pinyin
        assert "上一页：/color search hong --page 1" in pinyin

    @pytest.mark.asyncio
    async def test_non_exact_values_return_a_labeled_nearest_palette_color(self, tmp_path):
        context = self._context(tmp_path)

        by_hex  = text_segments_text(await color_main.handle("color", "#f9f4dd", {}, context))
        by_cmyk = text_segments_text(await color_main.handle("color", "4 5 18 1", {}, context))

        assert "最接近的收录色（近似匹配）" in by_hex
        assert "输入：HEX #f9f4dd" in by_hex
        assert "名称：乳白" in by_hex
        assert "输入：CMYK 4, 5, 18, 1" in by_cmyk
        assert "CIE76 色差" in by_cmyk

    @pytest.mark.asyncio
    async def test_random_color_is_bounded_to_loaded_palette(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            color_main.secrets, "randbelow", lambda upper: 0 if upper == 526 else -1
        )

        result = await color_main.handle("color", "random", {}, self._context(tmp_path))

        assert "名称：乳白" in text_segments_text(result)

    @pytest.mark.asyncio
    async def test_direct_spectral_type_routes_to_stellar_query(self, monkeypatch, tmp_path):
        query_stellar = AsyncMock(return_value=[{"type": "text", "data": {"text": "star-ok"}}])
        monkeypatch.setattr(color_main.stellar, "query_stellar_color", query_stellar)

        result = await color_main.handle("color", "G2V", {}, self._context(tmp_path))

        assert text_segments_text(result) == "star-ok"
        query_stellar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_color_can_be_added_queried_and_deleted(self, monkeypatch, tmp_path):
        context = self._context(tmp_path, admin=True)
        monkeypatch.setattr(
            color_main.image_gen,
            "generate_color_image",
            AsyncMock(return_value=None),
        )

        added = await color_main.handle(
            "color",
            "-w 审核红 #010203",
            {"user_id": 42},
            context,
        )
        queried = await color_main.handle(
            "color",
            "-n 审核红",
            {"user_id": 99},
            context,
        )
        deleted = await color_main.handle(
            "color",
            "-d 审核红",
            {"user_id": 42},
            context,
        )

        assert "添加成功" in text_segments_text(added)
        assert "#010203" in text_segments_text(queried)
        assert "已删除" in text_segments_text(deleted)
        assert color_data.load_custom_colors(context) == []

    @pytest.mark.asyncio
    async def test_readable_admin_commands_support_quoted_multiword_names(
        self, monkeypatch, tmp_path
    ):
        context = self._context(tmp_path, admin=True)
        monkeypatch.setattr(
            color_main.image_gen,
            "generate_color_image",
            AsyncMock(return_value=None),
        )

        added = await color_main.handle(
            "color",
            'add "品牌 蓝" #3366ff',
            {"user_id": 42},
            context,
        )
        queried = await color_main.handle("color", '"品牌 蓝"', {"user_id": 42}, context)
        deleted = await color_main.handle(
            "color",
            'delete "品牌 蓝"',
            {"user_id": 42},
            context,
        )

        assert "添加成功" in text_segments_text(added)
        assert "名称：品牌 蓝" in text_segments_text(queried)
        assert "已删除" in text_segments_text(deleted)

    @pytest.mark.asyncio
    async def test_custom_palette_io_runs_off_event_loop(self, monkeypatch, tmp_path):
        context = self._context(tmp_path, admin=True)
        monkeypatch.setattr(
            color_main.image_gen,
            "generate_color_image",
            AsyncMock(return_value=None),
        )
        event_loop_thread        = threading.get_ident()
        read_threads : list[int] = []
        write_threads: list[int] = []
        original_read            = color_data._read_custom_colors
        original_write           = color_data.write_json

        def tracked_read(*args, **kwargs):
            read_threads.append(threading.get_ident())
            return original_read(*args, **kwargs)

        def tracked_write(*args, **kwargs):
            write_threads.append(threading.get_ident())
            return original_write(*args, **kwargs)

        monkeypatch.setattr(color_data, "_read_custom_colors", tracked_read)
        monkeypatch.setattr(color_data, "write_json", tracked_write)

        result = await color_main.handle(
            "color",
            "-w 线程色 #010203",
            {"user_id": 42},
            context,
        )

        assert "添加成功" in text_segments_text(result)
        assert read_threads
        assert write_threads
        assert all(thread_id != event_loop_thread for thread_id in read_threads + write_threads)


class TestStellarColorData:
    def test_standard_library_parser_validates_real_table(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir = ROOT / "plugins" / "color",
            data_dir   = tmp_path,
            logger     = MagicMock(),
        )

        rows = color_stellar.load_stellar_colors(context)

        assert len(rows) == 105
        assert len({row.spectral_type for row in rows}) == 74
        assert rows[0].spectral_type == "M9.5V"
        assert rows[-1].spectral_type == "O1V"

    def test_spectral_type_list_is_unique(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir = ROOT / "plugins" / "color",
            data_dir   = tmp_path,
            logger     = MagicMock(),
        )

        result   = color_stellar.list_spectral_types("", context)
        rendered = text_segments_text(result)

        assert "共 74 个" in rendered
        assert rendered.count("M6V") == 1
        assert "第 1/3 页" in rendered
        assert "下一页：/color stars --page 2" in rendered

    def test_spectral_type_list_supports_later_pages_and_bounds(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir = ROOT / "plugins" / "color",
            data_dir   = tmp_path,
            logger     = MagicMock(),
        )

        second = text_segments_text(color_stellar.list_spectral_types("", context, page=2))
        beyond = text_segments_text(color_stellar.list_spectral_types("G", context, page=2))

        assert "G8V" in second
        assert "上一页：/color stars --page 1" in second
        assert beyond == "❌ 第 2 页超出范围（共 1 页）"

    @pytest.mark.asyncio
    async def test_duplicate_spectral_type_reports_source_grid(self, monkeypatch, tmp_path):
        context = SimpleNamespace(
            plugin_dir = ROOT / "plugins" / "color",
            data_dir   = tmp_path,
            logger     = MagicMock(),
        )
        monkeypatch.setattr(color_stellar, "generate_color_image", AsyncMock(return_value=None))

        result   = await color_stellar.query_stellar_color("m6v", context, tmp_path / "images")
        rendered = text_segments_text(result)

        assert "光谱型：M6V" in rendered
        assert "2,800-2,900 K" in rendered
        assert "#ffa548" in rendered

    def test_stellar_parser_rejects_bad_schema(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "stellar_colors.txt").write_text(
            "SpT Teff log(g) RGB Hex\nM6V nope 5.0 1,0,0 #ffffff\n",
            encoding="utf-8",
        )
        context = SimpleNamespace(plugin_dir=plugin_dir, logger=MagicMock())

        with pytest.raises(ValueError, match="numeric value"):
            color_stellar.load_stellar_colors(context)


class TestColorImageGeneration:
    @pytest.mark.skipif(
        not color_image_gen.MATPLOTLIB_AVAILABLE,
        reason="matplotlib/numpy are unavailable",
    )
    def test_real_renderer_produces_decodable_png(self):
        payload = color_image_gen._render_color_image("色卡", [1, 2, 3])

        with Image.open(BytesIO(payload)) as rendered:
            rendered.load()
            assert rendered.format == "PNG"
            assert rendered.width > 0
            assert rendered.height > 0


@pytest.mark.parametrize("blue", [100, 128, 255])
def test_color_rgb_three_digit_blue(blue):
    from plugins.color.main import _parse_custom_color

    result = _parse_custom_color(f"品牌 蓝 51 102 {blue}")
    assert result["name"] == "品牌 蓝"
    assert result["RGB"] == [51, 102, blue]


ROOT = REPOSITORY_ROOT


def _color_context(tmp_path: Path, *, admin: bool) -> SimpleNamespace:
    del admin  # 谁能写由 Core 按 plugin.json 检查，插件里不再判断。
    return SimpleNamespace(
        current_group_id    = 1001,
        current_user_id     = 42,
        data_dir            = tmp_path / "color-data",
        logger              = MagicMock(),
        plugin_dir          = ROOT / "plugins" / "color",
        get_command_catalog = lambda: (COLOR_ROOT,),
    )


def test_color_mutations_are_declared_for_bot_admins_in_the_manifest():
    nodes = {node.name: node for node in COLOR_ROOT.children}
    assert nodes["add"].permission == "bot_admin"
    assert nodes["delete"].permission == "bot_admin"
    # 解析器认的每个写入、删除词都是这两个节点的别名，Core 才能在调用插件前拦下。
    for word in ("add", "write", "添加", "新增", "-w", "--write"):
        assert COLOR_ROOT.resolve_child(word) is nodes["add"], word
    for word in ("delete", "del", "remove", "删除", "-d", "--delete"):
        assert COLOR_ROOT.resolve_child(word) is nodes["delete"], word


@pytest.mark.asyncio
async def test_color_mutations_only_run_through_the_declared_nodes(monkeypatch, tmp_path):
    context = _color_context(tmp_path, admin=False)

    async def no_image(*_args, **_kwargs):
        return None

    monkeypatch.setattr(color_main.image_gen, "generate_color_image", no_image)
    event = {"user_id": 42, "group_id": 1001}
    # 选项写在后面时 Core 解析到的是根命令（所有人可用），插件不能把它当成写入。
    bypass = await color_main.handle("color", "审核红 -w 1 2 3", event, context)
    assert "/color add" in text_segments_text(bypass)
    assert not color_data._custom_file(context).exists()

    added = await color_main.handle("color", "新增 审核红 1 2 3", event, context)
    assert "添加成功" in text_segments_text(added)
    deleted = await color_main.handle("color", "-d 审核红", event, context)
    assert "审核红" in text_segments_text(deleted)
    assert not any(color["name"] == "审核红" for color in color_data.load_custom_colors(context))


def test_color_custom_store_rejects_oversized_manual_scope_file(tmp_path):
    context = _color_context(tmp_path, admin=True)
    custom_file = color_data._custom_file(context)
    custom_file.parent.mkdir(parents=True)
    original = [{"name": str(index)} for index in range(201)]
    custom_file.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError, match="count exceeds"):
        color_data.mutate_custom_colors(context, lambda colors: colors.clear())
    assert json.loads(custom_file.read_text(encoding="utf-8")) == original


@pytest.mark.asyncio
async def test_color_image_cache_is_content_addressed_and_lru_bounded(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_render(name: str, _rgb: list[int]) -> bytes:
        calls.append(name)
        return name.encode("utf-8")

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(color_image_gen, "MATPLOTLIB_AVAILABLE", True)
    monkeypatch.setattr(color_image_gen, "_render_color_image", fake_render)
    monkeypatch.setattr(color_image_gen, "run_sync", inline)
    monkeypatch.setattr(
        color_image_gen,
        "IMAGE_CACHE_LIMITS",
        FileCacheLimits(max_entries=2, max_bytes=100, ttl_seconds=60),
    )
    context = SimpleNamespace(logger=MagicMock())
    output_dir = tmp_path / "images"
    first      = await color_image_gen.generate_color_image("first", [1, 2, 3], output_dir, context)
    await color_image_gen.generate_color_image("second", [4, 5, 6], output_dir, context)
    assert (
        await color_image_gen.generate_color_image("first", [1, 2, 3], output_dir, context) == first
    )
    await color_image_gen.generate_color_image("third", [7, 8, 9], output_dir, context)
    assert calls == ["first", "second", "third"]
    assert len(list(output_dir.glob("*.png"))) == 2
    assert Path(first).is_file()
