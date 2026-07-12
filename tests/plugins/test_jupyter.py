"""
jupyter 插件单元测试

由于 jupyter 插件使用相对导入且依赖 jupyter_client（外部依赖），
本测试主要测试模块结构和可导入性。
"""
import asyncio
import base64
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

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
        JupyterKernelManager.shutdown_all()
        manager_a1 = JupyterKernelManager.get_instance(data_dir, "user-1")
        manager_a2 = JupyterKernelManager.get_instance(data_dir, "user-1")
        manager_b = JupyterKernelManager.get_instance(data_dir, "user-2")

        assert manager_a1 is manager_a2
        assert manager_a1 is not manager_b
        JupyterKernelManager.shutdown_all()

    def test_kernel_manager_instances_are_workspace_isolated(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        JupyterKernelManager.shutdown_all()
        manager_a = JupyterKernelManager.get_instance(tmp_path / "ws-a", "user-1")
        manager_b = JupyterKernelManager.get_instance(tmp_path / "ws-b", "user-1")

        assert manager_a is not manager_b
        JupyterKernelManager.shutdown_all()

    def test_owner_key_uses_group_context_when_available(self):
        from plugins.jupyter.main import _owner_key

        context = SimpleNamespace(current_user_id=10001, current_group_id=20002)

        assert _owner_key(context) == "10001:20002"

    @pytest.mark.asyncio
    async def test_execute_timeout_interrupts_running_kernel(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")

        class _FakeKernelManager:
            def __init__(self):
                self.interrupted = False

            def is_alive(self):
                return True

            def interrupt_kernel(self):
                self.interrupted = True

        class _FakeKernelClient:
            def execute(self, _code):
                return "msg-1"

            def get_iopub_msg(self, timeout):
                raise TimeoutError()

        fake_km = _FakeKernelManager()
        manager._km = fake_km
        manager._kc = _FakeKernelClient()

        result = await manager.execute("print('hello')", timeout=0.01)

        assert result.success is False
        assert "超时" in result.error
        assert fake_km.interrupted is True

    @pytest.mark.asyncio
    async def test_cancel_during_kernel_start_waits_then_shuts_down(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        start_entered = threading.Event()
        release_start = threading.Event()
        shutdown_finished = threading.Event()

        class _FakeKernelManager:
            def is_alive(self):
                return True

        def blocking_start():
            start_entered.set()
            assert release_start.wait(timeout=2)
            manager._km = _FakeKernelManager()
            manager._kc = SimpleNamespace()

        def shutdown():
            manager._km = None
            manager._kc = None
            shutdown_finished.set()

        manager.start_kernel = blocking_start
        manager.shutdown_kernel = shutdown
        manager.ensure_idle_monitor = lambda: None

        execution = asyncio.create_task(manager.execute("1 + 1"))
        assert await asyncio.to_thread(start_entered.wait, 1)
        execution.cancel()
        await asyncio.sleep(0)
        assert not execution.done()

        release_start.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=2)

        assert shutdown_finished.is_set()
        assert manager._km is None and manager._kc is None
        assert not {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name().startswith("jupyter-")
        }

    @pytest.mark.asyncio
    async def test_cancel_during_submit_waits_interrupts_and_survives_repeat_cancel(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        submit_entered = threading.Event()
        release_submit = threading.Event()

        class _FakeKernelManager:
            def __init__(self):
                self.interrupt_calls = 0

            def is_alive(self):
                return True

            def interrupt_kernel(self):
                self.interrupt_calls += 1

        class _FakeKernelClient:
            def execute(self, _code):
                submit_entered.set()
                assert release_submit.wait(timeout=2)
                return "msg-cancel"

            def get_iopub_msg(self, _timeout):
                return {
                    "msg_type": "status",
                    "content": {"execution_state": "idle"},
                    "parent_header": {"msg_id": "msg-cancel"},
                }

        fake_km = _FakeKernelManager()
        manager._km = fake_km
        manager._kc = _FakeKernelClient()
        manager.ensure_idle_monitor = lambda: None

        execution = asyncio.create_task(manager.execute("1 + 1"))
        assert await asyncio.to_thread(submit_entered.wait, 1)
        execution.cancel()
        await asyncio.sleep(0)
        execution.cancel()
        await asyncio.sleep(0)
        assert not execution.done()

        release_submit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=2)

        assert fake_km.interrupt_calls == 1
        assert not {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name().startswith("jupyter-")
        }

    @pytest.mark.asyncio
    async def test_cancel_during_read_drains_reader_before_recovery(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        read_entered = threading.Event()
        release_read = threading.Event()
        reader_lock = threading.Lock()

        class _FakeKernelManager:
            def __init__(self):
                self.interrupt_calls = 0

            def is_alive(self):
                return True

            def interrupt_kernel(self):
                self.interrupt_calls += 1

        class _FakeKernelClient:
            def __init__(self):
                self.read_calls = 0
                self.active_readers = 0
                self.max_active_readers = 0

            def execute(self, _code):
                return "msg-cancel"

            def get_iopub_msg(self, _timeout):
                with reader_lock:
                    self.read_calls += 1
                    call = self.read_calls
                    self.active_readers += 1
                    self.max_active_readers = max(self.max_active_readers, self.active_readers)
                try:
                    if call == 1:
                        read_entered.set()
                        assert release_read.wait(timeout=2)
                        return {
                            "msg_type": "stream",
                            "content": {"name": "stdout", "text": "partial"},
                            "parent_header": {"msg_id": "msg-cancel"},
                        }
                    return {
                        "msg_type": "status",
                        "content": {"execution_state": "idle"},
                        "parent_header": {"msg_id": "msg-cancel"},
                    }
                finally:
                    with reader_lock:
                        self.active_readers -= 1

        fake_km = _FakeKernelManager()
        fake_kc = _FakeKernelClient()
        manager._km = fake_km
        manager._kc = fake_kc
        manager.ensure_idle_monitor = lambda: None

        execution = asyncio.create_task(manager.execute("print('partial')"))
        assert await asyncio.to_thread(read_entered.wait, 1)
        execution.cancel()
        await asyncio.sleep(0)
        assert not execution.done()

        release_read.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=2)

        assert fake_km.interrupt_calls == 1
        assert fake_kc.read_calls == 2
        assert fake_kc.max_active_readers == 1
        assert not {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name().startswith("jupyter-")
        }

    @pytest.mark.asyncio
    async def test_execute_filters_iopub_messages_by_parent_msg_id(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")

        class _FakeKernelManager:
            def is_alive(self):
                return True

        class _FakeKernelClient:
            def __init__(self):
                self.messages = [
                    {
                        "msg_type": "stream",
                        "content": {"name": "stdout", "text": "ignored"},
                        "parent_header": {"msg_id": "other-msg"},
                    },
                    {
                        "msg_type": "stream",
                        "content": {"name": "stdout", "text": "kept"},
                        "parent_header": {"msg_id": "msg-1"},
                    },
                    {
                        "msg_type": "status",
                        "content": {"execution_state": "idle"},
                        "parent_header": {"msg_id": "msg-1"},
                    },
                ]

            def execute(self, _code):
                return "msg-1"

            def get_iopub_msg(self, timeout):
                return self.messages.pop(0)

        manager._km = _FakeKernelManager()
        manager._kc = _FakeKernelClient()

        result = await manager.execute("print('hello')", timeout=1)

        assert result.success is True
        assert result.stdout == "kept"

    @pytest.mark.asyncio
    async def test_execute_interrupts_when_text_output_exceeds_memory_limit(self, tmp_path):
        from plugins.jupyter.jupyter_config import MAX_OUTPUT_BYTES
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")

        class _FakeKernelManager:
            def __init__(self):
                self.interrupted = False

            def is_alive(self):
                return True

            def interrupt_kernel(self):
                self.interrupted = True

        class _FakeKernelClient:
            def __init__(self):
                self.messages = [
                    {
                        "msg_type": "stream",
                        "content": {"name": "stdout", "text": "x" * (MAX_OUTPUT_BYTES + 1)},
                        "parent_header": {"msg_id": "msg-1"},
                    },
                    {
                        "msg_type": "status",
                        "content": {"execution_state": "idle"},
                        "parent_header": {"msg_id": "msg-1"},
                    },
                ]

            def execute(self, _code):
                return "msg-1"

            def get_iopub_msg(self, timeout):
                return self.messages.pop(0)

        fake_km = _FakeKernelManager()
        manager._km = fake_km
        manager._kc = _FakeKernelClient()

        result = await manager.execute("print('large')", timeout=1)

        assert result.success is False
        assert len(result.stdout.encode("utf-8")) <= MAX_OUTPUT_BYTES
        assert "安全上限" in result.error
        assert fake_km.interrupted is True

    def test_image_decode_rejects_size_and_pixel_bombs(self, tmp_path):
        from plugins.jupyter.jupyter_config import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        oversized = "A" * ((((MAX_IMAGE_BYTES + 2) // 3) * 4) + 4)
        assert manager._save_image(oversized, 0, manager.figures_dir / "oversized") is None

        width = MAX_IMAGE_PIXELS + 1
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, 1)
        encoded = base64.b64encode(png_header).decode("ascii")
        assert manager._save_image(encoded, 0, manager.figures_dir / "pixel-bomb") is None

    def test_each_execution_uses_a_distinct_artifact_directory(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1, 1)
        encoded = base64.b64encode(png_header).decode("ascii")

        first = manager._save_image(encoded, 0, manager.figures_dir / "execution-a")
        second = manager._save_image(encoded, 0, manager.figures_dir / "execution-b")

        assert first is not None and second is not None
        assert first != second
        assert first.parent.name == "execution-a"
        assert second.parent.name == "execution-b"

    @pytest.mark.asyncio
    async def test_manual_kernel_start_can_register_idle_monitor(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        manager._km = SimpleNamespace(is_alive=lambda: True)
        manager.ensure_idle_monitor()

        assert manager._shutdown_task is not None
        manager._shutdown_task.cancel()
        await asyncio.gather(manager._shutdown_task, return_exceptions=True)

    def test_main_reads_lazy_dependency_state_from_module(self, monkeypatch):
        from plugins.jupyter import main as jupyter_main

        def make_available():
            jupyter_main.jupyter_manager.JUPYTER_AVAILABLE = True
            jupyter_main.jupyter_manager.IMPORT_ERROR = None

        monkeypatch.setattr(jupyter_main.jupyter_manager, "JUPYTER_AVAILABLE", False)
        monkeypatch.setattr(jupyter_main.jupyter_manager, "lazy_import_jupyter", make_available)

        jupyter_main.init()

        assert jupyter_main.jupyter_manager.JUPYTER_AVAILABLE is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
