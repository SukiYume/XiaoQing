import builtins
import os
import signal

import main as entrypoint


def test_runtime_preparation_does_not_override_openmp_policy(monkeypatch) -> None:
    original_import = builtins.__import__

    def without_torch(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("optional dependency unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_torch)
    monkeypatch.setenv("KMP_DUPLICATE_LIB_OK", "operator-chosen-value")

    entrypoint._prepare_runtime()

    assert os.environ["KMP_DUPLICATE_LIB_OK"] == "operator-chosen-value"


def test_runtime_preparation_does_not_create_openmp_workaround(monkeypatch) -> None:
    monkeypatch.delenv("KMP_DUPLICATE_LIB_OK", raising=False)
    entrypoint._prepare_runtime()

    assert "KMP_DUPLICATE_LIB_OK" not in os.environ


def test_windows_runtime_handles_ctrl_break_for_graceful_shutdown(monkeypatch) -> None:
    installed: dict[object, object] = {}
    scheduled: list[object] = []
    marker = object()

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
