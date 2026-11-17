"""
Color 插件单元测试
"""

import asyncio
import json
import logging
import os
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

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
from tests.helpers.assertions import logged_events, text_segments_text
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

    @pytest.mark.parametrize(
        ("rgb", "expected"),
        [
            pytest.param([255, 128, 0], "#FF8000", id="orange"),
            pytest.param([255, 255, 255], "#FFFFFF", id="white"),
        ],
    )
    def test_rgb_to_hex(self, rgb, expected):
        """测试 RGB 转 HEX"""
        assert color_convert.rgb_to_hex(rgb).upper() == expected

    @pytest.mark.parametrize(
        "value", ["#FF8000", "FF8000", "#ff8000"], ids=["upper", "no_hash", "lower"]
    )
    def test_hex_to_rgb(self, value):
        """测试 HEX 转 RGB：大小写和有无 # 前缀都接受"""
        assert color_convert.hex_to_rgb(value) == [255, 128, 0]

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

    def test_load_colors_reuses_cached_builtin_palette(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        builtin_file = plugin_dir / "color.json"

        def palette(name: str) -> str:
            record = {
                "name"  : name,
                "pinyin": "yanzhi",
                "RGB"   : [213, 69, 71],
                "CMYK"  : [15, 85, 75, 0],
                "hex"   : "#d54547",
            }
            return json.dumps([record], ensure_ascii=False)

        builtin_file.write_text(palette("胭脂"), encoding="utf-8")
        stamp   = builtin_file.stat()
        context = SimpleNamespace(
            plugin_dir      = plugin_dir,
            data_dir        = data_dir,
            current_user_id = 42,
        )

        assert [color["name"] for color in color_data.load_colors(context)] == ["胭脂"]

        # 内容换成同样字节数的另一份，并把修改时间还原：文件身份没变，第二次加载应当复用缓存。
        builtin_file.write_text(palette("朱砂"), encoding="utf-8")
        os.utime(builtin_file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert builtin_file.stat().st_size == stamp.st_size

        assert [color["name"] for color in color_data.load_colors(context)] == ["胭脂"]

    def test_bundled_palette_is_complete_and_rgb_hex_consistent(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir       = ROOT / "plugins" / "color",
            data_dir         = tmp_path,
            current_group_id = 1001,
            current_user_id  = 42,
        )

        colors = color_data.load_colors(context)

        assert len(colors) == 526
        assert len({color["name"] for color in colors}) == 526
        assert all(color_convert.hex_to_rgb(color["hex"]) == color["RGB"] for color in colors)

    def test_builtin_load_failure_does_not_masquerade_as_custom_only_palette(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "color.json").write_text("{broken builtin", encoding="utf-8")
        context = SimpleNamespace(
            plugin_dir       = plugin_dir,
            data_dir         = tmp_path / "data",
            current_group_id = 1001,
            current_user_id  = 42,
        )
        color_data.mutate_custom_colors(
            context,
            lambda colors: colors.append(
                {
                    "name"  : "自定义色",
                    "pinyin": "",
                    "RGB"   : [1, 2, 3],
                    "hex"   : "#010203",
                    "CMYK"  : [67, 33, 0, 99],
                }
            ),
        )
        assert [color["name"] for color in color_data.load_custom_colors(context)] == ["自定义色"]

        assert color_data.load_colors(context) == []

    def test_custom_scope_fails_closed_without_authenticated_identity(self, tmp_path):
        context = SimpleNamespace(
            data_dir         = tmp_path,
            current_group_id = None,
            current_user_id  = None,
        )

        with pytest.raises(ValueError, match="requires a group or user scope"):
            color_data.load_custom_colors(context)

    @pytest.mark.parametrize("group_id", ["../escape", 0, -5, True, 2**63])
    def test_invalid_scope_id_never_reaches_a_file_path_and_keeps_the_bundled_palette(
        self, tmp_path, group_id, caplog
    ):
        data_dir = tmp_path / "invalid"
        context  = SimpleNamespace(
            plugin_dir       = ROOT / "plugins" / "color",
            data_dir         = data_dir,
            current_group_id = group_id,
            current_user_id  = 42,
        )
        # 同一个插件目录、合格的群号、没有自定义色：得到的就是内置色库。
        bundled = color_data.load_colors(
            SimpleNamespace(
                **{**vars(context), "current_group_id": 123, "data_dir": tmp_path / "ok"}
            )
        )

        with pytest.raises(ValueError, match="positive 64-bit integer"):
            color_data.load_custom_colors(context)
        with caplog.at_level(logging.ERROR):
            colors = color_data.load_colors(context)

        # 身份编号不合格时，自定义色不读不写任何文件，内置色库照常可用，失败记进日志。
        assert colors and colors == bundled
        assert not data_dir.exists() or not list(data_dir.iterdir())
        assert logged_events(caplog, "public_error")

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


class TestColorHandle:
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
    async def test_color_bare_spectype_flag_lists_all_types(self, tmp_path):
        result = await color_main.handle("color", "-t", {}, self._context(tmp_path))

        rendered = text_segments_text(result)
        assert "共 74 个" in rendered
        assert "第 1/3 页" in rendered

    @staticmethod
    def _context(tmp_path):
        return SimpleNamespace(
            plugin_dir          = ROOT / "plugins" / "color",
            data_dir            = tmp_path,
            current_group_id    = 1001,
            current_user_id     = 42,
            get_command_catalog = lambda: (COLOR_ROOT,),
        )

    @pytest.mark.asyncio
    async def test_help_does_not_load_assets_or_create_cache(self, tmp_path):
        context = self._context(tmp_path)
        # 插件目录里没有颜色库：help 若去加载它，回复会变成“颜色数据加载失败”。
        context.plugin_dir = tmp_path / "missing-assets"

        result = await color_main.handle("color", "", {}, context)

        assert "颜色工具｜526 种中国传统色" in text_segments_text(result)
        assert "/color list" in text_segments_text(result)
        assert not (tmp_path / "images").exists()

    @pytest.mark.asyncio
    async def test_rgb_space_form_and_picture_modifier_are_honored(self, tmp_path):
        context = self._context(tmp_path)

        plain    = await color_main.handle("color", "-r 1 2 3", {}, context)
        pictured = await color_main.handle("color", "-r 1 2 3 -p", {}, context)

        assert "输入：RGB 1, 2, 3" in text_segments_text(plain)
        assert "最接近的收录色" in text_segments_text(pictured)
        assert [segment["type"] for segment in plain].count("image") == 0
        assert [segment["type"] for segment in pictured].count("image") == 1

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
        with caplog.at_level(logging.DEBUG, "plugins.color"):
            result = await color_main.handle(
                "color",
                f"-n {canary}",
                {"user_id": 42},
                self._context(tmp_path),
            )

        assert canary in text_segments_text(result)
        assert logged_events(caplog, "name.query")
        assert canary not in caplog.text

    @pytest.mark.asyncio
    async def test_builtin_name_cannot_be_shadowed_by_custom_color(self, tmp_path):
        context = self._context(tmp_path)

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
    async def test_direct_spectral_type_routes_to_stellar_query(self, tmp_path):
        result = await color_main.handle("color", "G2V", {}, self._context(tmp_path))

        assert "光谱型：G2V" in text_segments_text(result)
        assert [segment["type"] for segment in result].count("image") == 1

    @pytest.mark.asyncio
    async def test_custom_color_can_be_added_queried_and_deleted(self, tmp_path):
        context = self._context(tmp_path)

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
    async def test_readable_admin_commands_support_quoted_multiword_names(self, tmp_path):
        context = self._context(tmp_path)

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
        context                    = self._context(tmp_path)
        custom_file                = color_data._custom_file(context)
        event_loop_thread          = threading.get_ident()
        replace_threads: list[int] = []
        real_replace               = os.replace

        def tracked_replace(source, destination, *args, **kwargs):
            if Path(destination) == custom_file:
                replace_threads.append(threading.get_ident())
            return real_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "replace", tracked_replace)

        # 读取：测试线程（即事件循环线程）持有自定义色库的进程内锁。锁是可重入的，
        # 只有当读取真的跑在别的线程上，查询命令才会被它挡住。
        with color_data._CUSTOM_COLORS_LOCK:
            query = asyncio.create_task(
                color_main.handle("color", "-n 乳白", {"user_id": 42}, context)
            )
            await asyncio.sleep(0.1)
            assert not query.done()

        assert "名称：乳白" in text_segments_text(await asyncio.wait_for(query, timeout=10))

        # 写入：落盘的 os.replace 发生在工作线程里。
        result = await color_main.handle("color", "-w 线程色 #010203", {"user_id": 42}, context)

        assert "添加成功" in text_segments_text(result)
        assert [color["name"] for color in color_data.load_custom_colors(context)] == ["线程色"]
        assert replace_threads
        assert all(thread_id != event_loop_thread for thread_id in replace_threads)


class TestStellarColorData:
    def test_standard_library_parser_validates_real_table(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir = ROOT / "plugins" / "color",
            data_dir   = tmp_path,
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
        )

        second = text_segments_text(color_stellar.list_spectral_types("", context, page=2))
        beyond = text_segments_text(color_stellar.list_spectral_types("G", context, page=2))

        assert "G8V" in second
        assert "上一页：/color stars --page 1" in second
        assert beyond == "❌ 第 2 页超出范围（共 1 页）"

    @pytest.mark.asyncio
    async def test_duplicate_spectral_type_reports_source_grid(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir = ROOT / "plugins" / "color",
            data_dir   = tmp_path,
        )

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
        context = SimpleNamespace(plugin_dir=plugin_dir)

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


def _color_context(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        current_group_id    = 1001,
        current_user_id     = 42,
        data_dir            = tmp_path / "color-data",
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
async def test_color_mutations_only_run_through_the_declared_nodes(tmp_path):
    context = _color_context(tmp_path)
    event   = {"user_id": 42, "group_id": 1001}
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
    context     = _color_context(tmp_path)
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

    monkeypatch.setattr(color_image_gen, "MATPLOTLIB_AVAILABLE", True)
    monkeypatch.setattr(color_image_gen, "_render_color_image", fake_render)
    monkeypatch.setattr(
        color_image_gen,
        "IMAGE_CACHE_LIMITS",
        FileCacheLimits(max_entries=2, max_bytes=100, ttl_seconds=60),
    )
    context    = SimpleNamespace()
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
