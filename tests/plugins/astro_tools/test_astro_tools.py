"""
astro_tools 插件单元测试
"""

from unittest.mock import AsyncMock

import pytest

from plugins.astro_tools import const as astro_const
from plugins.astro_tools import convert as astro_convert
from plugins.astro_tools import coord as astro_coord
from plugins.astro_tools import formula as astro_formula
from plugins.astro_tools import main as astro_tools
from plugins.astro_tools import obj as astro_obj
from plugins.astro_tools import redshift as astro_redshift
from plugins.astro_tools import time as astro_time
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


loaded_modules = {"time": astro_time, "obj": astro_obj}


@pytest.fixture
def mock_context():
    """模拟插件上下文"""

    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "astro_tools"
            self.data_dir   = self.plugin_dir / "data"
            self.logger     = self._create_logger()

        def _create_logger(self):
            import logging

            return logging.getLogger("test")

    return MockContext()


class TestAstroToolsHelp:
    """测试帮助功能"""

    @pytest.mark.asyncio
    async def test_help_command(self, mock_context):
        """测试 help 命令"""
        result = await astro_tools.handle("astro", "help", {}, mock_context)
        assert result is not None
        assert len(result) > 0
        result_str = str(result)
        # 帮助信息应包含天文工具相关内容
        assert any(
            keyword in result_str for keyword in ["astro", "天文", "工具", "时间", "坐标", "转换"]
        )

    @pytest.mark.asyncio
    async def test_empty_args(self, mock_context):
        """测试空参数返回帮助"""
        result = await astro_tools.handle("astro", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias", ["help", "HELP", "帮助"])
    async def test_help_with_extra_text_is_not_an_exact_help_request(self, mock_context, alias):
        result = await astro_tools.handle("astro", f"{alias} extra", {}, mock_context)

        assert f"未知命令: {alias.casefold()}" in str(result)


class TestAstroToolsTime:
    """测试时间转换功能"""

    @pytest.mark.asyncio
    async def test_time_now(self, mock_context):
        """测试获取当前时间"""
        result = await astro_tools.handle("astro", "time now", {}, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_time_help(self, mock_context):
        """测试时间帮助"""
        result = await astro_tools.handle("astro", "time help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "time" in result_str.lower() or "时间" in result_str

    @pytest.mark.asyncio
    async def test_time_numeric_unix_timestamp(self, mock_context):
        result = await loaded_modules["time"].handle_time("1706616000", mock_context)
        assert "Unix 1706616000.0" in result or "Unix 1706616000" in result
        assert "UTC" in result

    @pytest.mark.asyncio
    async def test_time_numeric_old_unix_timestamp(self, mock_context):
        result = await loaded_modules["time"].handle_time("946684800", mock_context)
        assert "Unix 946684800.0" in result or "Unix 946684800" in result

    @pytest.mark.asyncio
    async def test_time_explicit_unix_supports_early_timestamp(self, mock_context):
        result = await astro_time.handle_time("unix 0", mock_context)

        assert "Unix 0.0" in result
        assert "1970-01-01" in result

    @pytest.mark.parametrize("subcommand", ["jd nan", "mjd inf", "unix -inf"])
    @pytest.mark.asyncio
    async def test_time_rejects_non_finite_explicit_values(self, mock_context, subcommand):
        result = await astro_time.handle_time(subcommand, mock_context)

        assert result.startswith("无效")


class TestAstroToolsCoord:
    """测试坐标转换功能"""

    @pytest.mark.asyncio
    async def test_coord_help(self, mock_context):
        """测试坐标帮助"""
        result = await astro_tools.handle("astro", "coord help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "coord" in result_str.lower() or "坐标" in result_str

    @pytest.mark.asyncio
    async def test_negative_sexagesimal_declination_reaches_coord_handler(
        self,
        mock_context,
        monkeypatch,
    ):
        handler = AsyncMock(return_value="ok")
        monkeypatch.setitem(astro_tools._COMMAND_HANDLERS, "coord", handler)

        result = await astro_tools.handle(
            "astro",
            "coord 12:34:56 -12:34:56",
            {},
            mock_context,
        )

        assert "ok" in str(result)
        handler.assert_awaited_once_with("12:34:56 -12:34:56", mock_context)

    @pytest.mark.parametrize(
        "args",
        ["galactic nan 0", "ecliptic 0 inf", "12 nan", "galactic 1 2 extra"],
    )
    @pytest.mark.asyncio
    async def test_coord_rejects_non_finite_or_extra_values(self, mock_context, args):
        result = await astro_coord.handle_coord(args, mock_context)

        assert "无效" in result or "格式" in result or "请提供" in result


class TestAstroToolsConvert:
    """测试单位转换功能"""

    @pytest.mark.asyncio
    async def test_convert_help(self, mock_context):
        """测试转换帮助"""
        result = await astro_tools.handle("astro", "convert help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "convert" in result_str.lower() or "转换" in result_str

    @pytest.mark.parametrize(
        "args",
        [
            "1 Jy mJy",
            "1 Jy uJy",
            "1 Jy μJy",
            "1 Jy µJy",
            "1 pc ly",
            "1 cm km",
            "1 Hz THz",
            "1 K mK",
            "1 eV J",
            "1 erg J",
            "1 Msun kg",
            "1 km/s m/s",
            "1 erg/s J/s",
        ],
    )
    @pytest.mark.asyncio
    async def test_convert_executes_real_supported_units(self, mock_context, args):
        result = await astro_convert.handle_convert(args, mock_context)

        assert result.startswith("📐 单位转换")
        assert "XQ-PLUGIN-UNEXPECTED" not in result

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e9999"])
    @pytest.mark.asyncio
    async def test_convert_rejects_non_finite_values(self, mock_context, value):
        result = await astro_convert.handle_convert(f"{value} pc ly", mock_context)

        assert result == "数值必须是有限数字"

    @pytest.mark.asyncio
    async def test_convert_rejects_ignored_trailing_arguments(self, mock_context):
        result = await astro_convert.handle_convert("1 pc ly ignored", mock_context)

        assert result.startswith("格式:")


class TestAstroToolsRedshift:
    """测试红移计算功能"""

    @pytest.mark.asyncio
    async def test_redshift_help(self, mock_context):
        """测试红移帮助"""
        result = await astro_tools.handle("astro", "redshift help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "redshift" in result_str.lower() or "红移" in result_str

    @pytest.mark.asyncio
    async def test_nearby_distance_remains_readable_in_mpc(self, mock_context):
        result = await astro_redshift.handle_redshift("0.001", mock_context)

        assert "光度距离:" in result
        assert "Mpc" in result
        assert "Gpc" not in result


class TestAstroToolsFormula:
    """测试公式计算功能"""

    @pytest.mark.asyncio
    async def test_formula_list(self, mock_context):
        """测试列出所有公式"""
        result = await astro_tools.handle("astro", "formula list", {}, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_formula_help(self, mock_context):
        """测试公式帮助"""
        result = await astro_tools.handle("astro", "formula help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "formula" in result_str.lower() or "公式" in result_str

    @pytest.mark.asyncio
    async def test_formula_list_alias_returns_real_catalog(self, mock_context):
        result = await astro_formula.handle_formula("list", mock_context)

        assert "schwarzschild" in result
        assert "未找到公式" not in result

    def test_formula_rejects_non_finite_derived_result(self, mock_context):
        result = astro_formula._handle_calculation("schwarzschild 1e308", mock_context)

        assert result == "质量超出可计算范围"


class TestAstroToolsConst:
    @pytest.mark.asyncio
    async def test_constant_alias_resolves_to_single_canonical_value(self, mock_context):
        canonical = await astro_const.handle_const("c", mock_context)
        alias     = await astro_const.handle_const("speed", mock_context)

        assert alias == canonical


class TestAstroToolsErrorHandling:
    """测试错误处理"""

    @pytest.mark.asyncio
    async def test_invalid_command(self, mock_context):
        """测试无效命令"""
        result = await astro_tools.handle("astro", "invalid_command", {}, mock_context)
        assert result is not None
        # 应该返回错误信息或帮助
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_exception_handling(self, mock_context):
        """测试异常处理"""
        # 尝试使用格式错误的参数
        result = await astro_tools.handle("astro", "convert", {}, mock_context)
        assert result is not None
        # 不应该抛出未捕获的异常

    @pytest.mark.asyncio
    async def test_obj_query_runs_in_to_thread(self, mock_context, monkeypatch):
        calls = {"count": 0}

        async def _fake_to_thread(func, *args, **kwargs):
            calls["count"] += 1
            return None

        monkeypatch.setattr(loaded_modules["obj"].asyncio, "to_thread", _fake_to_thread)

        result = await loaded_modules["obj"].handle_obj("Crab Pulsar", mock_context)

        assert calls["count"] == 1
        assert "未找到天体" in result or "查询失败" not in result

    @pytest.mark.asyncio
    async def test_obj_rejects_oversized_name_before_network(self, mock_context, monkeypatch):
        called = False

        async def _fake_to_thread(*_args, **_kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(astro_obj.asyncio, "to_thread", _fake_to_thread)

        result = await astro_obj.handle_obj(
            "x" * (astro_obj.SIMBAD_MAX_OBJECT_NAME_CHARS + 1), mock_context
        )

        assert "天体名称过长" in result
        assert called is False

    @pytest.mark.asyncio
    async def test_builtin_sun_needs_neither_astropy_nor_network(
        self,
        mock_context,
        monkeypatch,
    ):
        async def forbidden_thread(*_args, **_kwargs):
            raise AssertionError("built-in solar-system data must stay local")

        monkeypatch.setattr(astro_obj.asyncio, "to_thread", forbidden_thread)

        result = await astro_obj.handle_obj("sun", mock_context)

        assert result.startswith("☀️ 太阳")
        assert "1.988e+30 kg" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        [
            "sun",
            "moon",
            "earth",
            "mercury",
            "venus",
            "mars",
            "jupiter",
            "saturn",
            "uranus",
            "neptune",
            "help",
        ],
    )
    async def test_obj_exact_subcommands_reject_extra_args_before_network(
        self, name, mock_context, monkeypatch
    ):
        called = False

        async def _fake_to_thread(*_args, **_kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(astro_obj.asyncio, "to_thread", _fake_to_thread)

        result = await astro_obj.handle_obj(f"{name} extra", mock_context)

        assert "不接受额外参数" in result
        assert f"/astro obj {name}" in result
        assert called is False

    def test_simbad_text_fields_are_whitespace_normalized(self):
        payload = {
            "metadata": [
                {"name": "ra"},
                {"name": "dec"},
                {"name": "otype"},
                {"name": "V"},
                {"name": "sp_type"},
            ],
            "data": [[10.0, 20.0, "  Galaxy\n type ", 1.0, "  G2V\t"]],
        }

        row = astro_obj._validate_simbad_payload(payload)

        assert row is not None
        assert row.otype == "Galaxy type"
        assert row.sp_type == "G2V"

    def test_simbad_v_magnitude_is_optional(self):
        payload = {
            "metadata": [
                {"name": "ra"},
                {"name": "dec"},
                {"name": "otype"},
                {"name": "V"},
                {"name": "sp_type"},
            ],
            "data": [[10.0, 20.0, "Galaxy", None, None]],
        }

        row = astro_obj._validate_simbad_payload(payload)

        assert row is not None
        assert row.v_magnitude is None
        assert "V星等" not in astro_obj._render_simbad_result("M31", row)

    def test_static_solar_system_text_has_no_one_line_function_shells(self):
        assert {"sun", "moon"} <= astro_obj.SOLAR_SYSTEM_INFO.keys()
        assert not hasattr(astro_obj, "_get_sun_info")
        assert not hasattr(astro_obj, "_get_moon_info")

    def test_obj_query_builder_is_pure_local(self, monkeypatch):
        monkeypatch.setattr(
            loaded_modules["obj"],
            "_build_simbad_client",
            lambda: pytest.fail("local ADQL construction must not initialize astroquery"),
            raising=False,
        )

        query = loaded_modules["obj"]._build_simbad_query("M31")

        assert "SELECT TOP 1" in query.upper()
        assert "M31" in query
