# 验证主入口启动、信号处理和运行期资源收口。
import signal
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main as entrypoint


@pytest.fixture
def restore_signal_handlers():
    """真实的信号处理器由 main() 安装，用例结束后还原进程原来的处理器。"""

    names    = ("SIGINT", "SIGTERM", "SIGBREAK")
    original = {
        getattr(signal, name): signal.getsignal(getattr(signal, name))
        for name in names
        if hasattr(signal, name)
    }
    yield
    for sig, handler in original.items():
        signal.signal(sig, handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("errors", [(), ("pending plugin cleanup",)])
async def test_main_reports_incomplete_shutdown(
    monkeypatch, capsys, restore_signal_handlers, errors
):
    """收到停止信号后同一退出路径区分完整回收与残留错误，避免服务进程误报成功。"""
    from core import app as app_module
    from core.app_lifecycle import AppLifecycleMixin

    state                       = AppLifecycleMixin()
    state._last_shutdown_errors = errors

    async def start_then_interrupt() -> None:
        signal.raise_signal(signal.SIGINT)

    app = SimpleNamespace(
        start=start_then_interrupt, stop=AsyncMock(), shutdown_errors=state.shutdown_errors
    )
    monkeypatch.setattr(app_module, "XiaoQingApp", lambda _: app)
    monkeypatch.setitem(sys.modules, "torch", ModuleType("torch"))
    if errors:
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await entrypoint.main()
    else:
        await entrypoint.main()
    app.stop.assert_awaited_once()
    assert capsys.readouterr().out.strip()


def test_windows_runtime_handles_ctrl_break_for_graceful_shutdown(monkeypatch) -> None:
    installed: dict[object, object] = {}
    scheduled: list[object]         = []
    marker                          = object()

    class Loop:
        def call_soon_threadsafe(self, callback) -> None:
            scheduled.append(callback())

    monkeypatch.setattr(entrypoint.sys, "platform", "win32")
    monkeypatch.setattr(entrypoint.signal, "SIGBREAK", marker, raising=False)
    monkeypatch.setattr(
        entrypoint.signal,
        "signal",
        lambda sig, handler: installed.__setitem__(sig, handler),
    )

    entrypoint._install_stop_signal_handlers(Loop(), lambda: "stopped")  # type: ignore[arg-type]

    assert signal.SIGINT in installed
    assert marker in installed
    installed[marker](marker, None)  # type: ignore[operator]
    assert scheduled == ["stopped"]
