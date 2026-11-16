# 验证主入口启动、信号处理和运行期资源收口。
import signal
import sys
from pathlib import Path
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
    monkeypatch.setattr(app_module, "XiaoQingApp", lambda *_args, **_kwargs: app)
    monkeypatch.setitem(sys.modules, "torch", ModuleType("torch"))
    if errors:
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await entrypoint.main()
    else:
        await entrypoint.main()
    app.stop.assert_awaited_once()
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("given", [None, "deploy"])
def test_cli_runs_the_app_in_the_runtime_root_with_the_installed_plugins(
    monkeypatch, tmp_path, given
) -> None:
    """配置、数据和日志在运行目录；插件代码总从 plugins 包所在的目录加载。"""
    from core import app as app_module

    built: list[tuple[object, object]] = []

    class StoppedApp:
        shutdown_errors = ()

        def __init__(self, root, plugins_dir=None) -> None:
            built.append((root, plugins_dir))

        async def start(self) -> None:
            raise KeyboardInterrupt

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(app_module, "XiaoQingApp", StoppedApp)
    monkeypatch.setitem(sys.modules, "torch", ModuleType("torch"))
    monkeypatch.chdir(tmp_path)
    argv = [] if given is None else ["--root", given]

    entrypoint.cli(argv)

    checkout = Path(entrypoint.__file__).resolve().parent
    expected = checkout if given is None else (tmp_path / given).resolve()
    assert built == [(expected, checkout / "plugins")]


def test_default_root_outside_a_checkout_is_the_working_directory(monkeypatch, tmp_path) -> None:
    """装成包后 main.py 旁边没有 pyproject.toml：运行目录取当前目录，不写进安装目录。"""

    installed = tmp_path / "site-packages"
    installed.mkdir()
    workdir = tmp_path / "bot"
    workdir.mkdir()
    monkeypatch.setattr(entrypoint, "__file__", str(installed / "main.py"))
    monkeypatch.chdir(workdir)

    assert entrypoint._default_root() == workdir


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
