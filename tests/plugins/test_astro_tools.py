"""
astro_tools 插件单元测试
"""
import pytest
import sys
from pathlib import Path
import importlib.util
import types

ROOT = Path(__file__).resolve().parent.parent.parent


# 动态加载 astro_tools 子模块（加载所有依赖）
modules_to_load = ["time", "coord", "convert", "redshift", "formula", "obj", "const"]
loaded_modules = {}

for mod_name in modules_to_load:
    spec = importlib.util.spec_from_file_location(
        f"astro_tools_{mod_name}",
        ROOT / "plugins" / "astro_tools" / f"{mod_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loaded_modules[mod_name] = module
    # 添加到 sys.modules 以便相对导入
    sys.modules[f"plugins.astro_tools.{mod_name}"] = module
    sys.modules[f"astro_tools_{mod_name}"] = module

# 读取主模块源代码
with open(ROOT / "plugins" / "astro_tools" / "main.py", "r", encoding="utf-8") as f:
    main_source = f.read()

# 替换相对导入
main_source = main_source.replace("from . import time as astro_time", "import astro_tools_time as astro_time")
main_source = main_source.replace("from . import coord", "import astro_tools_coord as astro_coord")
main_source = main_source.replace("from . import convert", "import astro_tools_convert as astro_convert")
main_source = main_source.replace("from . import redshift", "import astro_tools_redshift as astro_redshift")
main_source = main_source.replace("from . import formula", "import astro_tools_formula as astro_formula")
main_source = main_source.replace("from . import obj", "import astro_tools_obj as astro_obj")
main_source = main_source.replace("from . import const", "import astro_tools_const as astro_const")

# 重命名模块以便导入
sys.modules["astro_tools_time"] = loaded_modules["time"]
sys.modules["astro_tools_coord"] = loaded_modules["coord"]
sys.modules["astro_tools_convert"] = loaded_modules["convert"]
sys.modules["astro_tools_redshift"] = loaded_modules["redshift"]
sys.modules["astro_tools_formula"] = loaded_modules["formula"]
sys.modules["astro_tools_obj"] = loaded_modules["obj"]
sys.modules["astro_tools_const"] = loaded_modules["const"]

# 创建并执行主模块
astro_tools = types.ModuleType("astro_tools")
exec(main_source, astro_tools.__dict__)


@pytest.fixture
def mock_context():
    """模拟插件上下文"""
    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "astro_tools"
            self.data_dir = self.plugin_dir / "data"
            self.logger = self._create_logger()

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
        assert any(keyword in result_str for keyword in ["astro", "天文", "工具", "时间", "坐标", "转换"])

    @pytest.mark.asyncio
    async def test_empty_args(self, mock_context):
        """测试空参数返回帮助"""
        result = await astro_tools.handle("astro", "", {}, mock_context)
        assert result is not None
        assert len(result) > 0


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


class TestAstroToolsCoord:
    """测试坐标转换功能"""

    @pytest.mark.asyncio
    async def test_coord_help(self, mock_context):
        """测试坐标帮助"""
        result = await astro_tools.handle("astro", "coord help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "coord" in result_str.lower() or "坐标" in result_str


class TestAstroToolsConvert:
    """测试单位转换功能"""

    @pytest.mark.asyncio
    async def test_convert_help(self, mock_context):
        """测试转换帮助"""
        result = await astro_tools.handle("astro", "convert help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "convert" in result_str.lower() or "转换" in result_str


class TestAstroToolsRedshift:
    """测试红移计算功能"""

    @pytest.mark.asyncio
    async def test_redshift_help(self, mock_context):
        """测试红移帮助"""
        result = await astro_tools.handle("astro", "redshift help", {}, mock_context)
        assert result is not None
        result_str = str(result)
        assert "redshift" in result_str.lower() or "红移" in result_str


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


def test_init():
    """测试插件初始化"""
    # init 函数应该不抛出异常
    astro_tools.init()
    assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
