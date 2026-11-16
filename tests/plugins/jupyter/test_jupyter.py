"""jupyter 插件的代码提取、内核管理器执行/取消/输出边界和 REPL 会话测试。"""

import asyncio
import base64
import struct
import sys
import threading
import zlib
from queue import Empty as QueueEmpty
from types import ModuleType, SimpleNamespace

import pytest
import pytest_asyncio

from core.session import Session

# 在其他生命周期测试安装有意设置的 ``plugins.*`` 命名空间墓碑前导入当前插件版本；
# 这些单元测试直接覆盖仓库源码，而不是 PluginManager 动态加载出的版本。
from tests.helpers.jupyter_test_support import KernelBackend, install_kernel_backend
from tests.helpers.payloads import png_chunk as _png_chunk
from tests.helpers.payloads import valid_png as _valid_png


def _response_text(response: list[dict[str, object]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in response
        if segment.get("type") == "text" and isinstance(segment.get("data"), dict)
    )


@pytest_asyncio.fixture
async def kernel_backend(monkeypatch: pytest.MonkeyPatch):
    """依赖可用，内核由替身承担；用例结束时关掉全部内核并清空实例注册表。"""

    from plugins.jupyter import jupyter_manager

    manager_class = jupyter_manager.JupyterKernelManager
    backend       = KernelBackend()
    install_kernel_backend(monkeypatch, backend)
    with manager_class._instances_lock:  # noqa: SLF001 - 隔离其他用例留下的全局注册表。
        manager_class._instances.clear()  # noqa: SLF001
        manager_class._quarantined_instances.clear()  # noqa: SLF001
    yield backend
    await manager_class.shutdown_all_async()


async def _stop_idle_monitor(manager: object) -> None:
    """取消执行时登记的空闲监视器，避免它在用例结束后继续挂着。"""

    task = getattr(manager, "_shutdown_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _grayscale_png(width: int, height: int) -> bytes:
    """构造 1 位灰度 PNG：像素数可以很大，而文件只有几 KiB。"""

    header   = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    scanline = b"\x00" * (1 + (width + 7) // 8)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanline * height))
        + _png_chunk(b"IEND", b"")
    )


def _png_of_exact_size(size: int) -> bytes:
    """在合法 PNG 的 IEND 前塞一个私有辅助块，凑出恰好 ``size`` 字节的合法 PNG。"""

    base    = _valid_png()
    padding = size - len(base) - 12
    return base[:-12] + _png_chunk(b"prVt", b"x" * padding) + base[-12:]


class TestJupyterExecution:
    """代码提取、内核隔离、执行取消与输出边界。"""

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
        manager_b  = JupyterKernelManager.get_instance(data_dir, "user-2")

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

        fake_km     = _FakeKernelManager()
        manager._km = fake_km
        manager._kc = _FakeKernelClient()

        result = await manager.execute("print('hello')", timeout=0.01)

        assert result.success is False
        assert "超时" in result.error
        assert fake_km.interrupted is True

    @pytest.mark.asyncio
    async def test_cancel_during_kernel_start_waits_then_shuts_down(self, tmp_path, kernel_backend):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager       = JupyterKernelManager(tmp_path / "jupyter")
        start_entered = threading.Event()
        release_start = threading.Event()
        # 屏障放在第三方内核的启动调用里：内核已经起来，启动线程还没返回。
        kernel_backend.block_start = (start_entered, release_start)

        execution = asyncio.create_task(manager.execute("1 + 1"))
        assert await asyncio.to_thread(start_entered.wait, 1)
        execution.cancel()
        await asyncio.sleep(0)
        assert not execution.done()

        release_start.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=2)

        (kernel,) = kernel_backend.kernels
        assert kernel.shutdown_calls == 1
        assert kernel.alive is False
        assert manager._km is None and manager._kc is None
        assert not {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name().startswith("jupyter-")
        }

    @pytest.mark.asyncio
    async def test_cancel_during_submit_waits_interrupts_and_survives_repeat_cancel(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager        = JupyterKernelManager(tmp_path / "jupyter")
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
                    "msg_type"     : "status",
                    "content"      : {"execution_state": "idle"},
                    "parent_header": {"msg_id": "msg-cancel"},
                }

        fake_km     = _FakeKernelManager()
        manager._km = fake_km
        manager._kc = _FakeKernelClient()

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
        await _stop_idle_monitor(manager)

    @pytest.mark.asyncio
    async def test_cancel_during_read_drains_reader_before_recovery(self, tmp_path):
        from plugins.jupyter.jupyter_manager import JupyterKernelManager

        manager      = JupyterKernelManager(tmp_path / "jupyter")
        read_entered = threading.Event()
        release_read = threading.Event()
        reader_lock  = threading.Lock()

        class _FakeKernelManager:
            def __init__(self):
                self.interrupt_calls = 0

            def is_alive(self):
                return True

            def interrupt_kernel(self):
                self.interrupt_calls += 1

        class _FakeKernelClient:
            def __init__(self):
                self.read_calls         = 0
                self.active_readers     = 0
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
                            "msg_type"     : "stream",
                            "content"      : {"name": "stdout", "text": "partial"},
                            "parent_header": {"msg_id": "msg-cancel"},
                        }
                    return {
                        "msg_type"     : "status",
                        "content"      : {"execution_state": "idle"},
                        "parent_header": {"msg_id": "msg-cancel"},
                    }
                finally:
                    with reader_lock:
                        self.active_readers -= 1

        fake_km     = _FakeKernelManager()
        fake_kc     = _FakeKernelClient()
        manager._km = fake_km
        manager._kc = fake_kc

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
        await _stop_idle_monitor(manager)

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
                        "msg_type"     : "stream",
                        "content"      : {"name": "stdout", "text": "ignored"},
                        "parent_header": {"msg_id": "other-msg"},
                    },
                    {
                        "msg_type"     : "stream",
                        "content"      : {"name": "stdout", "text": "kept"},
                        "parent_header": {"msg_id": "msg-1"},
                    },
                    {
                        "msg_type"     : "status",
                        "content"      : {"execution_state": "idle"},
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
                        "msg_type"     : "stream",
                        "content"      : {"name": "stdout", "text": "x" * (MAX_OUTPUT_BYTES + 1)},
                        "parent_header": {"msg_id": "msg-1"},
                    },
                    {
                        "msg_type"     : "status",
                        "content"      : {"execution_state": "idle"},
                        "parent_header": {"msg_id": "msg-1"},
                    },
                ]

            def execute(self, _code):
                return "msg-1"

            def get_iopub_msg(self, timeout):
                return self.messages.pop(0)

        fake_km     = _FakeKernelManager()
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

        manager     = JupyterKernelManager(tmp_path / "jupyter")
        manager._km = SimpleNamespace(
            is_alive         = lambda: True,
            interrupt_kernel = lambda: None,
        )
        messages = [
            {
                "msg_type"     : "error",
                "content"      : {"traceback": ["secret" * MAX_OUTPUT_BYTES]},
                "parent_header": {"msg_id": "msg-1"},
            },
            {
                "msg_type"     : "status",
                "content"      : {"execution_state": "idle"},
                "parent_header": {"msg_id": "msg-1"},
            },
        ]
        manager._kc = SimpleNamespace(
            execute       = lambda _code: "msg-1",
            get_iopub_msg = lambda _timeout: messages.pop(0),
        )

        result = await manager.execute("raise RuntimeError", timeout=1)

        assert result.success is False
        assert result.error == f"输出超过 {MAX_OUTPUT_BYTES} 字节安全上限，已中断内核"
        assert "secret" not in result.error

    def test_total_image_budget_is_shared_across_messages(self, tmp_path):
        from plugins.jupyter.jupyter_config import MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES
        from plugins.jupyter.jupyter_manager import JupyterKernelManager, _OutputBudget
        from plugins.jupyter.jupyter_models import ExecutionResult

        manager = JupyterKernelManager(tmp_path / "jupyter")
        image   = _png_of_exact_size(MAX_IMAGE_BYTES)
        encoded = base64.b64encode(image).decode("ascii")
        result  = ExecutionResult()
        budget  = _OutputBudget()
        total   = 0

        for _ in range(MAX_TOTAL_IMAGE_BYTES // MAX_IMAGE_BYTES):
            total, exceeded = manager._process_data_message(
                {"image/png": encoded},
                result,
                budget,
                total,
                include_text=False,
            )
            assert exceeded is False
        total, exceeded = manager._process_data_message(
            {"image/png": encoded},
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

        manager   = JupyterKernelManager(tmp_path / "jupyter")
        oversized = "A" * ((((MAX_IMAGE_BYTES + 2) // 3) * 4) + 4)
        assert manager._decode_image(oversized) is None

        png     = _valid_png(3, 2)
        encoded = base64.b64encode(png).decode("ascii")
        assert manager._decode_image(encoded) == png

        side         = int(MAX_IMAGE_PIXELS**0.5)
        within_limit = _grayscale_png(side, side)
        assert side * side <= MAX_IMAGE_PIXELS
        assert manager._decode_image(base64.b64encode(within_limit).decode("ascii")) == within_limit
        over_limit = _grayscale_png(side + 1, side + 1)
        assert (side + 1) * (side + 1) > MAX_IMAGE_PIXELS
        assert manager._decode_image(base64.b64encode(over_limit).decode("ascii")) is None

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

    @pytest.mark.asyncio
    async def test_execute_handler_returns_validated_png_as_inline_segment(
        self,
        tmp_path,
        kernel_backend,
    ):
        from plugins.jupyter import main as jupyter_main

        png                      = _valid_png()
        kernel_backend.responder = lambda _code: [
            {
                "msg_type": "display_data",
                "content" : {"data": {"image/png": base64.b64encode(png).decode("ascii")}},
            }
        ]
        context = SimpleNamespace(
            data_dir         = tmp_path / "jupyter",
            current_user_id  = 1,
            current_group_id = 2,
            request_id       = "request-1",
        )

        response = await jupyter_main.handle("jupyter", "print('image')", {}, context)

        image_segments = [segment for segment in response if segment["type"] == "image"]
        assert image_segments == [
            {
                "type": "image",
                "data": {"file": "base64://" + base64.b64encode(png).decode("ascii")},
            }
        ]

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
        from plugins.jupyter import jupyter_manager
        from plugins.jupyter import main as jupyter_main

        installed = ModuleType("jupyter_client")
        installed.KernelManager = type("InstalledKernelManager", (), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "jupyter_client", installed)
        monkeypatch.setattr(jupyter_manager, "JUPYTER_AVAILABLE", False)
        monkeypatch.setattr(jupyter_manager, "IMPORT_ERROR", None)
        monkeypatch.setattr(jupyter_manager, "KernelManager", None)
        monkeypatch.setattr(jupyter_main, "_DEPENDENCY_PROBED", False)

        jupyter_main.init()

        assert jupyter_main.jupyter_manager.JUPYTER_AVAILABLE is True
        assert jupyter_main._dependencies_available() is True

    @pytest.mark.asyncio
    async def test_help_does_not_require_optional_dependency(self, monkeypatch, tmp_path):
        from plugins.jupyter import jupyter_manager
        from plugins.jupyter import main as jupyter_main

        monkeypatch.setitem(sys.modules, "jupyter_client", None)
        monkeypatch.setattr(jupyter_manager, "JUPYTER_AVAILABLE", False)
        monkeypatch.setattr(jupyter_manager, "IMPORT_ERROR", None)
        monkeypatch.setattr(jupyter_manager, "KernelManager", None)
        monkeypatch.setattr(jupyter_main, "_DEPENDENCY_PROBED", False)
        context = SimpleNamespace(data_dir=tmp_path)

        main_help   = await jupyter_main.handle("jupyter", "help", {}, context)
        kernel_help = await jupyter_main.handle("jupyter_kernel", "help", {}, context)

        assert "Jupyter" in _response_text(main_help)
        assert "内核" in _response_text(kernel_help)
        # 依赖探测一旦运行，缺失的 jupyter_client 会留下导入错误；帮助命令不应触发探测。
        assert jupyter_manager.IMPORT_ERROR is None

    @pytest.mark.asyncio
    async def test_handle_routes_only_canonical_manifest_commands(self, tmp_path):
        from plugins.jupyter import main as jupyter_main

        response = await jupyter_main.handle("py", "help", {}, SimpleNamespace(data_dir=tmp_path))
        assert _response_text(response) == "未知 Jupyter 命令"

    @pytest.mark.asyncio
    async def test_repl_preserves_indentation_and_multiline_input(self, tmp_path):
        from plugins.jupyter import main as jupyter_main

        session = Session(
            user_id     = 1,
            group_id    = 2,
            plugin_name = "jupyter",
            data        = {"code_buffer": [], "execution_count": 0},
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
        kernel_backend,
        success,
    ):
        from plugins.jupyter import main as jupyter_main

        kernel_backend.responder = lambda _code: (
            [] if success else [{"msg_type": "error", "content": {"traceback": ["code failed"]}}]
        )
        original_lines = ["for i in range(2):", "    print(i)"]
        session        = Session(
            user_id     = 1,
            group_id    = 2,
            plugin_name = "jupyter",
            data        = {"code_buffer": list(original_lines), "execution_count": 7},
        )
        context = SimpleNamespace(
            data_dir         = tmp_path,
            current_user_id  = 1,
            current_group_id = 2,
            request_id       = "req-repl",
            end_session      = lambda: asyncio.sleep(0),
        )

        response = await jupyter_main.handle_session("run", {}, context, session)

        (kernel,) = kernel_backend.kernels
        assert kernel.client_instance.executed[-1] == "for i in range(2):\n    print(i)"
        assert session.get("code_buffer") == ([] if success else original_lines)
        assert session.get("execution_count") == (8 if success else 7)
        assert ("执行完成" if success else "缓冲区已保留") in _response_text(response)
