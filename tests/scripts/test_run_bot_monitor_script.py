"""VBS 启停入口和 Bot monitor 基础配置测试。"""

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

from tests.helpers.bot_monitor_test_support import (
    LOG_PUMP,
    MONITOR,
    ROOT,
    STOP_LAUNCHER,
)
from tests.helpers.bot_monitor_test_support import (
    assert_process_not_running as _assert_process_not_running,
)
from tests.helpers.bot_monitor_test_support import (
    powershell_executable as _powershell_executable,
)
from tests.helpers.bot_monitor_test_support import (
    run_powershell as _run_powershell,
)


def test_vbs_launcher_delegates_to_the_monitor() -> None:
    source = (ROOT / "scripts" / "run-bot.vbs").read_text(encoding="utf-8")

    assert 'fso.BuildPath(scriptDir, "run-bot-monitor.ps1")' in source
    assert "-WindowStyle Hidden" in source
    assert "tasklist" not in source.lower()
    assert "wmic" not in source.lower()


def test_vbs_stop_launcher_delegates_to_the_monitor_stop_mode() -> None:
    source = STOP_LAUNCHER.read_text(encoding="utf-8")

    assert 'fso.BuildPath(scriptDir, "run-bot-monitor.ps1")' in source
    assert '""" -Stop"' in source
    assert "ws.Exec(command)" in source
    assert "process.Status = 0" in source
    assert "process.StdErr.ReadAll" in source
    assert "WScript.Echo" in source
    assert "taskkill" not in source.lower()
    assert "wmic" not in source.lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows Script Host is Windows-specific")
def test_vbs_launcher_parses_and_reports_a_missing_monitor(tmp_path: Path) -> None:
    cscript = shutil.which("cscript.exe")
    if cscript is None:
        pytest.skip("Windows Script Host is not installed")
    isolated_launcher = tmp_path / "run-bot.vbs"
    shutil.copy2(ROOT / "scripts" / "run-bot.vbs", isolated_launcher)

    result = subprocess.run(
        [cscript, "//NoLogo", str(isolated_launcher)],
        check=False,
        capture_output=True,
        text=True,
        encoding=locale.getencoding(),
        errors="replace",
        timeout=10,
    )

    assert result.returncode == 1
    assert "XiaoQing monitor script not found" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows Script Host is Windows-specific")
def test_vbs_stop_launcher_parses_and_reports_a_missing_monitor(tmp_path: Path) -> None:
    cscript = shutil.which("cscript.exe")
    if cscript is None:
        pytest.skip("Windows Script Host is not installed")
    isolated_launcher = tmp_path / "stop-bot.vbs"
    shutil.copy2(STOP_LAUNCHER, isolated_launcher)

    result = subprocess.run(
        [cscript, "//NoLogo", str(isolated_launcher)],
        check=False,
        capture_output=True,
        text=True,
        encoding=locale.getencoding(),
        errors="replace",
        timeout=10,
    )

    assert result.returncode == 1
    assert "XiaoQing monitor script not found" in result.stdout


def test_monitor_uses_scoped_identity_configured_launch_and_log_pump() -> None:
    source = MONITOR.read_text(encoding="utf-8")

    assert "XiaoQing.BotMonitor" in source
    assert "xiaoqing-bot.pid.json" in source
    assert "xiaoqing-monitor.pid.json" in source
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


def test_monitor_stop_mode_uses_exact_scoped_process_identities() -> None:
    source = MONITOR.read_text(encoding="utf-8")

    assert "[switch]$Stop" in source
    assert "Stop-XiaoQingService" in source
    assert "Get-TrackedMonitorProcess" in source
    assert "Test-ProcessMatchesCommandIdentity" in source
    assert "Stop-VerifiedCommandProcess" in source
    assert "@($LogPumpScript, $MainScript)" in source
    assert "@($LogPumpScript, $NapCatPath)" in source
    assert "ProcessNames @([IO.Path]::GetFileName($NapCatPath))" in source
    assert "WaitOne(30000)" in source
    assert 'Get-Process -Name "python"' not in source
    assert "taskkill.exe /IM" not in source


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
    invalid_value_file.write_text(
        json.dumps({"napcat_account": "not-an-account"}), encoding="utf-8"
    )
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
function Write-ProcessIdFile { param($Path, $ProcessId) }
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


@pytest.mark.skipif(os.name != "nt", reason="Windows process trees are required")
def test_stop_mode_ends_only_the_scoped_monitor_tree_and_is_idempotent(
    tmp_path: Path,
) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")
    cscript = shutil.which("cscript.exe")
    if cscript is None:
        pytest.skip("Windows Script Host is not installed")

    bot_root = tmp_path / "isolated-bot"
    scripts_directory = bot_root / "scripts"
    config_directory = bot_root / "config"
    scripts_directory.mkdir(parents=True)
    config_directory.mkdir()
    copied_monitor = scripts_directory / MONITOR.name
    shutil.copy2(MONITOR, copied_monitor)
    copied_stop_launcher = scripts_directory / STOP_LAUNCHER.name
    shutil.copy2(STOP_LAUNCHER, copied_stop_launcher)
    shutil.copy2(LOG_PUMP, scripts_directory / LOG_PUMP.name)
    (config_directory / "config.json").write_text(
        json.dumps({"napcat_account": "", "mkl_threading_layer": ""}),
        encoding="utf-8",
    )
    child_marker = bot_root / "bot-child.pid"
    (bot_root / "main.py").write_text(
        "import os, pathlib, time\n"
        f"marker = pathlib.Path({str(child_marker)!r})\n"
        "pending = marker.with_name(marker.name + '.tmp')\n"
        "pending.write_text(str(os.getpid()), encoding='utf-8')\n"
        "os.replace(pending, marker)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    monitor = subprocess.Popen(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(copied_monitor),
            "-BotRoot",
            str(bot_root),
            "-DisableNapCat",
            "-MonitorIntervalSeconds",
            "1",
            "-InitialRestartDelaySeconds",
            "1",
            "-MaximumRestartDelaySeconds",
            "1",
            "-StableRunSeconds",
            "1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owned_process_ids = {monitor.pid}
    try:
        monitor_pid_file = bot_root / "logs" / "xiaoqing-monitor.pid.json"
        bot_pid_file = bot_root / "logs" / "xiaoqing-bot.pid.json"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if monitor.poll() is not None:
                stdout, stderr = monitor.communicate(timeout=1)
                pytest.fail(
                    f"isolated monitor exited before startup: stdout={stdout!r}, stderr={stderr!r}"
                )
            if monitor_pid_file.is_file() and bot_pid_file.is_file() and child_marker.is_file():
                break
            time.sleep(0.05)
        else:
            pytest.fail("isolated monitor did not publish all PID markers")

        monitor_record = json.loads(monitor_pid_file.read_text(encoding="utf-8"))
        bot_record = json.loads(bot_pid_file.read_text(encoding="utf-8"))
        child_process_id = int(child_marker.read_text(encoding="utf-8"))
        assert monitor_record == {"process_id": monitor.pid}
        owned_process_ids.update({int(bot_record["process_id"]), child_process_id})

        stop_arguments = (
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(copied_monitor),
            "-BotRoot",
            str(bot_root),
            "-Stop",
        )
        result = subprocess.run(
            [cscript, "//NoLogo", str(copied_stop_launcher)],
            check=False,
            capture_output=True,
            text=True,
            encoding=locale.getencoding(),
            errors="replace",
            timeout=40,
        )

        assert result.returncode == 0, (
            f"stop failed: stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        assert "have stopped" in result.stdout
        monitor.wait(timeout=10)
        for process_id in owned_process_ids:
            _assert_process_not_running(process_id)
        assert sentinel.poll() is None
        assert not monitor_pid_file.exists()
        assert not bot_pid_file.exists()

        second_result = _run_powershell(executable, *stop_arguments, timeout=40)
        assert second_result.returncode == 0, second_result.stderr
    finally:
        _run_powershell(
            executable,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(copied_monitor),
            "-BotRoot",
            str(bot_root),
            "-Stop",
            timeout=40,
        )
        if monitor.poll() is None:
            taskkill = (
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
            )
            subprocess.run(
                [str(taskkill), "/PID", str(monitor.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        if sentinel.poll() is None:
            sentinel.terminate()
            try:
                sentinel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sentinel.kill()
                sentinel.wait(timeout=5)
