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

from plugins.color import convert as color_convert
from plugins.color import data_manager as color_data_manager
from plugins.color import image_gen as color_image_gen
from plugins.color import main as color_main
from plugins.color import query as color_query
from plugins.color import stellar as color_stellar
from tests.helpers.assertions import text_segments_text

ROOT = Path(__file__).resolve().parent.parent.parent

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


# ============================================================
# data_manager 模块测试
# ============================================================


class TestColorDataManager:
    """颜色数据管理器测试"""

    def test_format_color_info(self):
        """测试格式化颜色信息"""
        color = {
            "name": "胭脂",
            "RGB": [213, 69, 71],
            "CMYK": [15, 85, 75, 0],
            "hex": "#D54547",
        }
        result = color_data_manager.format_color_info(color)
        assert "胭脂" in result
        assert "#D54547" in result or "213, 69, 71" in result

    def test_load_colors_reuses_cached_builtin_palette(self, tmp_path, monkeypatch):
        calls = {"builtin": 0}
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
                        "name": "胭脂",
                        "pinyin": "yanzhi",
                        "RGB": [213, 69, 71],
                        "CMYK": [15, 85, 75, 0],
                        "hex": "#d54547",
                    }
                ]
            return default

        monkeypatch.setattr(color_data_manager, "load_json", _fake_load_json)
        context = MagicMock(plugin_dir=plugin_dir, data_dir=data_dir, logger=MagicMock())

        color_data_manager.load_colors(context)
        color_data_manager.load_colors(context)

        assert calls["builtin"] == 1

    def test_bundled_palette_is_complete_and_rgb_hex_consistent(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir=ROOT / "plugins" / "color",
            data_dir=tmp_path,
            current_group_id=1001,
            current_user_id=42,
            logger=MagicMock(),
        )

        colors = color_data_manager.load_colors(context)

        assert len(colors) == 526
        assert len({color["name"] for color in colors}) == 526
        assert all(color_convert.hex_to_rgb(color["hex"]) == color["RGB"] for color in colors)

    def test_custom_scope_fails_closed_without_authenticated_identity(self, tmp_path):
        context = SimpleNamespace(
            data_dir=tmp_path,
            current_group_id=None,
            current_user_id=None,
        )

        with pytest.raises(ValueError, match="requires a group or user scope"):
            color_data_manager.load_custom_colors(context)

    def test_noop_mutation_does_not_create_or_rewrite_scope_file(self, tmp_path):
        context = SimpleNamespace(
            data_dir=tmp_path,
            current_group_id=1001,
            current_user_id=42,
        )
        custom_file = color_data_manager._custom_file(context)

        assert color_data_manager.mutate_custom_colors(context, lambda _colors: False) is False
        assert not custom_file.exists()

    def test_mutation_rejects_invalid_record_without_overwriting_file(self, tmp_path):
        context = SimpleNamespace(
            data_dir=tmp_path,
            current_group_id=1001,
            current_user_id=42,
        )
        custom_file = color_data_manager._custom_file(context)
        custom_file.parent.mkdir(parents=True, exist_ok=True)
        original = [
            {
                "name": "合法色",
                "pinyin": "",
                "RGB": [1, 2, 3],
                "hex": "#010203",
                "CMYK": [67, 33, 0, 99],
            }
        ]
        custom_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(ValueError, match="RGB and HEX disagree"):
            color_data_manager.mutate_custom_colors(
                context,
                lambda colors: colors.append(
                    {
                        "name": "坏色",
                        "pinyin": "",
                        "RGB": [1, 2, 3],
                        "hex": "#ffffff",
                        "CMYK": [0, 0, 0, 0],
                    }
                ),
            )

        assert json.loads(custom_file.read_text(encoding="utf-8")) == original


class TestColorHandleFixes:
    @pytest.mark.asyncio
    async def test_color_bare_spectype_flag_lists_all_types(self, monkeypatch):
        context = MagicMock()
        context.plugin_dir = ROOT / "plugins" / "color"
        context.data_dir = ROOT / "plugins" / "color" / "data"
        context.logger = MagicMock()

        called = {}

        def _fake_list(prefix, _context):
            called["prefix"] = prefix
            return [{"type": "text", "data": {"text": "ok"}}]

        monkeypatch.setattr(color_main.stellar, "list_spectral_types", _fake_list)
        result = await color_main.handle("color", "-t", {}, context)

        assert result == [{"type": "text", "data": {"text": "ok"}}]
        assert called["prefix"] == ""

    @staticmethod
    def _context(tmp_path, *, admin=False):
        return SimpleNamespace(
            plugin_dir=ROOT / "plugins" / "color",
            data_dir=tmp_path,
            current_group_id=1001,
            current_user_id=42,
            logger=MagicMock(),
            is_global_admin=lambda user_id=None: admin and user_id == 42,
        )

    @pytest.mark.asyncio
    async def test_help_does_not_load_assets_or_create_cache(self, monkeypatch, tmp_path):
        context = self._context(tmp_path)
        load_colors = MagicMock(side_effect=AssertionError("help loaded palette"))
        monkeypatch.setattr(color_main.data_manager, "load_colors", load_colors)

        result = await color_main.handle("color", "", {}, context)

        assert "中国传统色彩查询" in text_segments_text(result)
        load_colors.assert_not_called()
        assert not (tmp_path / "images").exists()

    @pytest.mark.asyncio
    async def test_rgb_space_form_and_picture_modifier_are_honored(self, monkeypatch, tmp_path):
        context = self._context(tmp_path)
        generate = AsyncMock(return_value=None)
        monkeypatch.setattr(color_main.image_gen, "generate_color_image", generate)

        plain = await color_main.handle("color", "-r 1 2 3", {}, context)
        pictured = await color_main.handle("color", "-r 1 2 3 -p", {}, context)

        assert "RGB: [1, 2, 3]" in text_segments_text(plain)
        assert "RGB: [1, 2, 3]" in text_segments_text(pictured)
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
            "-a 红 extra",
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
        assert not color_data_manager._custom_file(context).exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ("-n 乳白", "#f9f4dc"),
            ("-x F9F4DC", "name: 乳白"),
            ("-c 4 5 18 0", "name: 乳白"),
            ("-a 乳", "乳白"),
        ],
    )
    async def test_read_query_matrix(self, tmp_path, args, expected):
        result = await color_main.handle("color", args, {"user_id": 42}, self._context(tmp_path))
        assert expected in text_segments_text(result)

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
        assert color_data_manager.load_custom_colors(context) == []

    @pytest.mark.asyncio
    async def test_custom_palette_io_runs_off_event_loop(self, monkeypatch, tmp_path):
        context = self._context(tmp_path, admin=True)
        monkeypatch.setattr(
            color_main.image_gen,
            "generate_color_image",
            AsyncMock(return_value=None),
        )
        event_loop_thread = threading.get_ident()
        read_threads: list[int] = []
        write_threads: list[int] = []
        original_read = color_data_manager._read_custom_colors
        original_write = color_data_manager.write_json

        def tracked_read(*args, **kwargs):
            read_threads.append(threading.get_ident())
            return original_read(*args, **kwargs)

        def tracked_write(*args, **kwargs):
            write_threads.append(threading.get_ident())
            return original_write(*args, **kwargs)

        monkeypatch.setattr(color_data_manager, "_read_custom_colors", tracked_read)
        monkeypatch.setattr(color_data_manager, "write_json", tracked_write)

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
            plugin_dir=ROOT / "plugins" / "color",
            data_dir=tmp_path,
            logger=MagicMock(),
        )

        rows = color_stellar.load_stellar_colors(context)

        assert len(rows) == 105
        assert len({row.spectral_type for row in rows}) == 74
        assert rows[0].spectral_type == "M9.5V"
        assert rows[-1].spectral_type == "O1V"

    def test_spectral_type_list_is_unique(self, tmp_path):
        context = SimpleNamespace(
            plugin_dir=ROOT / "plugins" / "color",
            data_dir=tmp_path,
            logger=MagicMock(),
        )

        result = color_stellar.list_spectral_types("", context)
        rendered = text_segments_text(result)

        assert "共 74 个" in rendered
        assert rendered.count("M6V") == 1

    @pytest.mark.asyncio
    async def test_duplicate_spectral_type_reports_source_grid(self, monkeypatch, tmp_path):
        context = SimpleNamespace(
            plugin_dir=ROOT / "plugins" / "color",
            data_dir=tmp_path,
            logger=MagicMock(),
        )
        monkeypatch.setattr(color_stellar, "generate_color_image", AsyncMock(return_value=None))

        result = await color_stellar.query_stellar_color("m6v", context, tmp_path / "images")
        rendered = text_segments_text(result)

        assert "光谱型: M6V" in rendered
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
