"""
jupyter 插件单元测试

由于 jupyter 插件使用相对导入且依赖 jupyter_client（外部依赖），
本测试主要测试模块结构和可导入性。
"""

import asyncio
import base64
import struct
import threading
import zlib
from queue import Empty as QueueEmpty
from types import SimpleNamespace

import pytest

from core.session import Session

# 在其他生命周期测试安装有意设置的 ``plugins.*`` 命名空间墓碑前导入当前插件版本；
# 这些单元测试直接覆盖仓库源码，而不是 PluginManager 动态加载出的版本。
from plugins.jupyter import jupyter_config as _jupyter_config
from plugins.jupyter import jupyter_manager as _jupyter_manager
from plugins.jupyter import jupyter_models as _jupyter_models
from plugins.jupyter import main as _jupyter_main
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.payloads import png_chunk as _png_chunk

ROOT = REPOSITORY_ROOT


def _valid_png(width: int = 1, height: int = 1) -> bytes:
    """构造结构和 CRC 完整的最小 RGBA PNG，避免把残缺文件当作测试夹具。"""

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanline = b"\x00" + b"\x00\x00\x00\x00" * max(1, min(width * height, 1))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanline))
        + _png_chunk(b"IEND", b"")
    )


def _response_text(response: list[dict[str, object]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in response
        if segment.get("type") == "text" and isinstance(segment.get("data"), dict)
    )


# ============================================================
# Test Module Structure
# ============================================================


class TestJupyterRuntimeContract:
    """Validate the installed package generation through normal imports."""

    def test_entrypoints_and_runtime_types_import(self):
        assert _jupyter_main.init() is None
        assert callable(_jupyter_main.handle)
        assert callable(_jupyter_main.handle_session)
        assert _jupyter_manager.JupyterKernelManager is not None
        assert _jupyter_models.ExecutionResult is not None
        assert _jupyter_config.DEFAULT_TIMEOUT > 0
        assert _jupyter_config.MAX_IMAGES > 0


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

    def test_extract_code_and_timeout_preserves_code_after_single_delimiter(self):
        from plugins.jupyter.main import extract_code_and_timeout

        code, timeout = extract_code_and_timeout("  -t 12     print('kept')  ")

        assert timeout == 12.0
        assert code == "    print('kept')  "

    @pytest.mark.parametrize(
        "args",
        (
            "--timeout=999999 print('too large')",
            "--timeout=0 print('zero')",
            "--timeout nope print('invalid')",
            "--timeout",
        ),
    )
    def test_extract_code_and_timeout_rejects_invalid_values(self, args):
        from plugins.jupyter.main import JupyterCommandError, extract_code_and_timeout

        with pytest.raises(JupyterCommandError):
            extract_code_and_timeout(args)

    @pytest.mark.asyncio
    async def test_kernel_manager_instances_are_user_isolated(self, tmp_path):
        """同一用户复用实例，不同用户实例隔离"""
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        data_dir = tmp_path / "jupyter"
        await JupyterKernelManager.shutdown_all_async()
        manager_a1 = JupyterKernelManager.get_instance(data_dir, "user-1")
        manager_a2 = JupyterKernelManager.get_instance(data_dir, "user-1")
        manager_b = JupyterKernelManager.get_instance(data_dir, "user-2")

        assert manager_a1 is manager_a2
        assert manager_a1 is not manager_b
        await JupyterKernelManager.shutdown_all_async()

    @pytest.mark.asyncio
    async def test_kernel_manager_instances_are_workspace_isolated(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        await JupyterKernelManager.shutdown_all_async()
        manager_a = JupyterKernelManager.get_instance(tmp_path / "ws-a", "user-1")
        manager_b = JupyterKernelManager.get_instance(tmp_path / "ws-b", "user-1")

        assert manager_a is not manager_b
        await JupyterKernelManager.shutdown_all_async()

    def test_owner_key_uses_group_context_when_available(self):
        from plugins.jupyter.main import _owner_key

        context = SimpleNamespace(current_user_id=10001, current_group_id=20002)

        assert _owner_key(context) == "user:10001:group:20002"

    @pytest.mark.parametrize(
        ("group_id", "expected"),
        ((None, "user:10001:private"), (20002, "user:10001:group:20002")),
    )
    def test_owner_key_has_no_shared_fallback(self, group_id, expected):
        from plugins.jupyter.main import _owner_key

        context = SimpleNamespace(current_user_id=10001, current_group_id=group_id)
        assert _owner_key(context) == expected
        with pytest.raises(RuntimeError):
            _owner_key(SimpleNamespace(current_user_id=None, current_group_id=group_id))

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
                raise QueueEmpty()

        fake_km = _FakeKernelManager()
        manager._km = fake_km
        manager._kc = _FakeKernelClient()

        result = await manager.execute("print('hello')", timeout=0.01)

        assert result.success is False
        assert "超时" in result.error
        assert fake_km.interrupted is True
        assert not manager.figures_dir.exists()

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
        assert not manager.figures_dir.exists()
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
        assert not manager.figures_dir.exists()
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
        assert not manager.figures_dir.exists()
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
        assert result.images == []
        assert not manager.figures_dir.exists()

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

    @pytest.mark.asyncio
    async def test_oversized_traceback_is_replaced_by_fixed_limit_error(self, tmp_path):
        from plugins.jupyter.jupyter_config import MAX_OUTPUT_BYTES
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        manager._km = SimpleNamespace(
            is_alive=lambda: True,
            interrupt_kernel=lambda: None,
        )
        messages = [
            {
                "msg_type": "error",
                "content": {"traceback": ["secret" * MAX_OUTPUT_BYTES]},
                "parent_header": {"msg_id": "msg-1"},
            },
            {
                "msg_type": "status",
                "content": {"execution_state": "idle"},
                "parent_header": {"msg_id": "msg-1"},
            },
        ]
        manager._kc = SimpleNamespace(
            execute=lambda _code: "msg-1",
            get_iopub_msg=lambda _timeout: messages.pop(0),
        )

        result = await manager.execute("raise RuntimeError", timeout=1)

        assert result.success is False
        assert result.error == f"输出超过 {MAX_OUTPUT_BYTES} 字节安全上限，已中断内核"
        assert "secret" not in result.error

    def test_total_image_budget_is_shared_across_messages(self, tmp_path, monkeypatch):
        from plugins.jupyter.jupyter_config import MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES
        from plugins.jupyter.jupyter_manager import JupyterKernelManager, _OutputBudget
        from plugins.jupyter.jupyter_models import ExecutionResult

        manager = JupyterKernelManager(tmp_path / "jupyter")
        image = b"x" * MAX_IMAGE_BYTES
        monkeypatch.setattr(manager, "_decode_image", lambda _value: image)
        result = ExecutionResult()
        budget = _OutputBudget()
        total = 0

        for _ in range(MAX_TOTAL_IMAGE_BYTES // MAX_IMAGE_BYTES):
            total, exceeded = manager._process_data_message(
                {"image/png": "encoded"},
                result,
                budget,
                total,
                include_text=False,
            )
            assert exceeded is False
        total, exceeded = manager._process_data_message(
            {"image/png": "encoded"},
            result,
            budget,
            total,
            include_text=False,
        )

        assert exceeded is True
        assert total == MAX_TOTAL_IMAGE_BYTES
        assert len(result.images) == 2
        assert "图片输出超过" in result.error

    def test_image_decode_rejects_size_and_pixel_bombs(self, tmp_path):
        from plugins.jupyter.jupyter_config import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        oversized = "A" * ((((MAX_IMAGE_BYTES + 2) // 3) * 4) + 4)
        assert manager._decode_image(oversized) is None

        encoded = base64.b64encode(_valid_png(MAX_IMAGE_PIXELS + 1, 1)).decode("ascii")
        assert manager._decode_image(encoded) is None

    def test_valid_image_decode_is_in_memory_only(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager = JupyterKernelManager(tmp_path / "jupyter")
        png = _valid_png()
        encoded = base64.b64encode(png).decode("ascii")

        decoded = manager._decode_image(encoded)

        assert decoded == png
        assert not manager.figures_dir.exists()

    @pytest.mark.parametrize("mutation", ("crc", "trailing", "missing_iend"))
    def test_image_decode_rejects_structurally_invalid_png(self, tmp_path, mutation):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        png = _valid_png()
        if mutation == "crc":
            damaged = bytearray(png)
            damaged[-1] ^= 1
            png = bytes(damaged)
        elif mutation == "trailing":
            png += b"trailing"
        else:
            png = png[: -len(_png_chunk(b"IEND", b""))]

        manager = JupyterKernelManager(tmp_path / "jupyter")
        assert manager._decode_image(base64.b64encode(png).decode("ascii")) is None
        assert manager._decode_image(None) is None

    def test_manager_cleans_only_legacy_execution_directories(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        figures = tmp_path / "jupyter" / "figures"
        legacy = figures / ("a" * 32)
        preserved = figures / "user-kept"
        flat_legacy = figures / "output_1769229059_0.png"
        unrelated_flat = figures / "output_123_0.png"
        legacy.mkdir(parents=True)
        preserved.mkdir()
        (legacy / "output_0.png").write_bytes(b"old")
        flat_legacy.write_bytes(b"old")
        unrelated_flat.write_bytes(b"keep")
        (preserved / "note.txt").write_text("keep", encoding="utf-8")

        JupyterKernelManager(tmp_path / "jupyter")

        assert not legacy.exists()
        assert not flat_legacy.exists()
        assert unrelated_flat.read_bytes() == b"keep"
        assert (preserved / "note.txt").read_text(encoding="utf-8") == "keep"

    @pytest.mark.asyncio
    async def test_execute_handler_returns_validated_png_as_inline_segment(
        self,
        tmp_path,
        monkeypatch,
    ):
        from plugins.jupyter import main as jupyter_main
        from plugins.jupyter.jupyter_models import ExecutionResult

        png = _valid_png()
        fake_manager = SimpleNamespace(
            execute=lambda *_args, **_kwargs: asyncio.sleep(
                0,
                result=ExecutionResult(images=[png]),
            )
        )
        monkeypatch.setattr(
            jupyter_main.JupyterKernelManager,
            "get_instance",
            lambda *_args, **_kwargs: fake_manager,
        )
        context = SimpleNamespace(
            data_dir=tmp_path / "jupyter",
            current_user_id=1,
            current_group_id=2,
            request_id="request-1",
        )

        response = await jupyter_main._handle_execute("print('image')", context)

        image_segments = [segment for segment in response if segment["type"] == "image"]
        assert image_segments == [
            {
                "type": "image",
                "data": {"file": "base64://" + base64.b64encode(png).decode("ascii")},
            }
        ]
        assert not (context.data_dir / "figures").exists()

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

    @pytest.mark.asyncio
    async def test_help_does_not_require_optional_dependency(self, monkeypatch, tmp_path):
        from plugins.jupyter import main as jupyter_main

        monkeypatch.setattr(
            jupyter_main,
            "_dependencies_available",
            lambda: (_ for _ in ()).throw(AssertionError("dependency probe must not run")),
        )
        context = SimpleNamespace(data_dir=tmp_path)

        main_help = await jupyter_main.handle("jupyter", "help", {}, context)
        kernel_help = await jupyter_main.handle("jupyter_kernel", "help", {}, context)

        assert "Jupyter" in _response_text(main_help)
        assert "内核" in _response_text(kernel_help)

    @pytest.mark.asyncio
    async def test_handle_routes_only_canonical_manifest_commands(self, tmp_path):
        from plugins.jupyter import main as jupyter_main

        response = await jupyter_main.handle("py", "help", {}, SimpleNamespace(data_dir=tmp_path))
        assert _response_text(response) == "未知 Jupyter 命令"

    @pytest.mark.asyncio
    async def test_repl_preserves_indentation_and_multiline_input(self, tmp_path):
        from plugins.jupyter import main as jupyter_main

        session = Session(
            user_id=1,
            group_id=2,
            plugin_name="jupyter",
            data={"code_buffer": [], "execution_count": 0},
        )
        context = SimpleNamespace(end_session=lambda: asyncio.sleep(0), data_dir=tmp_path)

        await jupyter_main.handle_session(
            "for i in range(2):\n    print(i)",
            {},
            context,
            session,
        )

        assert session.get("code_buffer") == ["for i in range(2):", "    print(i)"]

    @pytest.mark.asyncio
    async def test_repl_rejects_foreign_session_before_reading_its_state(self, tmp_path):
        from plugins.jupyter import main as jupyter_main

        class ForeignSession:
            plugin_name = "another_plugin"

            def get(self, *_args):
                raise AssertionError("foreign session state must not be read")

        context = SimpleNamespace(
            get_session=lambda: asyncio.sleep(0, result=ForeignSession()),
            data_dir=tmp_path,
        )

        response = await jupyter_main._start_repl_session(context)
        assert "其他插件" in _response_text(response)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("success", (True, False))
    async def test_repl_execution_updates_state_only_after_success(
        self,
        tmp_path,
        monkeypatch,
        success,
    ):
        from plugins.jupyter import main as jupyter_main
        from plugins.jupyter.jupyter_models import ExecutionResult

        executed = {}

        class Manager:
            async def execute(self, code, *, timeout, audit_id):
                executed.update(code=code, timeout=timeout, audit_id=audit_id)
                return ExecutionResult(success=success, error="code failed" if not success else "")

        monkeypatch.setattr(
            jupyter_main.JupyterKernelManager,
            "get_instance",
            lambda *_args, **_kwargs: Manager(),
        )
        original_lines = ["for i in range(2):", "    print(i)"]
        session = Session(
            user_id=1,
            group_id=2,
            plugin_name="jupyter",
            data={"code_buffer": list(original_lines), "execution_count": 7},
        )
        context = SimpleNamespace(
            data_dir=tmp_path,
            current_user_id=1,
            current_group_id=2,
            request_id="req-repl",
            end_session=lambda: asyncio.sleep(0),
        )

        response = await jupyter_main.handle_session("run", {}, context, session)

        assert executed["code"] == "for i in range(2):\n    print(i)"
        assert session.get("code_buffer") == ([] if success else original_lines)
        assert session.get("execution_count") == (8 if success else 7)
        assert ("执行完成" if success else "缓冲区已保留") in _response_text(response)
