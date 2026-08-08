"""Jupyter 内核管理器的清理、恢复、输出和防御性分支契约。"""

from __future__ import annotations

import asyncio
import base64
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.jupyter import jupyter_manager as manager_module
from plugins.jupyter.jupyter_config import (
    DEFAULT_TIMEOUT,
    MAX_EXECUTION_TIMEOUT,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_KERNEL_INSTANCES,
    MAX_OUTPUT_BYTES,
)
from plugins.jupyter.jupyter_manager import (
    JupyterKernelManager,
    KernelCleanupReport,
    _ExecutionState,
    _OutputBudget,
    validate_png_bytes,
)
from plugins.jupyter.jupyter_models import ExecutionResult
from tests.helpers.payloads import png_chunk as _png_chunk


@pytest.fixture(autouse=True)
def isolated_manager_globals(monkeypatch: pytest.MonkeyPatch):
    original = (
        manager_module.JUPYTER_AVAILABLE,
        manager_module.IMPORT_ERROR,
        manager_module.KernelManager,
    )
    with JupyterKernelManager._instances_lock:  # noqa: SLF001 - 隔离全局注册表。
        JupyterKernelManager._instances.clear()  # noqa: SLF001
        JupyterKernelManager._quarantined_instances.clear()  # noqa: SLF001
    yield
    manager_module.JUPYTER_AVAILABLE = original[0]
    manager_module.IMPORT_ERROR = original[1]
    manager_module.KernelManager = original[2]
    with JupyterKernelManager._instances_lock:  # noqa: SLF001
        JupyterKernelManager._instances.clear()  # noqa: SLF001
        JupyterKernelManager._quarantined_instances.clear()  # noqa: SLF001


def _alive_kernel(**extra: Any) -> SimpleNamespace:
    values = {
        "is_alive": lambda: True,
        "interrupt_kernel": lambda: None,
        "kernel_name": "python3",
        **extra,
    }
    return SimpleNamespace(**values)


def _valid_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def test_lazy_import_short_circuits_and_can_retry_dependency(monkeypatch) -> None:
    import builtins
    import sys
    from types import ModuleType

    sentinel = object()
    manager_module.JUPYTER_AVAILABLE = True
    manager_module.KernelManager = sentinel
    manager_module.lazy_import_jupyter()
    assert manager_module.KernelManager is sentinel

    manager_module.JUPYTER_AVAILABLE = False
    manager_module.KernelManager = None
    original_import = builtins.__import__

    def fail_jupyter_import(name: str, *args: Any, **kwargs: Any):
        if name == "jupyter_client":
            raise ImportError("dependency unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_jupyter_import)
    manager_module.lazy_import_jupyter()
    assert manager_module.JUPYTER_AVAILABLE is False
    assert manager_module.IMPORT_ERROR == "dependency unavailable"

    fake_module = ModuleType("jupyter_client")
    fake_manager = type("FakeKernelManager", (), {})
    fake_module.KernelManager = fake_manager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jupyter_client", fake_module)
    monkeypatch.setattr(builtins, "__import__", original_import)
    manager_module.lazy_import_jupyter()
    assert manager_module.JUPYTER_AVAILABLE is True
    assert manager_module.KernelManager is fake_manager


def test_output_budget_sanitizes_controls_and_reports_exhaustion() -> None:
    budget = _OutputBudget()
    output, exceeded = budget.append("", "\x1b[31mred\x1b[0m\x00\x7f\t")
    assert output == "red\t"
    assert exceeded is False

    budget.used_bytes = MAX_OUTPUT_BYTES
    assert budget.append(output, "x") == (output, True)
    assert budget.append(output, "") == (output, False)


def test_legacy_cleanup_scan_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JupyterKernelManager(tmp_path)
    removed: list[object] = []

    class FakeDirectory:
        def iterdir(self):
            return iter(range(4_097))

        def rmdir(self) -> None:
            return None

    manager.figures_dir = FakeDirectory()  # type: ignore[assignment]
    monkeypatch.setattr(manager, "_remove_legacy_figure", removed.append)
    manager._cleanup_legacy_figures()
    assert len(removed) == 4_096


def test_legacy_cleanup_preserves_links_and_contains_os_errors(tmp_path: Path) -> None:
    class Link:
        def is_symlink(self) -> bool:
            return True

    class Broken:
        def is_symlink(self) -> bool:
            raise OSError("private path")

    JupyterKernelManager._remove_legacy_figure(Link())  # type: ignore[arg-type]
    JupyterKernelManager._remove_legacy_figure(Broken())  # type: ignore[arg-type]


@pytest.mark.parametrize("owner", (None, "", "has space", "x" * 129, "换行"))
def test_instance_key_rejects_unbounded_or_non_ascii_owner(tmp_path: Path, owner) -> None:
    with pytest.raises(ValueError):
        JupyterKernelManager._instance_key(tmp_path, owner)


def test_instance_registry_has_a_hard_process_bound(tmp_path: Path) -> None:
    for index in range(MAX_KERNEL_INSTANCES):
        JupyterKernelManager.get_instance(tmp_path / str(index), f"user-{index}")

    with pytest.raises(RuntimeError, match="instance limit"):
        JupyterKernelManager.get_instance(tmp_path / "overflow", "overflow")


@pytest.mark.asyncio
async def test_async_shutdown_all_propagates_aggregate_failure(tmp_path: Path) -> None:
    manager = JupyterKernelManager.get_instance(tmp_path, "user-1")

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("private cleanup detail")

    manager.shutdown_kernel = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="1 Jupyter kernel cleanup"):
        await JupyterKernelManager.shutdown_all_async()


@pytest.mark.asyncio
async def test_async_shutdown_cancels_idle_monitor_before_kernel_cleanup(tmp_path: Path) -> None:
    manager = JupyterKernelManager.get_instance(tmp_path, "user-1")
    idle_task = asyncio.create_task(asyncio.Event().wait())
    manager._shutdown_task = idle_task  # noqa: SLF001
    calls: list[bool] = []
    manager.shutdown_kernel = (  # type: ignore[method-assign]
        lambda cancel_idle_task, **_kwargs: calls.append(cancel_idle_task)
    )

    await JupyterKernelManager.shutdown_all_async()

    assert idle_task.cancelled()
    assert calls == [False]


def test_awaitable_cleanup_supports_success_error_and_deadline() -> None:
    async def succeed() -> int:
        return 7

    async def fail() -> None:
        raise RuntimeError("cleanup failed")

    assert JupyterKernelManager._resolve_awaitable(succeed()) == 7
    with pytest.raises(RuntimeError, match="cleanup failed"):
        JupyterKernelManager._resolve_awaitable(fail())

    async def slow() -> None:
        await asyncio.sleep(0.2)

    with pytest.raises(TimeoutError):
        JupyterKernelManager._resolve_awaitable(slow(), timeout=0.01)


def test_cleanup_helpers_handle_missing_and_raising_capabilities() -> None:
    report = KernelCleanupReport("unit")
    assert JupyterKernelManager._kernel_alive(None, report=report, stage="none") is False
    assert JupyterKernelManager._kernel_alive(object(), report=report, stage="missing") is None

    broken = SimpleNamespace(is_alive=lambda: (_ for _ in ()).throw(RuntimeError("alive failed")))
    assert JupyterKernelManager._kernel_alive(broken, report=report, stage="broken") is None
    assert "broken.is_alive:RuntimeError" in report.errors

    JupyterKernelManager._request_kernel_shutdown(object(), report)
    assert report.shutdown_succeeded is False
    assert JupyterKernelManager._request_resource_cleanup(object(), report) is None


def test_wait_for_kernel_exit_can_report_still_alive_without_sleeping() -> None:
    report = KernelCleanupReport("wait")
    assert (
        JupyterKernelManager._wait_for_kernel_exit(
            SimpleNamespace(is_alive=lambda: True),
            report,
            stage="wait",
            timeout=0,
        )
        is True
    )


def test_cleanup_retries_resource_cleanup_after_force_kill() -> None:
    alive = True
    cleanup_calls = 0

    def is_alive() -> bool:
        return alive

    def shutdown_kernel(**_kwargs: Any) -> None:
        raise RuntimeError("shutdown failed")

    def cleanup_resources(**_kwargs: Any) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("cleanup failed")

    def kill(**_kwargs: Any) -> None:
        nonlocal alive
        alive = False

    kernel = SimpleNamespace(
        is_alive=is_alive,
        shutdown_kernel=shutdown_kernel,
        cleanup_resources=cleanup_resources,
        provisioner=SimpleNamespace(kill=kill),
    )
    report = JupyterKernelManager._cleanup_kernel_resources(
        kernel,
        None,
        context="force-kill",
    )
    assert report.orphan_confirmed_absent is True
    assert cleanup_calls == 2
    assert "cleanup_resources_after_kill:RuntimeError" in report.errors


def test_status_reports_running_kernel_metadata(tmp_path: Path) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._km = _alive_kernel(kernel_name="python-test")  # noqa: SLF001
    manager._started_at = manager_module.time.monotonic() - 3  # noqa: SLF001
    manager._execution_count = 4  # noqa: SLF001
    status = manager.get_status()
    assert status["running"] is True
    assert status["kernel_name"] == "python-test"
    assert status["execution_count"] == 4


def test_start_rejects_broken_or_unavailable_manager(tmp_path: Path, monkeypatch) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._broken = True  # noqa: SLF001
    with pytest.raises(RuntimeError, match="已隔离"):
        manager.start_kernel()

    manager._broken = False  # noqa: SLF001
    monkeypatch.setattr(manager_module, "lazy_import_jupyter", lambda: None)
    manager_module.JUPYTER_AVAILABLE = False
    manager_module.KernelManager = None
    with pytest.raises(ImportError, match="依赖未加载"):
        manager.start_kernel()


def test_start_returns_immediately_for_live_kernel(tmp_path: Path, monkeypatch) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._km = _alive_kernel()  # noqa: SLF001
    monkeypatch.setattr(manager_module, "lazy_import_jupyter", lambda: None)
    manager_module.JUPYTER_AVAILABLE = True
    manager_module.KernelManager = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("factory must not run")
    )
    assert manager.start_kernel() is True


def test_stale_resources_are_cleaned_before_new_kernel(tmp_path: Path, monkeypatch) -> None:
    manager = JupyterKernelManager(tmp_path)
    stale = SimpleNamespace(
        is_alive=lambda: False,
        shutdown_kernel=lambda **_kwargs: None,
        cleanup_resources=lambda **_kwargs: None,
    )
    manager._km = stale  # noqa: SLF001
    manager._kc = SimpleNamespace(stop_channels=lambda: None)  # noqa: SLF001

    class Client:
        def start_channels(self) -> None:
            return None

        def wait_for_ready(self, timeout: float) -> None:
            assert timeout == 30

        def execute(self, _code: str) -> str:
            return "init"

        def get_iopub_msg(self, timeout: float) -> dict[str, object]:
            return {
                "msg_type": "status",
                "content": {"execution_state": "idle"},
                "parent_header": {"msg_id": "init"},
            }

    kernel = SimpleNamespace(
        kernel_name="python3",
        start_kernel=lambda: None,
        client=lambda: Client(),
        is_alive=lambda: True,
    )
    monkeypatch.setattr(manager_module, "lazy_import_jupyter", lambda: None)
    manager_module.JUPYTER_AVAILABLE = True
    manager_module.KernelManager = lambda **_kwargs: kernel

    assert manager.start_kernel() is True
    assert manager._km is kernel  # noqa: SLF001


def test_unconfirmed_stale_resources_quarantine_manager(tmp_path: Path, monkeypatch) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._km = object()  # noqa: SLF001
    report = KernelCleanupReport("stale", orphan_confirmed_absent=False)
    monkeypatch.setattr(manager, "_cleanup_kernel_resources", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(manager_module, "lazy_import_jupyter", lambda: None)
    manager_module.JUPYTER_AVAILABLE = True
    manager_module.KernelManager = lambda **_kwargs: object()

    with pytest.raises(RuntimeError, match="旧 Jupyter kernel"):
        manager.start_kernel()
    assert manager.broken is True


def test_matplotlib_initialization_requires_matching_idle(monkeypatch) -> None:
    client = SimpleNamespace(execute=lambda _code: "init")
    times = iter((0.0, 11.0))
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError, match="did not become idle"):
        JupyterKernelManager._initialize_matplotlib(client)


@pytest.mark.asyncio
async def test_idle_loop_closes_expired_kernel(tmp_path: Path, monkeypatch) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._km = _alive_kernel()  # noqa: SLF001
    manager._last_activity = 0  # noqa: SLF001
    calls: list[bool] = []

    async def no_wait(_seconds: float) -> None:
        return None

    def shutdown(cancel_idle_task: bool) -> None:
        calls.append(cancel_idle_task)
        manager._km = None  # noqa: SLF001

    monkeypatch.setattr(manager_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(manager, "shutdown_kernel", shutdown)
    await manager._check_idleness_loop()
    assert calls == [False]


@pytest.mark.asyncio
async def test_idle_loop_does_not_shutdown_an_active_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._km = _alive_kernel()  # noqa: SLF001
    manager._last_activity = 0  # noqa: SLF001
    calls: list[bool] = []

    monkeypatch.setattr(manager_module.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(
        manager,
        "shutdown_kernel",
        lambda cancel_idle_task: calls.append(cancel_idle_task),
    )

    await manager._execute_lock.acquire()  # noqa: SLF001 - 模拟仍在执行的长任务。
    shutdown_check = asyncio.create_task(manager._shutdown_if_expired())  # noqa: SLF001
    try:
        await asyncio.sleep(0)
        assert shutdown_check.done() is False
        manager._last_activity = 1_000.0  # noqa: SLF001 - 执行结束时刷新活动时间。
    finally:
        manager._execute_lock.release()  # noqa: SLF001

    assert await shutdown_check is False
    assert calls == []


def test_interrupt_and_client_guards(tmp_path: Path) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager.interrupt_kernel()
    with pytest.raises(RuntimeError, match="client is unavailable"):
        manager._require_client()
    assert manager._is_matching_idle(None, None) is False
    assert manager._is_matching_idle({"msg_type": "stream"}, "id") is False


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (None, DEFAULT_TIMEOUT),
        ("bad", DEFAULT_TIMEOUT),
        (float("inf"), DEFAULT_TIMEOUT),
        (0, DEFAULT_TIMEOUT),
        (9999, MAX_EXECUTION_TIMEOUT),
    ),
)
def test_manager_timeout_normalization(value, expected) -> None:
    assert JupyterKernelManager._normalize_timeout(value) == expected


@pytest.mark.asyncio
async def test_tracked_thread_and_pending_helpers_clear_failures(tmp_path: Path) -> None:
    manager = JupyterKernelManager(tmp_path)
    state = _ExecutionState(kernel_was_running=True)

    def fail() -> None:
        raise RuntimeError("thread failed")

    with pytest.raises(RuntimeError, match="thread failed"):
        await manager._run_tracked_thread(state, "unit", fail)
    assert state.pending_task is None and state.pending_kind is None
    assert await manager._await_cancel_pending(None, "audit") == (None, False)

    task = asyncio.create_task(asyncio.sleep(0, result=7))
    assert await manager._await_cancel_pending(task, "audit") == (7, False)
    failed_task = asyncio.create_task(asyncio.sleep(0))
    await failed_task
    failed_task = asyncio.create_task(asyncio.to_thread(fail))
    value, failed = await manager._await_cancel_pending(failed_task, "audit")
    assert value is None and failed is True


@pytest.mark.asyncio
async def test_cancel_cleanup_handles_finished_idle_and_failed_recovery(tmp_path: Path) -> None:
    manager = JupyterKernelManager(tmp_path)
    idle_message = {
        "msg_type": "status",
        "content": {"execution_state": "idle"},
        "parent_header": {"msg_id": "msg"},
    }
    idle_task = asyncio.create_task(asyncio.sleep(0, result=idle_message))
    await manager._cancel_execution_cleanup(
        state=_ExecutionState(
            kernel_was_running=True,
            pending_task=idle_task,
            pending_kind="read",
            msg_id="msg",
        ),
        audit_id="audit",
    )

    async def fail_recovery() -> str:
        raise RuntimeError("recovery failed")

    recovery = asyncio.create_task(fail_recovery())
    await manager._cancel_execution_cleanup(
        state=_ExecutionState(kernel_was_running=True, recovery_task=recovery),
        audit_id="audit",
    )


def test_message_helpers_cover_malformed_stderr_and_multiple_results(tmp_path: Path) -> None:
    manager = JupyterKernelManager(tmp_path)
    result = ExecutionResult(images=[b"x"] * MAX_IMAGES)
    budget = _OutputBudget()
    assert manager._append_image(result, "ignored", 0) == (0, False)
    assert manager._process_data_message(None, result, budget, 0, include_text=True) == (0, False)
    assert manager._matching_message_content(None, "msg") is None
    assert manager._matching_message_content({}, "msg") is None
    assert (
        manager._matching_message_content(
            {"parent_header": {"msg_id": "msg"}, "content": None},
            "msg",
        )
        is None
    )

    result = ExecutionResult()
    action, _ = manager._process_iopub_message(
        {
            "msg_type": "stream",
            "parent_header": {"msg_id": "msg"},
            "content": {"name": "stderr", "text": "\x1b[31mwarn\x1b[0m"},
        },
        "msg",
        result,
        budget,
        0,
    )
    assert action == "continue"
    assert result.stderr == "warn"

    for text in ("one", "two"):
        manager._process_iopub_message(
            {
                "msg_type": "execute_result",
                "parent_header": {"msg_id": "msg"},
                "content": {"data": {"text/plain": text}},
            },
            "msg",
            result,
            budget,
            0,
        )
    assert result.result == "one\ntwo"


@pytest.mark.asyncio
async def test_recovery_requires_message_id_and_clears_failed_task(
    tmp_path: Path, monkeypatch
) -> None:
    manager = JupyterKernelManager(tmp_path)
    with pytest.raises(RuntimeError, match="message id"):
        await manager._run_recovery(_ExecutionState(kernel_was_running=True), "audit")

    async def fail(_msg_id: str, _audit_id: str) -> str:
        raise RuntimeError("recovery failed")

    monkeypatch.setattr(manager, "_interrupt_and_recover", fail)
    state = _ExecutionState(kernel_was_running=True, msg_id="msg")
    with pytest.raises(RuntimeError, match="recovery failed"):
        await manager._run_recovery(state, "audit")
    assert state.recovery_task is None


@pytest.mark.asyncio
async def test_execute_contains_invalid_message_id_and_submit_failure(tmp_path: Path) -> None:
    def running_manager(path: Path) -> JupyterKernelManager:
        manager = JupyterKernelManager(path)
        alive = True

        def shutdown_kernel(**_kwargs: Any) -> None:
            nonlocal alive
            alive = False

        manager._km = SimpleNamespace(  # noqa: SLF001
            is_alive=lambda: alive,
            interrupt_kernel=lambda: None,
            shutdown_kernel=shutdown_kernel,
            cleanup_resources=lambda **_kwargs: None,
        )
        manager.ensure_idle_monitor = lambda: None  # type: ignore[method-assign]
        return manager

    manager = running_manager(tmp_path / "invalid-id")
    manager._kc = SimpleNamespace(execute=lambda _code: 123)  # noqa: SLF001
    result = await manager.execute("print(1)")
    assert result.success is False
    assert result.error == "执行失败，请稍后重试"
    assert manager.is_running is False

    manager = running_manager(tmp_path / "submit-error")
    manager._kc = SimpleNamespace(  # noqa: SLF001
        execute=lambda _code: (_ for _ in ()).throw(RuntimeError("private submit detail"))
    )
    result = await manager.execute("print(2)")
    assert result.success is False
    assert "private submit detail" not in result.error
    assert manager.is_running is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("restarted", "shutdown", "quarantined"))
async def test_interrupt_recovery_fallback_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._kc = SimpleNamespace(  # noqa: SLF001
        get_iopub_msg=lambda _timeout: (_ for _ in ()).throw(RuntimeError("read failed"))
    )

    def restart() -> None:
        if outcome != "restarted":
            raise RuntimeError("restart failed")

    def shutdown() -> None:
        if outcome == "quarantined":
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(manager, "restart_kernel", restart)
    monkeypatch.setattr(manager, "shutdown_kernel", shutdown)
    assert await manager._recover_after_interrupt("msg", audit_id="audit") == outcome


@pytest.mark.asyncio
async def test_failed_interrupt_still_runs_recovery(tmp_path: Path, monkeypatch) -> None:
    manager = JupyterKernelManager(tmp_path)
    manager._km = SimpleNamespace(  # noqa: SLF001
        is_alive=lambda: True,
        interrupt_kernel=lambda: (_ for _ in ()).throw(RuntimeError("interrupt failed")),
    )
    manager._kc = SimpleNamespace(  # noqa: SLF001
        get_iopub_msg=lambda _timeout: (_ for _ in ()).throw(RuntimeError("read failed"))
    )
    monkeypatch.setattr(manager, "restart_kernel", lambda: None)
    assert await manager._interrupt_and_recover("msg", "audit") == "restarted"


def test_shared_png_validator_rejects_malformed_boundaries() -> None:
    png = _valid_png()
    assert validate_png_bytes(png) is True
    assert validate_png_bytes(b"tiny") is False
    assert validate_png_bytes(None) is False

    duplicate_header = png[:-12] + _png_chunk(b"IHDR", png[16:29]) + _png_chunk(b"IEND", b"")
    assert validate_png_bytes(duplicate_header) is False

    invalid_type = bytearray(_png_chunk(b"IDAT", b"x"))
    invalid_type[4:8] = b"ID1T"
    assert validate_png_bytes(bytes(invalid_type)) is False
    assert JupyterKernelManager._decode_image("not base64!") is None
    assert JupyterKernelManager._decode_image("A" * (((MAX_IMAGE_BYTES + 2) // 3) * 4 + 1)) is None

    encoded = base64.b64encode(png).decode("ascii")
    assert JupyterKernelManager._decode_image(encoded) == png
