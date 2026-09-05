"""PowerShell monitor 参数、解析与 NapCat 启动边界。"""

from __future__ import annotations

import codecs
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.helpers.bot_monitor_test_support import (
    MONITOR,
)
from tests.helpers.bot_monitor_test_support import (
    powershell_executable as _powershell_executable,
)
from tests.helpers.bot_monitor_test_support import (
    run_powershell as _run_powershell,
)


def test_windows_powershell_native_argument_quoting_round_trip(tmp_path: Path) -> None:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell 5.1 is not installed")
    receiver = tmp_path / "argument receiver.py"
    output   = tmp_path / "received arguments.json"
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
    # Windows PowerShell 5.1 会把无 BOM 的脚本按系统代码页解释；监控脚本含有
    # 中文维护注释，因此 BOM 是生产解析契约，不只是编辑器格式偏好。
    assert MONITOR.read_bytes().startswith(codecs.BOM_UTF8)

    executables = [
        executable
        for name in ("powershell.exe", "pwsh")
        if (executable := shutil.which(name)) is not None
    ]
    if not executables:
        pytest.skip("PowerShell is not installed")
    environment    = {**os.environ, "XIAOQING_MONITOR_AST_PATH": str(MONITOR)}
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


def test_elevated_stop_preserves_scoped_paths_and_reports_child_failure() -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")
    environment = {**os.environ, "XIAOQING_MONITOR_AST_PATH": str(MONITOR)}
    probe       = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @(
            'ConvertTo-NativeArgument',
            'Join-NativeArguments',
            'Invoke-ElevatedStop'
        )
}, $true)
foreach ($definition in $definitions) {
    Invoke-Expression $definition.Extent.Text
}

$script:MonitorScript = 'C:\repo with spaces\scripts\run-bot-monitor.ps1'
$script:BotRoot = 'C:\repo with spaces'
$script:NapCatPath = 'C:\NapCat Shell\NapCatWinBootMain.exe'
$script:MockExitCode = 0
$script:Disposed = $false
function Test-Path {
    param($LiteralPath, $PathType)
    # 本测试隔离验证 UAC 子进程参数与退出码传播；PowerShell 宿主的真实路径
    # 属于 Windows 运行环境门禁，在 Ubuntu CI 的 pwsh 中没有 .exe 后缀。
    return $true
}
function Start-Process {
    param(
        $FilePath,
        $ArgumentList,
        $WorkingDirectory,
        $WindowStyle,
        $Verb,
        [switch]$Wait,
        [switch]$PassThru,
        $ErrorAction
    )
    $script:ObservedFilePath = $FilePath
    $script:ObservedArguments = $ArgumentList
    $script:ObservedWorkingDirectory = $WorkingDirectory
    $script:ObservedVerb = $Verb
    $result = [pscustomobject]@{ ExitCode = $script:MockExitCode }
    $result | Add-Member -MemberType ScriptMethod -Name Dispose -Value {
        $script:Disposed = $true
    }
    return $result
}

Invoke-ElevatedStop
if ($script:ObservedVerb -ne 'RunAs') { exit 71 }
if ($script:ObservedWorkingDirectory -ne $script:BotRoot) { exit 72 }
foreach ($fragment in @(
    '-Stop',
    '-ElevationAttempted',
    $script:MonitorScript,
    $script:BotRoot,
    $script:NapCatPath
)) {
    if ($script:ObservedArguments.IndexOf(
        $fragment,
        [StringComparison]::OrdinalIgnoreCase
    ) -lt 0) { exit 73 }
}
if (-not $script:Disposed) { exit 74 }

$script:MockExitCode = 17
$script:Disposed = $false
try {
    Invoke-ElevatedStop
    exit 75
} catch {
    if ($_.Exception.Message -notlike '*elevated stop exited with code 17*') { exit 76 }
}
if (-not $script:Disposed) { exit 77 }
exit 0
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

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
    scripts  = bot_root / "scripts"
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


def test_napcat_detection_does_not_hide_cim_or_identity_failures(tmp_path: Path) -> None:
    executable = _powershell_executable()
    if executable is None:
        pytest.skip("PowerShell is not installed")
    napcat_path = tmp_path / "NapCatWinBootMain.exe"
    napcat_path.write_bytes(b"test executable marker")
    environment = {
        **os.environ,
        "XIAOQING_MONITOR_AST_PATH": str(MONITOR),
        "XIAOQING_TEST_NAPCAT_PATH": str(napcat_path),
    }
    probe = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:XIAOQING_MONITOR_AST_PATH, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('Test-CommandLineContains', 'Test-NapCatRunning')
}, $true)
foreach ($definition in $definitions) {
    Invoke-Expression $definition.Extent.Text
}
$script:DisableNapCat = $false
$script:NapCatPath = $env:XIAOQING_TEST_NAPCAT_PATH

function Get-CimInstance { throw 'simulated NapCat CIM failure' }
try {
    Test-NapCatRunning | Out-Null
    exit 61
} catch {
    if ($_.Exception.Message -notlike '*simulated NapCat CIM failure*') { exit 62 }
}

function Get-CimInstance { return [pscustomobject]@{ CommandLine = $null } }
try {
    Test-NapCatRunning | Out-Null
    exit 63
} catch {
    if ($_.Exception.Message -notlike '*Unable to verify command line*') { exit 64 }
}

function Get-CimInstance {
    return [pscustomobject]@{ CommandLine = '"' + $script:NapCatPath + '" 123456789' }
}
if (-not (Test-NapCatRunning)) { exit 65 }
exit 0
"""

    result = _run_powershell(executable, "-Command", probe, env=environment)

    assert result.returncode == 0, result.stderr
