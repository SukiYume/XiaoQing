from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.jupyter import jupyter_manager as manager_module
from plugins.jupyter.jupyter_manager import JupyterKernelManager


class FakeClient:
    def __init__(self, *, fail_start: bool = False, fail_ready: bool = False) -> None:
        self.fail_start = fail_start
        self.fail_ready = fail_ready
        self.started = False
        self.stop_calls = 0

    def start_channels(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("start_channels failed")

    def wait_for_ready(self, timeout: float) -> None:
        assert timeout == 30
        if self.fail_ready:
            raise TimeoutError("kernel not ready")

    def stop_channels(self) -> None:
        self.stop_calls += 1

    def execute(self, _code: str) -> str:
        return "init-message"

    def get_iopub_msg(self, timeout: float) -> dict[str, Any]:
        del timeout
        return {
            "msg_type": "status",
            "content": {"execution_state": "idle"},
            "parent_header": {"msg_id": "init-message"},
        }


class FakeKernel:
    kernel_name = "python3"

    def __init__(
        self,
        client: FakeClient | None = None,
        *,
        fail_start: bool = False,
        fail_client: bool = False,
        fail_shutdown: bool = False,
        block_start: tuple[threading.Event, threading.Event] | None = None,
    ) -> None:
        self.client_instance = client or FakeClient()
        self.fail_start = fail_start
        self.fail_client = fail_client
        self.fail_shutdown = fail_shutdown
        self.block_start = block_start
        self.alive = False
        self.start_calls = 0
        self.shutdown_calls = 0
        self.cleanup_calls = 0

    def start_kernel(self) -> None:
        self.start_calls += 1
        self.alive = True
        if self.block_start is not None:
            entered, release = self.block_start
            entered.set()
            assert release.wait(timeout=2)
        if self.fail_start:
            raise RuntimeError("start_kernel failed")

    def client(self) -> FakeClient:
        if self.fail_client:
            raise RuntimeError("client failed")
        return self.client_instance

    def is_alive(self) -> bool:
        return self.alive

    def shutdown_kernel(self, now: bool = False) -> None:
        assert now is True
        self.shutdown_calls += 1
        if self.fail_shutdown:
            raise RuntimeError("shutdown failed")
        self.alive = False

    def cleanup_resources(self, restart: bool = False) -> None:
        assert restart is False
        self.cleanup_calls += 1


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch):
    with JupyterKernelManager._instances_lock:  # noqa: SLF001 - lifecycle isolation.
        JupyterKernelManager._instances.clear()  # noqa: SLF001
        JupyterKernelManager._quarantined_instances.clear()  # noqa: SLF001
    monkeypatch.setattr(manager_module, "JUPYTER_AVAILABLE", True)
    monkeypatch.setattr(manager_module, "IMPORT_ERROR", None)
    monkeypatch.setattr(manager_module, "lazy_import_jupyter", lambda: None)
    yield
    with JupyterKernelManager._instances_lock:  # noqa: SLF001
        JupyterKernelManager._instances.clear()  # noqa: SLF001
        JupyterKernelManager._quarantined_instances.clear()  # noqa: SLF001


@pytest.mark.parametrize(
    ("stage", "client_kwargs", "kernel_kwargs"),
    [
        ("start", {}, {"fail_start": True}),
        ("client", {}, {"fail_client": True}),
        ("channels", {"fail_start": True}, {}),
        ("ready", {"fail_ready": True}, {}),
    ],
)
def test_every_post_construction_start_failure_rolls_back_all_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    client_kwargs: dict[str, bool],
    kernel_kwargs: dict[str, bool],
) -> None:
    client = FakeClient(**client_kwargs)
    kernel = FakeKernel(client, **kernel_kwargs)
    monkeypatch.setattr(manager_module, "KernelManager", lambda **_kwargs: kernel)
    manager = JupyterKernelManager(tmp_path)

    with pytest.raises(RuntimeError, match="启动内核失败"):
        manager.start_kernel()

    assert manager._km is None  # noqa: SLF001
    assert manager._kc is None  # noqa: SLF001
    assert kernel.shutdown_calls == 1
    assert kernel.cleanup_calls == 1
    if stage in {"channels", "ready"}:
        assert client.stop_calls == 1
    else:
        assert client.stop_calls == 0
    assert manager.broken is False
    assert manager.last_cleanup_report is not None
    assert manager.last_cleanup_report.orphan_confirmed_absent is True


def test_kernel_manager_constructor_failure_leaves_clean_retryable_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manager_module,
        "KernelManager",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("constructor failed")),
    )
    manager = JupyterKernelManager(tmp_path)

    with pytest.raises(RuntimeError, match="constructor failed"):
        manager.start_kernel()

    assert manager._km is None and manager._kc is None  # noqa: SLF001
    assert manager.broken is False
    assert manager.last_cleanup_report is not None
    assert manager.last_cleanup_report.orphan_confirmed_absent is True


def test_failed_start_can_retry_on_the_same_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeKernel(fail_start=True)
    second = FakeKernel()
    kernels = iter((first, second))
    monkeypatch.setattr(manager_module, "KernelManager", lambda **_kwargs: next(kernels))
    manager = JupyterKernelManager(tmp_path)

    with pytest.raises(RuntimeError):
        manager.start_kernel()
    assert manager.start_kernel() is True

    assert manager.is_running is True
    assert manager._km is second  # noqa: SLF001


def test_start_does_not_publish_partial_kernel_before_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    kernel = FakeKernel(block_start=(entered, release))
    monkeypatch.setattr(manager_module, "KernelManager", lambda **_kwargs: kernel)
    manager = JupyterKernelManager(tmp_path)

    worker = threading.Thread(target=manager.start_kernel)
    worker.start()
    assert entered.wait(timeout=1)
    assert manager._km is None and manager._kc is None  # noqa: SLF001
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert manager._km is kernel  # noqa: SLF001


def test_concurrent_start_creates_exactly_one_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeKernel] = []

    def factory(**_kwargs: Any) -> FakeKernel:
        kernel = FakeKernel()
        original_start = kernel.start_kernel

        def delayed_start() -> None:
            time.sleep(0.03)
            original_start()

        kernel.start_kernel = delayed_start  # type: ignore[method-assign]
        created.append(kernel)
        return kernel

    monkeypatch.setattr(manager_module, "KernelManager", factory)
    manager = JupyterKernelManager(tmp_path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: manager.start_kernel(), range(4)))

    assert results == [True, True, True, True]
    assert len(created) == 1


def test_shutdown_continues_after_channel_failure(
    tmp_path: Path,
) -> None:
    class BrokenClient(FakeClient):
        def stop_channels(self) -> None:
            self.stop_calls += 1
            raise RuntimeError("channel cleanup failed")

    client = BrokenClient()
    kernel = FakeKernel(client)
    kernel.alive = True
    manager = JupyterKernelManager(tmp_path)
    manager._km = kernel  # noqa: SLF001
    manager._kc = client  # noqa: SLF001

    manager.shutdown_kernel()

    assert client.stop_calls == 1
    assert kernel.shutdown_calls == 1
    assert kernel.cleanup_calls == 1
    assert manager._km is None and manager._kc is None  # noqa: SLF001
    assert manager.last_cleanup_report is not None
    assert manager.last_cleanup_report.channels_stopped is False


def test_shutdown_failure_uses_provisioner_kill_fallback(
    tmp_path: Path,
) -> None:
    kernel = FakeKernel(fail_shutdown=True)
    kernel.alive = True

    class Provisioner:
        def __init__(self) -> None:
            self.calls = 0

        async def kill(self, restart: bool = False) -> None:
            assert restart is False
            self.calls += 1
            kernel.alive = False

    provisioner = Provisioner()
    kernel.provisioner = provisioner  # type: ignore[attr-defined]
    manager = JupyterKernelManager(tmp_path)
    manager._km = kernel  # noqa: SLF001
    manager._kc = kernel.client_instance  # noqa: SLF001

    manager.shutdown_kernel()

    assert provisioner.calls == 1
    assert manager.last_cleanup_report is not None
    assert manager.last_cleanup_report.orphan_confirmed_absent is True
    assert "provisioner.kill" in manager.last_cleanup_report.fallback_methods


def test_successful_shutdown_evicts_registered_instance(tmp_path: Path) -> None:
    owner = "user-1"
    manager = JupyterKernelManager.get_instance(tmp_path, owner)
    kernel = FakeKernel()
    kernel.alive = True
    manager._km = kernel  # noqa: SLF001
    manager._kc = kernel.client_instance  # noqa: SLF001

    manager.shutdown_kernel()

    replacement = JupyterKernelManager.get_instance(tmp_path, owner)
    assert replacement is not manager
    assert manager.is_running is False


def test_restart_keeps_registered_manager_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "user-1"
    manager = JupyterKernelManager.get_instance(tmp_path, owner)
    old_kernel = FakeKernel()
    old_kernel.alive = True
    manager._km = old_kernel  # noqa: SLF001
    manager._kc = old_kernel.client_instance  # noqa: SLF001
    new_kernel = FakeKernel()
    monkeypatch.setattr(manager_module, "KernelManager", lambda **_kwargs: new_kernel)

    manager.restart_kernel()

    assert JupyterKernelManager.get_instance(tmp_path, owner) is manager
    assert old_kernel.alive is False
    assert new_kernel.alive is True


@pytest.mark.asyncio
async def test_shutdown_all_async_propagates_cleanup_failures(tmp_path: Path) -> None:
    first = JupyterKernelManager.get_instance(tmp_path / "a", "user-1")
    second = JupyterKernelManager.get_instance(tmp_path / "b", "user-2")
    first.shutdown_kernel = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fail_shutdown(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("cleanup failed")

    second.shutdown_kernel = fail_shutdown  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="1 Jupyter kernel cleanup"):
        await JupyterKernelManager.shutdown_all_async()

    assert JupyterKernelManager._instances == {}  # noqa: SLF001
    assert JupyterKernelManager._quarantined_instances == set()  # noqa: SLF001


def test_unconfirmed_orphan_marks_instance_broken_and_evicts_registry(
    tmp_path: Path,
) -> None:
    owner = "user-1"
    manager = JupyterKernelManager.get_instance(tmp_path, owner)
    kernel = FakeKernel(fail_shutdown=True)
    kernel.alive = True

    class Provisioner:
        process = SimpleNamespace(kill=lambda: (_ for _ in ()).throw(RuntimeError("kill failed")))

        def kill(self, restart: bool = False) -> None:
            del restart
            raise RuntimeError("provisioner kill failed")

    kernel.provisioner = Provisioner()  # type: ignore[attr-defined]

    def private_kill() -> None:
        raise RuntimeError("private kill failed")

    kernel._kill_kernel = private_kill  # type: ignore[attr-defined]
    manager._km = kernel  # noqa: SLF001
    manager._kc = kernel.client_instance  # noqa: SLF001

    with pytest.raises(RuntimeError, match="无法确认"):
        manager.shutdown_kernel()

    assert manager.broken is True
    assert manager in JupyterKernelManager._quarantined_instances  # noqa: SLF001
    replacement = JupyterKernelManager.get_instance(tmp_path, owner)
    assert replacement is not manager
    assert replacement.broken is False


def test_shutdown_waits_for_inflight_start_then_leaves_no_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    kernel = FakeKernel(block_start=(entered, release))
    monkeypatch.setattr(manager_module, "KernelManager", lambda **_kwargs: kernel)
    manager = JupyterKernelManager(tmp_path)

    start_thread = threading.Thread(target=manager.start_kernel)
    stop_thread = threading.Thread(target=manager.shutdown_kernel)
    start_thread.start()
    assert entered.wait(timeout=1)
    stop_thread.start()
    time.sleep(0.03)
    assert stop_thread.is_alive()
    release.set()
    start_thread.join(timeout=2)
    stop_thread.join(timeout=2)

    assert not start_thread.is_alive() and not stop_thread.is_alive()
    assert manager.is_running is False
    assert kernel.alive is False
    assert manager._km is None and manager._kc is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_kernel_shutdown_command_does_not_claim_success_when_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.jupyter import main as jupyter_main

    class BrokenManager:
        def shutdown_kernel(self) -> None:
            raise RuntimeError("orphan still alive")

    monkeypatch.setattr(
        jupyter_main.JupyterKernelManager,
        "get_instance",
        lambda *_args, **_kwargs: BrokenManager(),
    )
    context = SimpleNamespace(
        data_dir=tmp_path,
        current_user_id=1,
        current_group_id=2,
    )

    response = await jupyter_main._handle_kernel("shutdown", context)

    text = response[0]["data"]["text"]
    assert "无法确认" in text
    assert "内核已关闭" not in text
