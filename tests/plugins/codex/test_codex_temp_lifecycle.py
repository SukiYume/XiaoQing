# 验证 Codex 临时文件在成功、失败和取消后回收。
from __future__ import annotations

import asyncio
import shutil
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from plugins.codex import runner as codex_runner
from plugins.codex.config import CodexPluginConfig, load_plugin_config
from plugins.codex.runner import (
    CodexRunner,
    ProcessTreeTerminationResult,
    _archive_large_message,
    _capture_final_output,
    _FinalOutputCapture,
    _remove_output_path_with_retry,
)
from tests.helpers.assertions import logged_events
from tests.helpers.codex_fakes import CallbackStreamingProcess
from tests.helpers.codex_test_support import confirmed_termination, install_termination_probe
from tests.helpers.settings_snapshot import with_settings_reader


@pytest.fixture
def config(tmp_path: Path) -> CodexPluginConfig:
    context = SimpleNamespace(
        data_dir = tmp_path / "plugin-data",
        config   = {
            "plugins": {
                "codex": {
                    "default_cwd"           : str(tmp_path),
                    "allowed_cwd_roots"     : [str(tmp_path)],
                    "job_timeout_seconds"   : 30,
                    "max_stdout_bytes"      : 64 * 1024,
                    "max_stderr_bytes"      : 64 * 1024,
                    "max_json_line_bytes"   : 16 * 1024,
                    "max_final_output_bytes": 64 * 1024,
                    "max_qq_text_chars"     : 2_000,
                }
            }
        },
        secrets={},
    )
    return load_plugin_config(with_settings_reader(context))


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        label            = "lifecycle",
        job_id           = 7,
        process          = None,
        cancel_requested = False,
        cancel_event     = asyncio.Event(),
    )


def _raw_outputs(output_dir: Path) -> list[Path]:
    return list(output_dir.glob("codex-last-*.txt")) if output_dir.exists() else []


class _Writer:
    def write(self, _payload: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _ErrorReader:
    async def read(self, _size: int) -> bytes:
        raise OSError("reader failed")


class _EofReader:
    async def read(self, _size: int) -> bytes:
        return b""


class _BlockingReader:
    def __init__(self) -> None:
        self._never = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        await asyncio.wait_for(self._never.wait(), timeout=10)
        return b""


class _StreamingProcess:
    pid = 43211

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin                  = _Writer()
        self.stdout                 = _ErrorReader()
        self.stderr                 = _EofReader()
        self._never                 = asyncio.Event()

    async def wait(self) -> int:
        await asyncio.wait_for(self._never.wait(), timeout=10)
        return int(self.returncode or 0)

    def kill(self) -> None:
        self.returncode = -9
        self._never.set()


class _BlockingStreamingProcess:
    pid = 43212

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin                  = _Writer()
        self.stdout                 = _BlockingReader()
        self.stderr                 = _BlockingReader()
        self.wait_entered           = asyncio.Event()
        self._never                 = asyncio.Event()

    async def wait(self) -> int:
        self.wait_entered.set()
        await asyncio.wait_for(self._never.wait(), timeout=10)
        return int(self.returncode or 0)

    def kill(self) -> None:
        self.returncode = -9
        self._never.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_step", ["artifact_dir", "codex_lookup"])
async def test_pre_spawn_failures_remove_raw_output(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    output_dir   = tmp_path / "outputs"
    runner       = CodexRunner(config, output_dir)
    artifact_dir = tmp_path / "artifacts" if failing_step == "artifact_dir" else None
    real_mkdir   = Path.mkdir
    real_which   = shutil.which

    def failing_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == artifact_dir:
            raise OSError("preflight failed")
        real_mkdir(self, *args, **kwargs)

    def failing_which(command: str, *args: Any, **kwargs: Any) -> str | None:
        if failing_step == "codex_lookup" and command == config.codex_bin:
            raise OSError("preflight failed")
        return real_which(command, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    monkeypatch.setattr(shutil, "which", failing_which)
    spawn = AsyncMock()
    monkeypatch.setattr(codex_runner.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(OSError, match="preflight failed"):
        await runner.run(
            cwd          = tmp_path,
            prompt       = "body",
            thread_id    = None,
            job          = _job(),
            artifact_dir = artifact_dir,
        )

    spawn.assert_not_awaited()
    assert not _raw_outputs(output_dir)


@pytest.mark.asyncio
async def test_spawn_failure_timeout_and_cancellation_remove_raw_output(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"
    runner     = CodexRunner(config, output_dir)
    monkeypatch.setattr(
        codex_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("spawn failed")),
    )

    with pytest.raises(OSError, match="spawn failed"):
        await runner.run(cwd=tmp_path, prompt="body", thread_id=None, job=_job())
    assert not _raw_outputs(output_dir)

    never = asyncio.Event()

    async def stuck_spawn(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.wait_for(never.wait(), timeout=10)

    monkeypatch.setattr(codex_runner.asyncio, "create_subprocess_exec", stuck_spawn)
    timeout_runner = CodexRunner(replace(config, spawn_timeout_seconds=0.01), output_dir)
    with pytest.raises(RuntimeError, match="spawn timed out"):
        await timeout_runner.run(cwd=tmp_path, prompt="body", thread_id=None, job=_job())
    assert not _raw_outputs(output_dir)

    spawn_entered = asyncio.Event()

    async def cancellable_spawn(*_args: Any, **_kwargs: Any) -> Any:
        spawn_entered.set()
        await asyncio.wait_for(never.wait(), timeout=10)

    monkeypatch.setattr(codex_runner.asyncio, "create_subprocess_exec", cancellable_spawn)
    task = asyncio.create_task(runner.run(cwd=tmp_path, prompt="body", thread_id=None, job=_job()))
    await asyncio.wait_for(spawn_entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _raw_outputs(output_dir)


@pytest.mark.asyncio
async def test_stream_reader_failure_terminates_owned_process_and_removes_raw(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"
    process    = _StreamingProcess()
    monkeypatch.setattr(
        codex_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    probe = install_termination_probe(monkeypatch)

    with pytest.raises(OSError, match="reader failed"):
        await CodexRunner(config, output_dir).run(
            cwd       = tmp_path,
            prompt    = "body",
            thread_id = None,
            job       = _job(),
        )

    assert probe.terminated
    assert all(terminated is process for terminated in probe.terminated)
    assert process.returncode == -9
    assert not _raw_outputs(output_dir)


@pytest.mark.asyncio
async def test_stream_cancel_preserves_cancel_when_first_tree_cleanup_fails(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"
    process    = _BlockingStreamingProcess()
    monkeypatch.setattr(
        codex_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    probe = install_termination_probe(monkeypatch, fail_first=True)
    task = asyncio.create_task(
        CodexRunner(config, output_dir).run(
            cwd       = tmp_path,
            prompt    = "body",
            thread_id = None,
            job       = _job(),
        )
    )
    await asyncio.wait_for(process.wait_entered.wait(), timeout=10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert probe.attempts == [process, process]
    assert probe.terminated == [process]
    assert process.returncode == -9
    assert not _raw_outputs(output_dir)


@pytest.mark.asyncio
async def test_capture_failure_terminates_owned_process_and_removes_raw(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"

    async def exchange(_input: bytes) -> tuple[bytes, bytes]:
        return b"{}\n", b""

    process = CallbackStreamingProcess(exchange, returncode=0)
    monkeypatch.setattr(
        codex_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        codex_runner,
        "_capture_final_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("capture failed")),
    )
    probe = install_termination_probe(monkeypatch)

    with pytest.raises(ValueError, match="capture failed"):
        await CodexRunner(config, output_dir).run(
            cwd       = tmp_path,
            prompt    = "body",
            thread_id = None,
            job       = _job(),
        )

    assert probe.terminated == [process]
    assert not _raw_outputs(output_dir)


@pytest.mark.asyncio
async def test_cancel_during_capture_waits_for_thread_then_removes_windows_raw_file(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir               = tmp_path / "outputs"
    captured_args: list[Any] = []
    entered                  = threading.Event()
    release                  = threading.Event()

    async def exchange(_input: bytes) -> tuple[bytes, bytes]:
        output_path = Path(captured_args[captured_args.index("-o") + 1])
        output_path.write_text("sensitive output", encoding="utf-8")
        return b"", b""

    process = CallbackStreamingProcess(exchange, returncode=0)

    async def spawn(*args: Any, **_kwargs: Any) -> CallbackStreamingProcess:
        captured_args.extend(args)
        return process

    def blocked_capture(path: Path, **_kwargs: Any) -> _FinalOutputCapture:
        with path.open("rb") as handle:
            entered.set()
            release.wait(timeout=5)
            handle.read()
        return _FinalOutputCapture(text="done")

    monkeypatch.setattr(codex_runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codex_runner, "_capture_final_output", blocked_capture)
    probe = install_termination_probe(monkeypatch)
    task  = asyncio.create_task(
        CodexRunner(config, output_dir).run(
            cwd       = tmp_path,
            prompt    = "body",
            thread_id = None,
            job       = _job(),
        )
    )
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)

    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert _raw_outputs(output_dir)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert probe.terminated == [process]
    assert not _raw_outputs(output_dir)


@pytest.mark.asyncio
async def test_cancel_during_final_cleanup_preserves_cancel_and_cleans_all_resources(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir       = tmp_path / "outputs"
    cleanup_entered  = asyncio.Event()
    cleanup_release  = asyncio.Event()
    cleanup_finished = False

    async def exchange(_input: bytes) -> tuple[bytes, bytes]:
        return b"", b""

    process = CallbackStreamingProcess(exchange, returncode=0)
    monkeypatch.setattr(
        codex_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    def capture_with_archive(
        path: Path,
        *,
        output_dir: Path,
        **_kwargs: Any,
    ) -> _FinalOutputCapture:
        path.write_text("raw", encoding="utf-8")
        archive = output_dir / "codex-orphan.txt"
        archive.write_text("orphan", encoding="utf-8")
        return _FinalOutputCapture(text="captured", archive_path=str(archive.resolve()))

    monkeypatch.setattr(codex_runner, "_capture_final_output", capture_with_archive)
    monkeypatch.setattr(
        codex_runner,
        "_qq_preview",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("post-capture failed")),
    )

    async def blocked_terminate(terminating: Any, **_kwargs: Any) -> ProcessTreeTerminationResult:
        nonlocal cleanup_finished
        cleanup_entered.set()
        await asyncio.wait_for(cleanup_release.wait(), timeout=10)
        cleanup_finished       = True
        terminating.returncode = -9
        return confirmed_termination(forced=True)

    monkeypatch.setattr(codex_runner, "terminate_process_tree", blocked_terminate)
    task = asyncio.create_task(
        CodexRunner(config, output_dir).run(
            cwd       = tmp_path,
            prompt    = "body",
            thread_id = None,
            job       = _job(),
        )
    )
    await asyncio.wait_for(cleanup_entered.wait(), timeout=10)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished is True
    assert not _raw_outputs(output_dir)
    assert not (output_dir / "codex-orphan.txt").exists()


@pytest.mark.asyncio
async def test_cancel_during_final_unlink_does_not_return_normal_result(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir               = tmp_path / "outputs"
    unlink_entered           = asyncio.Event()
    unlink_release           = asyncio.Event()
    captured_args: list[Any] = []

    async def exchange(_input: bytes) -> tuple[bytes, bytes]:
        output_path = Path(captured_args[captured_args.index("-o") + 1])
        output_path.write_text(
            "archive" * config.max_qq_text_chars,
            encoding="utf-8",
        )
        return b"", b""

    process = CallbackStreamingProcess(exchange, returncode=0)

    async def spawn(*args: Any, **_kwargs: Any) -> CallbackStreamingProcess:
        captured_args.extend(args)
        return process

    monkeypatch.setattr(codex_runner.asyncio, "create_subprocess_exec", spawn)

    async def blocked_remove(path: Path, **_kwargs: Any) -> bool:
        unlink_entered.set()
        await asyncio.wait_for(unlink_release.wait(), timeout=10)
        path.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(codex_runner, "_remove_output_path_with_retry", blocked_remove)
    probe = install_termination_probe(monkeypatch)
    task  = asyncio.create_task(
        CodexRunner(config, output_dir).run(
            cwd       = tmp_path,
            prompt    = "body",
            thread_id = None,
            job       = _job(),
        )
    )
    await asyncio.wait_for(unlink_entered.wait(), timeout=10)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    unlink_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert probe.attempts == []
    assert not _raw_outputs(output_dir)
    assert not list(output_dir.glob("codex-*.txt"))


@pytest.mark.asyncio
async def test_unlink_retries_permission_error_and_logs_only_bounded_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "codex-last-retry.txt"
    path.write_text("secret", encoding="utf-8")
    original_unlink = Path.unlink
    calls           = 0

    def flaky_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        if self == path:
            calls += 1
            if calls < 3:
                raise PermissionError("locked")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert await _remove_output_path_with_retry(path, attempts=3, delay_seconds=0) is True
    assert calls == 3
    assert not path.exists()

    failed = tmp_path / "codex-last-stuck.txt"
    failed.write_text("secret", encoding="utf-8")

    def always_locked(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == failed:
            raise PermissionError("full path and secret must not be logged")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", always_locked)
    with caplog.at_level("WARNING"):
        assert await _remove_output_path_with_retry(failed, attempts=2, delay_seconds=0) is False
    [event] = logged_events(caplog, "output_remove.failed")
    assert event.fields["file"] == "codex-last-stuck.txt"
    assert event.fields["error"] == "PermissionError"
    assert "full path and secret" not in caplog.text


def test_archive_write_and_replace_failures_roll_back_partial_destinations(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    original_write_bytes = Path.write_bytes

    def partial_write(self: Path, data: bytes) -> int:
        if self.name.startswith("codex-"):
            original_write_bytes(self, b"partial")
            raise OSError("write failed")
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    with pytest.raises(OSError, match="write failed"):
        _archive_large_message(
            "x" * (config.max_qq_text_chars + 1),
            output_dir = output_dir,
            job        = _job(),
            config     = config,
        )
    assert not list(output_dir.glob("codex-*.txt"))

    monkeypatch.setattr(Path, "write_bytes", original_write_bytes)
    raw = output_dir / "raw.txt"
    raw.write_text("x" * (config.max_qq_text_chars + 1), encoding="utf-8")
    monkeypatch.setattr(
        codex_runner.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        _capture_final_output(
            raw,
            output_dir = output_dir,
            job        = _job(),
            config     = config,
        )
    assert raw.exists()
    assert not list(output_dir.glob("codex-*.txt"))


@pytest.mark.asyncio
async def test_success_keeps_archive_does_not_kill_and_closes_handle_before_spawn(
    tmp_path: Path,
    config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir                   = tmp_path / "outputs"
    captured_output: Path | None = None
    long_text                    = "q" * (config.max_qq_text_chars + 1)

    async def exchange(_input: bytes) -> tuple[bytes, bytes]:
        assert captured_output is not None
        captured_output.write_text(long_text, encoding="utf-8")
        return b"", b""

    process = CallbackStreamingProcess(exchange, returncode=0)

    async def spawn(*args: Any, **_kwargs: Any) -> CallbackStreamingProcess:
        nonlocal captured_output
        captured_output = Path(args[args.index("-o") + 1])
        # This open occurs during spawn and proves NamedTemporaryFile is already closed.
        captured_output.write_text("spawn can reopen", encoding="utf-8")
        return process

    monkeypatch.setattr(codex_runner.asyncio, "create_subprocess_exec", spawn)
    probe = install_termination_probe(monkeypatch)

    result = await CodexRunner(config, output_dir).run(
        cwd       = tmp_path,
        prompt    = "unrestricted admin prompt",
        thread_id = None,
        job       = _job(),
    )

    assert probe.attempts == []
    assert result.output_path is not None
    archive = Path(result.output_path)
    assert archive.exists()
    assert archive.read_text(encoding="utf-8") == long_text
    assert not _raw_outputs(output_dir)
