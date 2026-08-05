from __future__ import annotations

import json
import locale
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import run_process_with_rotating_logs as log_pump

ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / "scripts" / "run-bot-monitor.ps1"
LOG_PUMP = ROOT / "scripts" / "run_process_with_rotating_logs.py"
POWERSHELL_TIMEOUT_SECONDS = 30


def _powershell_executable() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh")


def _powershell_output_encoding(executable: str) -> str:
    """返回 PowerShell 宿主实际使用的管道文本编码。"""

    if os.name == "nt" and Path(executable).name.casefold() == "powershell.exe":
        # Windows PowerShell 5.1 仍按系统代码页写入重定向管道；显式使用
        # locale.getencoding()，避免 Python UTF-8 模式覆盖本机代码页。
        return locale.getencoding()
    return "utf-8"


def _run_powershell(
    executable: str,
    *arguments: str,
    env: dict[str, str] | None = None,
    timeout: float = POWERSHELL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """运行 PowerShell，并稳定解码不同宿主产生的诊断输出。"""

    return subprocess.run(
        [executable, "-NoLogo", "-NoProfile", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding=_powershell_output_encoding(executable),
        errors="replace",
        env=env,
        timeout=timeout,
    )


def _assert_process_not_running(process_id: int) -> None:
    if os.name == "nt":
        executable = _powershell_executable()
        assert executable is not None
        environment = {**os.environ, "XIAOQING_TEST_CHILD_PID": str(process_id)}
        result = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-Command",
                "if (Get-Process -Id $env:XIAOQING_TEST_CHILD_PID "
                "-ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }",
            ],
            check=False,
            capture_output=True,
            env=environment,
            timeout=POWERSHELL_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)


def test_vbs_launcher_delegates_to_the_monitor() -> None:
    source = (ROOT / "scripts" / "run-bot.vbs").read_text(encoding="utf-8")

    assert 'fso.BuildPath(scriptDir, "run-bot-monitor.ps1")' in source
    assert "-WindowStyle Hidden" in source
    assert "tasklist" not in source.lower()
    assert "wmic" not in source.lower()


def test_monitor_uses_scoped_identity_configured_launch_and_log_pump() -> None:
    source = MONITOR.read_text(encoding="utf-8")

    assert "XiaoQing.BotMonitor" in source
    assert "xiaoqing-bot.pid.json" in source
    assert "Get-CimInstance" in source
    assert "Test-CommandLineContains" in source
    assert "MaximumRestartDelaySeconds" in source
    assert "Get-TrackedBotProcess" in source
    assert "run_process_with_rotating_logs.py" in source
    assert "-WindowStyle Hidden" in source
    assert "Write-Utf8NoBomAtomically" in source
    assert "Stop-OwnedProcessTree" in source
    assert "taskkill.exe" in source
    assert "WaitForExit(5000)" in source
    assert "[IO.File]::WriteAllText" in source
    assert "UTF8Encoding" in source
    assert "tasklist" not in source.lower()
    assert "wmic" not in source.lower()
    assert "RedirectStandardOutput" not in source
    assert "RedirectStandardError" not in source
    assert "Rotate-Log" not in source
    assert "utf8NoBOM" not in source


def test_monitor_reads_launcher_config_without_baking_deployment_values() -> None:
    source = MONITOR.read_text(encoding="utf-8")

    assert '"config\\config.json"' in source
    assert 'Properties["napcat_account"]' in source
    assert 'Properties["mkl_threading_layer"]' in source
    assert '"MKL_THREADING_LAYER"' in source
    assert "XIAOQING_NAPCAT_ACCOUNT" not in source
    assert "CondaPath" not in source
    assert "CondaEnvironment" not in source
    assert "PythonPath" not in source
    assert '"python"' in source
    for parameter in (
        "$BotArguments",
        "$DisableNapCat",
        "$NapCatArguments",
    ):
        assert parameter in source


def test_monitor_resolves_script_relative_defaults_after_parameter_binding() -> None:
    source = MONITOR.read_text(encoding="utf-8")
    parameter_block, runtime_body = source.split("Set-StrictMode", maxsplit=1)

    assert "$PSScriptRoot" not in parameter_block
    assert "$ScriptDirectory = $PSScriptRoot" in runtime_body
    assert "$BotRoot = Split-Path -Parent $ScriptDirectory" in runtime_body
    assert "Unable to resolve XiaoQing monitor script directory" in runtime_body


def test_monitor_passes_configured_account_as_first_napcat_argument(tmp_path: Path) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")

    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "napcat_account": "123456789",
                "mkl_threading_layer": "TBB",
            }
        ),
        encoding="utf-8",
    )
    invalid_type_file = tmp_path / "invalid-type.json"
    invalid_type_file.write_text(json.dumps({"napcat_account": 123456789}), encoding="utf-8")
    invalid_value_file = tmp_path / "invalid-value.json"
    invalid_value_file.write_text(json.dumps({"napcat_account": "not-an-account"}), encoding="utf-8")
    invalid_mkl_type_file = tmp_path / "invalid-mkl-type.json"
    invalid_mkl_type_file.write_text(
        json.dumps({"mkl_threading_layer": 123}),
        encoding="utf-8",
    )
    invalid_mkl_value_file = tmp_path / "invalid-mkl-value.json"
    invalid_mkl_value_file.write_text(
        json.dumps({"mkl_threading_layer": "TBB;unsafe"}),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "XIAOQING_MONITOR_AST_PATH": str(MONITOR),
        "XIAOQING_TEST_CONFIG": str(config_file),
        "XIAOQING_TEST_INVALID_TYPE_CONFIG": str(invalid_type_file),
        "XIAOQING_TEST_INVALID_VALUE_CONFIG": str(invalid_value_file),
        "XIAOQING_TEST_INVALID_MKL_TYPE_CONFIG": str(invalid_mkl_type_file),
        "XIAOQING_TEST_INVALID_MKL_VALUE_CONFIG": str(invalid_mkl_value_file),
        "XIAOQING_TEST_NAPCAT": str(tmp_path / "NapCatWinBootMain.exe"),
    }
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('Read-LauncherConfig', 'Start-NapCat')
}, $true)
foreach ($definition in $definitions) {
    Invoke-Expression $definition.Extent.Text
}
foreach ($invalidConfig in @(
    $env:XIAOQING_TEST_INVALID_TYPE_CONFIG,
    $env:XIAOQING_TEST_INVALID_VALUE_CONFIG,
    $env:XIAOQING_TEST_INVALID_MKL_TYPE_CONFIG,
    $env:XIAOQING_TEST_INVALID_MKL_VALUE_CONFIG
)) {
    try {
        Read-LauncherConfig -Path $invalidConfig | Out-Null
        exit 41
    } catch {
        if ($_.Exception.Message -notlike '*config.json*') { exit 42 }
    }
}
$script:CapturedArguments = $null
function Start-LogPumpedProcess {
    param($CommandArguments, $WorkingDirectory, $StandardOutputLog, $StandardErrorLog)
    $script:CapturedArguments = @($CommandArguments)
}
$launcherConfig = Read-LauncherConfig -Path $env:XIAOQING_TEST_CONFIG
if ($launcherConfig.MklThreadingLayer -ne 'TBB') { exit 43 }
$script:NapCatAccount = $launcherConfig.NapCatAccount
$script:NapCatPath = $env:XIAOQING_TEST_NAPCAT
$script:NapCatArguments = @('--mode', 'production')
$script:NapCatLog = 'stdout.log'
$script:NapCatErrorLog = 'stderr.log'
Start-NapCat
$script:CapturedArguments | ConvertTo-Json -Compress
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        str(tmp_path / "NapCatWinBootMain.exe"),
        "123456789",
        "--mode",
        "production",
    ]


def test_monitor_scopes_configured_mkl_layer_to_bot_process_tree() -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")

    environment = {**os.environ, "XIAOQING_MONITOR_AST_PATH": str(MONITOR)}
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Start-TrackedBot'
}, $true)
Invoke-Expression $definition.Extent.Text
$script:PidFile = Join-Path ([IO.Path]::GetTempPath()) 'xiaoqing-mkl-test.pid'
$script:MainScript = 'C:\repo\main.py'
$script:BotRoot = 'C:\repo'
$script:BotLog = 'C:\repo\stdout.log'
$script:BotErrorLog = 'C:\repo\stderr.log'
$script:BotArguments = @()
$script:MklThreadingLayer = 'TBB'
$script:CapturedMklThreadingLayer = $null
function Start-LogPumpedProcess {
    param($CommandArguments, $WorkingDirectory, $StandardOutputLog, $StandardErrorLog)
    $script:CapturedMklThreadingLayer = $env:MKL_THREADING_LAYER
    return [pscustomobject]@{ Id = 424242 }
}
function Write-Utf8NoBomAtomically { }
$original = [Environment]::GetEnvironmentVariable(
    'MKL_THREADING_LAYER', [EnvironmentVariableTarget]::Process)
try {
    $env:MKL_THREADING_LAYER = 'ambient-value'
    Start-TrackedBot
    if ($script:CapturedMklThreadingLayer -ne 'TBB') { exit 51 }
    if ($env:MKL_THREADING_LAYER -ne 'ambient-value') { exit 52 }
} finally {
    [Environment]::SetEnvironmentVariable(
        'MKL_THREADING_LAYER', $original, [EnvironmentVariableTarget]::Process)
}
exit 0
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr


def test_monitor_default_bot_root_resolves_in_windows_powershell(
    tmp_path: Path,
) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")

    clean_checkout = tmp_path / "clean checkout"
    scripts_directory = clean_checkout / "scripts"
    config_directory = clean_checkout / "config"
    scripts_directory.mkdir(parents=True)
    config_directory.mkdir()
    copied_monitor = scripts_directory / MONITOR.name
    shutil.copy2(MONITOR, copied_monitor)
    shutil.copy2(LOG_PUMP, scripts_directory / LOG_PUMP.name)
    (clean_checkout / "main.py").write_text("", encoding="utf-8")
    (config_directory / "config.json").write_text(
        '{"napcat_account":"","mkl_threading_layer":""}\n',
        encoding="utf-8",
    )

    result = _run_powershell(
        executable,
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(copied_monitor),
        "-NapCatPath",
        str(tmp_path / "missing-napcat.exe"),
    )

    assert result.returncode != 0
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    assert "NapCat executable not found" in output
    assert "Unable to resolve XiaoQing monitor script directory" not in output


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
    child = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)"
    )
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
    child = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)"
    )
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
    monkeypatch.setattr(log_pump.subprocess, "run", lambda *args, **kwargs: completed)

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


def test_log_pump_requests_hidden_windows_children() -> None:
    source = LOG_PUMP.read_text(encoding="utf-8")

    assert "subprocess.CREATE_NO_WINDOW" in source
    assert "subprocess.STARTUPINFO" in source
    assert "subprocess.STARTF_USESHOWWINDOW" in source
    assert "subprocess.SW_HIDE" in source


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
$definition = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Start-TrackedBot'
}, $true) | Select-Object -First 1
Invoke-Expression $definition.Extent.Text
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


def test_windows_powershell_native_argument_quoting_round_trip(tmp_path: Path) -> None:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell 5.1 is not installed")
    receiver = tmp_path / "argument receiver.py"
    output = tmp_path / "received arguments.json"
    receiver.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text("
        "json.dumps(sys.argv[2:], ensure_ascii=False), encoding='utf-8')\n",
        encoding="utf-8",
    )
    expected = [
        "",
        "plain",
        "with space",
        'quote"inside',
        "trailing slash " + chr(92),
        "中文 参数",
    ]
    environment = {
        **os.environ,
        "XIAOQING_MONITOR_AST_PATH": str(MONITOR),
        "XIAOQING_TEST_PYTHON": sys.executable,
        "XIAOQING_TEST_RECEIVER": str(receiver),
        "XIAOQING_TEST_OUTPUT": str(output),
    }
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('ConvertTo-NativeArgument', 'Join-NativeArguments')
}, $true)
foreach ($definition in $definitions) {
    Invoke-Expression $definition.Extent.Text
}
$values = @('', 'plain', 'with space', 'quote"inside', 'trailing slash \', '中文 参数')
$arguments = @($env:XIAOQING_TEST_RECEIVER, $env:XIAOQING_TEST_OUTPUT) + $values
$line = Join-NativeArguments $arguments
$process = Start-Process `
    -FilePath $env:XIAOQING_TEST_PYTHON `
    -ArgumentList $line `
    -WindowStyle Hidden `
    -PassThru
if (-not $process.WaitForExit(5000)) {
    $process.Kill()
    exit 30
}
exit $process.ExitCode
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == expected


def test_monitor_parses_in_windows_powershell_and_pwsh() -> None:
    executables = [
        executable
        for name in ("powershell.exe", "pwsh")
        if (executable := shutil.which(name)) is not None
    ]
    if not executables:
        pytest.skip("PowerShell is not installed")
    environment = {**os.environ, "XIAOQING_MONITOR_AST_PATH": str(MONITOR)}
    parser_command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:XIAOQING_MONITOR_AST_PATH,[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )

    for executable in executables:
        result = _run_powershell(
            executable,
            "-Command",
            parser_command,
            env=environment,
        )
        assert result.returncode == 0, result.stderr


def test_mutex_identity_normalizes_trailing_separators_without_damaging_roots(
    tmp_path: Path,
) -> None:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell 5.1 is not installed")
    environment = {
        **os.environ,
        "XIAOQING_MONITOR_AST_PATH": str(MONITOR),
        "XIAOQING_MUTEX_TEST_ROOT": str(tmp_path),
    }
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('Get-NormalizedDirectoryPath', 'Get-MutexName')
}, $true)
foreach ($definition in $definitions) {
    Invoke-Expression $definition.Extent.Text
}
$plain = [IO.Path]::GetFullPath($env:XIAOQING_MUTEX_TEST_ROOT)
$withSeparator = $plain + [IO.Path]::DirectorySeparatorChar
if ((Get-MutexName $plain) -ne (Get-MutexName $withSeparator)) {
    Write-Error 'trailing separator changed the mutex identity'
    exit 40
}
$driveRoot = [IO.Path]::GetPathRoot($plain)
$normalizedDriveRoot = Get-NormalizedDirectoryPath $driveRoot
if (-not $normalizedDriveRoot -or $normalizedDriveRoot -ne $driveRoot) {
    Write-Error "drive root was damaged: '$normalizedDriveRoot'"
    exit 41
}
$uncRoot = '\\server\share\'
$normalizedUncRoot = Get-NormalizedDirectoryPath $uncRoot
if (-not $normalizedUncRoot -or $normalizedUncRoot -ne [IO.Path]::GetFullPath($uncRoot)) {
    Write-Error "UNC share root was damaged: '$normalizedUncRoot'"
    exit 42
}
if ((Get-MutexName '\\server\share') -ne (Get-MutexName $uncRoot)) {
    Write-Error 'UNC share root separator changed the mutex identity'
    exit 43
}
exit 0
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["-MonitorIntervalSeconds", "0"],
        ["-InitialRestartDelaySeconds", "0"],
        ["-MaximumRestartDelaySeconds", "0"],
        ["-StableRunSeconds", "0"],
        ["-MaximumLogBytes", str(64 * 1024 - 1)],
        ["-LogBackupCount", "0"],
        ["-InitialRestartDelaySeconds", "11", "-MaximumRestartDelaySeconds", "10"],
    ],
)
def test_monitor_rejects_invalid_ranges_before_launch(arguments: list[str]) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")

    result = _run_powershell(executable, "-File", str(MONITOR), *arguments)

    assert result.returncode != 0


def test_monitor_fails_closed_when_napcat_executable_is_missing(tmp_path: Path) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")

    bot_root = tmp_path / "bot"
    scripts = bot_root / "scripts"
    scripts.mkdir(parents=True)
    config_dir = bot_root / "config"
    config_dir.mkdir()
    (bot_root / "main.py").write_text("", encoding="utf-8")
    (scripts / "run_process_with_rotating_logs.py").write_text("", encoding="utf-8")
    (config_dir / "config.json").write_text(
        json.dumps({"napcat_account": ""}),
        encoding="utf-8",
    )
    missing_napcat = tmp_path / "missing-napcat.exe"

    environment = {
        **os.environ,
        "XIAOQING_MONITOR_PATH": str(MONITOR),
        "XIAOQING_TEST_BOT_ROOT": str(bot_root),
        "XIAOQING_TEST_NAPCAT_PATH": str(missing_napcat),
    }
    # 顶层 throw 的原生错误流会随 PowerShell 宿主而变化。由同一个
    # PowerShell 进程捕获脚本异常并显式输出消息，验证真实预检分支，
    # 同时避免把宿主的错误流转发细节误当成监控脚本契约。
    probe = r"""
try {
    & $env:XIAOQING_MONITOR_PATH `
        -BotRoot $env:XIAOQING_TEST_BOT_ROOT `
        -NapCatPath $env:XIAOQING_TEST_NAPCAT_PATH
    exit 91
} catch {
    [Console]::Out.WriteLine([string]$_.Exception.Message)
    exit 17
}
"""
    result = _run_powershell(
        executable,
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        probe,
        env=environment,
    )

    assert result.returncode == 17, (
        f"unexpected PowerShell result: stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    assert "NapCat executable not found" in output
    assert "-DisableNapCat" in output


def test_monitor_explicit_disable_is_the_only_missing_napcat_bypass(tmp_path: Path) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")

    environment = {
        **os.environ,
        "XIAOQING_MONITOR_AST_PATH": str(MONITOR),
        "XIAOQING_MISSING_NAPCAT": str(tmp_path / "missing.exe"),
    }
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Test-NapCatRunning'
}, $true)
Invoke-Expression $definition.Extent.Text
$NapCatPath = $env:XIAOQING_MISSING_NAPCAT
$DisableNapCat = $true
if (-not (Test-NapCatRunning)) { exit 41 }
$DisableNapCat = $false
try {
    Test-NapCatRunning | Out-Null
    exit 42
} catch {
    if ($_.Exception.Message -notlike '*disappeared while monitoring*') { exit 43 }
}
exit 0
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr
