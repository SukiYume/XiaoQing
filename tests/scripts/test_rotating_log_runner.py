"""日志轮转子进程、PID 提交和进程树回收测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import run_process_with_rotating_logs as log_pump
from tests.helpers.bot_monitor_test_support import (
    LOG_PUMP,
    MONITOR,
)
from tests.helpers.bot_monitor_test_support import (
    assert_process_not_running as _assert_process_not_running,
)
from tests.helpers.bot_monitor_test_support import (
    pid_marker_child_source as _pid_marker_child_source,
)
from tests.helpers.bot_monitor_test_support import (
    powershell_executable as _powershell_executable,
)
from tests.helpers.bot_monitor_test_support import (
    run_powershell as _run_powershell,
)


def test_log_pump_rotates_both_streams_while_child_remains_running(tmp_path: Path) -> None:
    working_directory = tmp_path / "working directory"
    working_directory.mkdir()
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    completion_marker = working_directory / "completed.txt"
    child = (
        "import os, pathlib, sys, time\n"
        "marker = pathlib.Path(sys.argv[1])\n"
        "for index in range(400):\n"
        "    os.write(sys.stdout.fileno(), f'OUT-{index:04d}|'.encode() + b'o' * 1014)\n"
        "    os.write(sys.stderr.fileno(), f'ERR-{index:04d}|'.encode() + b'e' * 1014)\n"
        "    if index % 25 == 0:\n"
        "        time.sleep(0.002)\n"
        "marker.write_text('completed\\n', encoding='utf-8')\n"
    )
    command = [
        sys.executable,
        str(LOG_PUMP),
        "--stdout-log",
        str(stdout_log),
        "--stderr-log",
        str(stderr_log),
        "--max-bytes",
        str(64 * 1024),
        "--backup-count",
        "2",
        "--cwd",
        str(working_directory),
        "--",
        sys.executable,
        "-c",
        child,
        str(completion_marker),
    ]

    result = subprocess.run(command, check=False, capture_output=True, timeout=20)

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert completion_marker.read_text(encoding="utf-8") == "completed\n"
    for log_path, marker in ((stdout_log, b"OUT-"), (stderr_log, b"ERR-")):
        family = [log_path, Path(f"{log_path}.1"), Path(f"{log_path}.2")]
        assert all(path.is_file() for path in family)
        assert all(0 < path.stat().st_size <= 64 * 1024 for path in family)
        assert marker in b"".join(path.read_bytes() for path in family)
        assert {path.name for path in tmp_path.glob(f"{log_path.name}.*")} == {
            f"{log_path.name}.1",
            f"{log_path.name}.2",
        }


def test_existing_oversized_log_is_streamed_trimmed_and_aliases_are_pruned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "existing.log"
    maximum_bytes = 2 * log_pump._COPY_SIZE + 123
    original = b"prefix" * 1000 + os.urandom(maximum_bytes + 5000)
    log_path.write_bytes(original)
    for suffix in ("0", "00", "01", "1", "2", "3"):
        Path(f"{log_path}.{suffix}").write_bytes(f"backup-{suffix}".encode())
    Path(f"{log_path}.keep").write_text("unrelated", encoding="utf-8")

    original_open = Path.open
    read_sizes: list[int] = []

    class GuardedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name: str):
            return getattr(self.stream, name)

        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= log_pump._COPY_SIZE
            read_sizes.append(size)
            return self.stream.read(size)

    def guarded_open(path: Path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == log_path and mode == "rb":
            return GuardedReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", guarded_open)
    with log_pump.BoundedRotatingLog(
        log_path,
        maximum_bytes=maximum_bytes,
        backup_count=2,
    ):
        pass

    assert read_sizes
    assert max(read_sizes) <= log_pump._COPY_SIZE
    assert Path(f"{log_path}.1").read_bytes() == original[-maximum_bytes:]
    assert Path(f"{log_path}.keep").read_text(encoding="utf-8") == "unrelated"
    numeric_backups = {
        path.name
        for path in tmp_path.glob(f"{log_path.name}.*")
        if path.name.removeprefix(f"{log_path.name}.").isdecimal()
    }
    assert numeric_backups == {f"{log_path.name}.1", f"{log_path.name}.2"}
    assert all(
        path.stat().st_size <= maximum_bytes
        for path in (log_path, Path(f"{log_path}.1"), Path(f"{log_path}.2"))
    )
    assert not tuple(tmp_path.glob(f".{log_path.name}.*.trim"))


def test_log_failure_stops_long_running_child_instead_of_discarding_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = log_pump.BoundedRotatingLog.write
    failed_once = False

    def fail_first_write(self: log_pump.BoundedRotatingLog, data: bytes) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("simulated full disk")
        original_write(self, data)

    monkeypatch.setattr(log_pump.BoundedRotatingLog, "write", fail_first_write)
    child = "import os, sys, time; os.write(sys.stdout.fileno(), b'output'); time.sleep(60)"
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="log pump failed"):
        log_pump.run_process(
            [sys.executable, "-c", child],
            working_directory=tmp_path,
            stdout_log=tmp_path / "stdout.log",
            stderr_log=tmp_path / "stderr.log",
            maximum_bytes=1024,
            backup_count=1,
        )

    assert failed_once
    assert time.monotonic() - started < 10


def test_thread_start_failure_reaps_owned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_marker = tmp_path / "child-started.txt"
    child = _pid_marker_child_source()
    original_start = log_pump.threading.Thread.start
    starts = 0

    def fail_second_start(thread: log_pump.threading.Thread) -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            deadline = time.monotonic() + 3
            while not pid_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            raise RuntimeError("simulated thread start failure")
        original_start(thread)

    monkeypatch.setattr(log_pump.threading.Thread, "start", fail_second_start)

    with pytest.raises(RuntimeError, match="simulated thread start failure"):
        log_pump.run_process(
            [sys.executable, "-c", child, str(pid_marker)],
            working_directory=tmp_path,
            stdout_log=tmp_path / "stdout.log",
            stderr_log=tmp_path / "stderr.log",
            maximum_bytes=1024,
            backup_count=1,
        )

    assert pid_marker.is_file()
    child_pid = int(pid_marker.read_text(encoding="utf-8"))
    _assert_process_not_running(child_pid)


def test_second_thread_construction_failure_reaps_owned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_marker = tmp_path / "constructor-child-started.txt"
    child = _pid_marker_child_source()
    original_thread = log_pump.threading.Thread
    constructions = 0

    def fail_second_construction(*args, **kwargs):
        nonlocal constructions
        constructions += 1
        if constructions == 2:
            deadline = time.monotonic() + 3
            while not started_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            raise RuntimeError("simulated thread construction failure")
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(log_pump.threading, "Thread", fail_second_construction)

    with pytest.raises(RuntimeError, match="simulated thread construction failure"):
        log_pump.run_process(
            [sys.executable, "-c", child, str(started_marker)],
            working_directory=tmp_path,
            stdout_log=tmp_path / "constructor-stdout.log",
            stderr_log=tmp_path / "constructor-stderr.log",
            maximum_bytes=1024,
            backup_count=1,
        )

    assert started_marker.is_file()
    _assert_process_not_running(int(started_marker.read_text(encoding="utf-8")))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are not available on Windows")
def test_log_failure_terminates_posix_descendant_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "grandchild.pid"
    child = (
        "import os, pathlib, signal, subprocess, sys, time\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(grandchild.pid), encoding='utf-8')\n"
        "def stop(_signum, _frame):\n"
        "    grandchild.wait(timeout=5)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "os.write(sys.stdout.fileno(), b'trigger')\n"
        "time.sleep(60)\n"
    )

    def fail_write(_self: log_pump.BoundedRotatingLog, _data: bytes) -> None:
        raise OSError("simulated full disk")

    monkeypatch.setattr(log_pump.BoundedRotatingLog, "write", fail_write)

    with pytest.raises(RuntimeError, match="log pump failed"):
        log_pump.run_process(
            [sys.executable, "-c", child, str(marker)],
            working_directory=tmp_path,
            stdout_log=tmp_path / "stdout.log",
            stderr_log=tmp_path / "stderr.log",
            maximum_bytes=1024,
            backup_count=1,
        )

    grandchild_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"POSIX descendant process survived cleanup: {grandchild_pid}")


@pytest.mark.skipif(os.name != "nt", reason="taskkill fallback is Windows-specific")
def test_taskkill_failure_is_recorded_and_falls_back_to_direct_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 24680
        returncode = None
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            assert timeout == log_pump._SHUTDOWN_TIMEOUT_SECONDS
            return 9

    process = FakeProcess()
    completed = subprocess.CompletedProcess([], 1)
    monkeypatch.setattr(log_pump.subprocess, "run", lambda *_args, **_kwargs: completed)

    return_code, cleanup_error = log_pump._terminate_process_tree(process)

    assert return_code == 9
    assert process.terminated
    assert cleanup_error is not None
    assert "taskkill failed" in str(cleanup_error)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--max-bytes", str(64 * 1024 - 1)),
        ("--max-bytes", str(10 * 1024**3 + 1)),
        ("--backup-count", "0"),
        ("--backup-count", "101"),
    ],
)
def test_log_pump_cli_rejects_out_of_range_limits(
    tmp_path: Path,
    option: str,
    value: str,
) -> None:
    arguments = [
        sys.executable,
        str(LOG_PUMP),
        "--stdout-log",
        str(tmp_path / "stdout.log"),
        "--stderr-log",
        str(tmp_path / "stderr.log"),
        "--max-bytes",
        str(64 * 1024),
        "--backup-count",
        "1",
        "--cwd",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    arguments[arguments.index(option) + 1] = value

    result = subprocess.run(arguments, check=False, capture_output=True, timeout=10)

    assert result.returncode != 0


def test_log_pump_cli_rejects_invalid_paths_before_launch(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    base = [
        sys.executable,
        str(LOG_PUMP),
        "--stdout-log",
        str(stdout_log),
        "--stderr-log",
        str(stderr_log),
        "--max-bytes",
        str(64 * 1024),
        "--backup-count",
        "1",
        "--cwd",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    missing_cwd = list(base)
    missing_cwd[missing_cwd.index("--cwd") + 1] = str(tmp_path / "missing")
    shared_log = list(base)
    shared_log[shared_log.index("--stderr-log") + 1] = str(stdout_log)

    for command in (missing_cwd, shared_log):
        result = subprocess.run(command, check=False, capture_output=True, timeout=10)
        assert result.returncode != 0

    stdout_log.mkdir()
    result = subprocess.run(base, check=False, capture_output=True, timeout=10)
    assert result.returncode != 0


def test_log_pump_requests_hidden_windows_children() -> None:
    source = LOG_PUMP.read_text(encoding="utf-8")

    assert "subprocess.CREATE_NO_WINDOW" in source
    assert "subprocess.STARTUPINFO" in source
    assert "subprocess.STARTF_USESHOWWINDOW" in source
    assert "subprocess.SW_HIDE" in source
    assert 'start_new_session=os.name != "nt"' in source


def test_log_pump_direct_api_rejects_invalid_inputs_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid input must be rejected before Popen")

    monkeypatch.setattr(log_pump.subprocess, "Popen", unexpected_spawn)
    common = {
        "command": [sys.executable, "-c", "pass"],
        "working_directory": tmp_path,
        "stdout_log": tmp_path / "stdout.log",
        "stderr_log": tmp_path / "stderr.log",
        "maximum_bytes": 1024,
        "backup_count": 1,
    }

    with pytest.raises(ValueError, match="working_directory"):
        log_pump.run_process(**{**common, "working_directory": tmp_path / "missing"})
    with pytest.raises(ValueError, match="maximum_bytes"):
        log_pump.run_process(**{**common, "maximum_bytes": 0})
    with pytest.raises(ValueError, match="backup_count"):
        log_pump.run_process(**{**common, "backup_count": 0})
    with pytest.raises(ValueError, match="different files"):
        log_pump.run_process(**{**common, "stderr_log": tmp_path / "stdout.log"})


def test_pid_commit_failure_invokes_owned_tree_cleanup() -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")
    environment = {**os.environ, "XIAOQING_MONITOR_AST_PATH": str(MONITOR)}
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('Write-ProcessIdFile', 'Start-TrackedBot')
}, $true)
foreach ($definition in $definitions) {
    Invoke-Expression $definition.Extent.Text
}
$script:PidFile = Join-Path ([IO.Path]::GetTempPath()) 'xiaoqing-monitor-nonexistent.pid'
$script:MainScript = 'C:\repo\main.py'
$script:LogPumpScript = 'C:\repo\scripts\run_process_with_rotating_logs.py'
$script:BotRoot = 'C:\repo'
$script:BotLog = 'C:\repo\stdout.log'
$script:BotErrorLog = 'C:\repo\stderr.log'
$script:BotArguments = @()
$script:StoppedProcessId = 0
function Start-LogPumpedProcess {
    param($CommandArguments, $WorkingDirectory, $StandardOutputLog, $StandardErrorLog)
    return [pscustomobject]@{ Id = 424242 }
}
function Write-Utf8NoBomAtomically { throw 'simulated PID commit failure' }
function Stop-OwnedProcessTree {
    param($Process)
    $script:StoppedProcessId = $Process.Id
}
try {
    Start-TrackedBot | Out-Null
    Write-Error 'Start-TrackedBot unexpectedly succeeded'
    exit 20
} catch {
    if ($script:StoppedProcessId -ne 424242) {
        Write-Error "owned helper was not stopped: $script:StoppedProcessId"
        exit 21
    }
    if ($_.Exception.Message -notmatch 'simulated PID commit failure') {
        Write-Error "wrong error was propagated: $($_.Exception.Message)"
        exit 22
    }
    exit 0
}
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr


def test_tracked_pid_recovery_does_not_hide_cim_or_identity_failures(
    tmp_path: Path,
) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")
    pid_file = tmp_path / "tracked.pid.json"
    pid_file.write_text('{"process_id":424242}\n', encoding="utf-8")
    environment = {
        **os.environ,
        "XIAOQING_MONITOR_AST_PATH": str(MONITOR),
        "XIAOQING_TEST_PID_FILE": str(pid_file),
    }
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @(
            'Test-CommandLineContains',
            'Get-CimProcessWithReadableCommandLine',
            'Get-TrackedProcessFromFile',
            'Get-TrackedBotProcess'
        )
}, $true)
foreach ($definition in $definitions) {
    Invoke-Expression $definition.Extent.Text
}
$script:PidFile = $env:XIAOQING_TEST_PID_FILE
$script:LogPumpScript = 'C:\repo\scripts\run_process_with_rotating_logs.py'
$script:MainScript = 'C:\repo\main.py'

function Start-Sleep {}
function Get-CimInstance {
    $script:CimAttempts++
    throw 'simulated CIM failure'
}
$script:CimAttempts = 0
try {
    Get-TrackedBotProcess | Out-Null
    exit 51
} catch {
    if ($_.Exception.Message -notlike '*simulated CIM failure*') { exit 52 }
}
if ($script:CimAttempts -ne 3) { exit 59 }
if (-not (Test-Path -LiteralPath $script:PidFile -PathType Leaf)) { exit 53 }

function Get-CimInstance {
    $script:CimAttempts++
    return [pscustomobject]@{ CommandLine = $null }
}
$script:CimAttempts = 0
try {
    Get-TrackedBotProcess | Out-Null
    exit 54
} catch {
    if ($_.Exception.Message -notlike '*Unable to verify command line*') { exit 55 }
    if ($_.Exception -isnot [UnauthorizedAccessException]) { exit 62 }
}
if ($script:CimAttempts -ne 3) { exit 60 }
if (-not (Test-Path -LiteralPath $script:PidFile -PathType Leaf)) { exit 56 }

function Get-CimInstance {
    $script:CimAttempts++
    if ($script:CimAttempts -lt 3) {
        return [pscustomobject]@{ CommandLine = $null }
    }
    return [pscustomobject]@{
        CommandLine = 'python C:\repo\scripts\run_process_with_rotating_logs.py C:\repo\main.py'
    }
}
$script:CimAttempts = 0
$recovered = Get-TrackedBotProcess
if ($null -eq $recovered -or $script:CimAttempts -ne 3) { exit 61 }

[IO.File]::WriteAllText($script:PidFile, '{', [Text.Encoding]::UTF8)
$script:CimWasCalled = $false
function Get-CimInstance { $script:CimWasCalled = $true }
$result = Get-TrackedBotProcess
if ($null -ne $result -or $script:CimWasCalled) { exit 57 }
if (Test-Path -LiteralPath $script:PidFile) { exit 58 }
exit 0
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr
