from __future__ import annotations

import asyncio
import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.codex import runner


class _FakeProcess:
    def __init__(self, *, pid: int = 43210, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.exited = asyncio.Event()
        self.kill_calls = 0
        if returncode is not None:
            self.exited.set()

    async def wait(self) -> int | None:
        await self.exited.wait()
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self.exited.set()


class _FakeTaskkill(_FakeProcess):
    def __init__(self, target: _FakeProcess, *, code: int = 0, hang: bool = False) -> None:
        super().__init__(pid=9876)
        self.target = target
        self.code = code
        self.hang = hang

    async def wait(self) -> int:
        if self.hang:
            await self.exited.wait()
            return int(self.returncode or 0)
        self.returncode = self.code
        self.exited.set()
        if self.code == 0:
            self.target.returncode = -15
            self.target.exited.set()
        return self.code


@pytest.mark.asyncio
async def test_posix_kills_process_group_even_when_parent_already_exited(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    sigterm = getattr(signal, "SIGTERM", 15)
    sigkill = getattr(signal, "SIGKILL", 9)
    monkeypatch.setattr(runner.signal, "SIGTERM", sigterm, raising=False)
    monkeypatch.setattr(runner.signal, "SIGKILL", sigkill, raising=False)
    process = _FakeProcess(returncode=0)
    group_alive = True
    signals: list[int] = []

    def killpg(_process_group_id: int, sent_signal: int) -> None:
        nonlocal group_alive
        if sent_signal == 0:
            if group_alive:
                return
            raise ProcessLookupError
        signals.append(sent_signal)
        if sent_signal == sigkill:
            group_alive = False

    monkeypatch.setattr(runner.os, "killpg", killpg, raising=False)

    result = await runner.terminate_process_tree(
        process,  # type: ignore[arg-type]
        term_grace_seconds=0,
        kill_timeout_seconds=0.1,
    )

    assert signals == [sigterm, sigkill]
    assert result.tree_confirmed is True
    assert result.parent_reaped is True
    assert result.forced is True


@pytest.mark.asyncio
async def test_windows_awaits_taskkill_and_confirms_tree(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "win32")
    target = _FakeProcess()
    helper = _FakeTaskkill(target, code=0)
    create_helper = AsyncMock(return_value=helper)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", create_helper)

    result = await runner.terminate_process_tree(
        target,  # type: ignore[arg-type]
        helper_timeout_seconds=0.1,
        kill_timeout_seconds=0.1,
    )

    assert result.tree_confirmed is True
    assert result.parent_reaped is True
    assert result.helper_error is None
    create_helper.assert_awaited_once_with(
        "taskkill",
        "/PID",
        str(target.pid),
        "/T",
        "/F",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("helper_code", "hang"), [(1, False), (0, True)])
async def test_windows_taskkill_failure_or_timeout_falls_back_to_parent_kill(
    monkeypatch,
    helper_code,
    hang,
):
    monkeypatch.setattr(runner.sys, "platform", "win32")
    target = _FakeProcess()
    helper = _FakeTaskkill(target, code=helper_code, hang=hang)
    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=helper),
    )

    result = await runner.terminate_process_tree(
        target,  # type: ignore[arg-type]
        helper_timeout_seconds=0.01,
        kill_timeout_seconds=0.01,
    )

    assert result.tree_confirmed is False
    assert result.parent_reaped is True
    assert result.forced is True
    assert result.helper_error
    assert target.kill_calls == 1
    if hang:
        assert helper.kill_calls == 1


@pytest.mark.asyncio
async def test_windows_attempts_taskkill_even_if_parent_has_exited(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "win32")
    target = _FakeProcess(returncode=0)
    helper = _FakeTaskkill(target, code=0)
    create_helper = AsyncMock(return_value=helper)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", create_helper)

    result = await runner.terminate_process_tree(
        target,  # type: ignore[arg-type]
        helper_timeout_seconds=0.1,
        kill_timeout_seconds=0.1,
    )

    assert create_helper.await_count == 1
    assert result.tree_confirmed is True
    assert result.parent_reaped is True


@pytest.mark.asyncio
async def test_pipe_drain_has_hard_deadline_and_closes_transports():
    never = asyncio.Event()

    class _HangingProcess:
        stdin = MagicMock()

        async def communicate(self):
            await never.wait()

    process = _HangingProcess()
    pipe_transports = [MagicMock(), MagicMock(), MagicMock()]
    process._transport = SimpleTransport = MagicMock()  # noqa: N806
    SimpleTransport.get_pipe_transport.side_effect = pipe_transports

    stdout, stderr = await runner._drain_process_after_termination(
        process,  # type: ignore[arg-type]
        timeout_seconds=0.01,
    )

    assert (stdout, stderr) == (b"", b"")
    process.stdin.close.assert_called_once()
    assert all(transport.close.call_count == 1 for transport in pipe_transports)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group integration test")
@pytest.mark.asyncio
async def test_real_posix_parent_and_term_ignoring_child_are_reaped():
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print(child.pid, flush=True); "
        "time.sleep(60)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int((await asyncio.wait_for(process.stdout.readline(), timeout=2)).strip())

    try:
        result = await runner.terminate_process_tree(
            process,
            term_grace_seconds=0.2,
            kill_timeout_seconds=2,
        )
        assert result.parent_reaped is True
        assert result.tree_confirmed is True
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
