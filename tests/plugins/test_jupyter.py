"""
jupyter 插件单元测试

由于 jupyter 插件使用相对导入且依赖 jupyter_client（外部依赖），
本测试主要测试模块结构和可导入性。
"""
import pytest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================
# Test Module Structure
# ============================================================

class TestJupyterModuleStructure:
    """测试 Jupyter 模块结构"""

    def test_jupyter_main_file_exists(self):
        """测试 jupyter main.py 文件存在"""
        main_file = ROOT / "plugins" / "jupyter" / "main.py"
        assert main_file.exists()

    def test_jupyter_manager_file_exists(self):
        """测试 jupyter_manager.py 文件存在"""
        manager_file = ROOT / "plugins" / "jupyter" / "jupyter_manager.py"
        assert manager_file.exists()

    def test_jupyter_models_file_exists(self):
        """测试 jupyter_models.py 文件存在"""
        models_file = ROOT / "plugins" / "jupyter" / "jupyter_models.py"
        assert models_file.exists()

    def test_jupyter_config_file_exists(self):
        """测试 jupyter_config.py 文件存在"""
        config_file = ROOT / "plugins" / "jupyter" / "jupyter_config.py"
        assert config_file.exists()

    def test_jupyter_init_file_exists(self):
        """测试 __init__.py 文件存在"""
        init_file = ROOT / "plugins" / "jupyter" / "__init__.py"
        assert init_file.exists()

    def test_jupyter_main_content(self):
        """测试 main.py 包含必要的函数"""
        content = (ROOT / "plugins" / "jupyter" / "main.py").read_text(encoding="utf-8")
        assert "def handle(" in content
        assert "def handle_session(" in content
        assert "def _handle_execute(" in content
        assert "def _handle_kernel(" in content
        assert "async def _start_repl_session(" in content

    def test_jupyter_manager_content(self):
        """测试 jupyter_manager.py 包含必要的类"""
        content = (ROOT / "plugins" / "jupyter" / "jupyter_manager.py").read_text(encoding="utf-8")
        assert "class JupyterKernelManager" in content
        assert "def execute(" in content
        assert "def start_kernel(" in content
        assert "def shutdown_kernel(" in content

    def test_jupyter_models_content(self):
        """测试 jupyter_models.py 包含必要的数据类"""
        content = (ROOT / "plugins" / "jupyter" / "jupyter_models.py").read_text(encoding="utf-8")
        assert "ExecutionResult" in content

    def test_jupyter_config_constants(self):
        """测试 jupyter_config.py 包含必要的常量"""
        content = (ROOT / "plugins" / "jupyter" / "jupyter_config.py").read_text(encoding="utf-8")
        assert "DEFAULT_TIMEOUT" in content
        assert "MAX_IMAGES" in content


# ============================================================
# Test Plugin JSON
# ============================================================

class TestJupyterPluginJson:
    """测试 Jupyter plugin.json 配置"""

    def test_plugin_json_exists(self):
        """测试 plugin.json 存在"""
        import json
        plugin_file = ROOT / "plugins" / "jupyter" / "plugin.json"
        assert plugin_file.exists()

        content = json.loads(plugin_file.read_text(encoding="utf-8"))
        assert "name" in content
        assert "version" in content


# ============================================================
# Test Module Import (with mocking)
# ============================================================

class TestJupyterModuleImport:
    """测试 Jupyter 模块导入（带模拟）"""

    def test_can_import_with_mocking(self):
        """测试通过模拟可以导入模块"""
        import importlib.util
        from unittest.mock import Mock

        # 模拟 jupyter_client 模块
        sys.modules["jupyter_client"] = Mock()
        sys.modules["jupyter_client.KernelManager"] = Mock()

        # 加载 config
        spec_config = importlib.util.spec_from_file_location(
            "jupyter_config",
            ROOT / "plugins" / "jupyter" / "jupyter_config.py"
        )
        jupyter_config = importlib.util.module_from_spec(spec_config)
        sys.modules["jupyter_config"] = jupyter_config
        spec_config.loader.exec_module(jupyter_config)

        # 验证常量
        assert hasattr(jupyter_config, "DEFAULT_TIMEOUT")
        assert hasattr(jupyter_config, "MAX_IMAGES")

        # 清理
        del sys.modules["jupyter_config"]
        del sys.modules["jupyter_client"]
        del sys.modules["jupyter_client.KernelManager"]


# ============================================================
# Test Documentation
# ============================================================

class TestJupyterDocumentation:
    """测试 Jupyter 插件文档"""

    def test_readme_exists(self):
        """测试 README.md 存在"""
        readme = ROOT / "plugins" / "jupyter" / "README.md"
        assert readme.exists()

    def test_readme_content(self):
        """测试 README.md 包含必要内容"""
        readme = ROOT / "plugins" / "jupyter" / "README.md"
        content = readme.read_text(encoding="utf-8")
        assert "Jupyter" in content or "jupyter" in content
        assert len(content) > 100  # 至少有一些内容


# ============================================================
# Regression tests for CodeReview issues
# ============================================================

class TestJupyterCodeReviewFixes:
    """测试 CodeReview 提到的 Jupyter 问题修复"""

    def test_extract_code_and_timeout_does_not_damage_inline_t_literal(self):
        """`-t` 只应作为前置参数处理，不应误伤代码中的字符串"""
        from plugins.jupyter.main import extract_code_and_timeout

        code, timeout = extract_code_and_timeout('-t 12 print("-t 3 should stay")')

        assert timeout == 12.0
        assert code == 'print("-t 3 should stay")'

    def test_extract_code_and_timeout_supports_long_option(self):
        """支持 `--timeout` 前置参数"""
        from plugins.jupyter.main import extract_code_and_timeout

        code, timeout = extract_code_and_timeout("--timeout 7 print('ok')")

        assert timeout == 7.0
        assert code == "print('ok')"

    def test_kernel_manager_instances_are_user_isolated(self, tmp_path):
        """同一用户复用实例，不同用户实例隔离"""
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        data_dir = tmp_path / "jupyter"
        manager_a1 = JupyterKernelManager.get_instance(data_dir, "user-1")
        manager_a2 = JupyterKernelManager.get_instance(data_dir, "user-1")
        manager_b = JupyterKernelManager.get_instance(data_dir, "user-2")

        assert manager_a1 is manager_a2
        assert manager_a1 is not manager_b


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
