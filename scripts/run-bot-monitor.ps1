<#
.SYNOPSIS
    Single-instance, fail-safe Windows monitor for XiaoQing and NapCat.

The script never terminates a process it did not create.  It recognises the
bot only through this repository's PID file plus the absolute main.py path,
uses a named mutex to prevent competing monitors, rotates redirected logs,
and backs off after crashes.
#>

[CmdletBinding()]
param(
    [string]$BotRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CondaPath = (Join-Path $env:USERPROFILE "miniconda3\Scripts\conda.exe"),
    [string]$NapCatPath = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "NapCat.Shell\NapCatWinBootMain.exe"),
    [int]$MonitorIntervalSeconds = 10,
    [int]$InitialRestartDelaySeconds = 10,
    [int]$MaximumRestartDelaySeconds = 300,
    [int]$StableRunSeconds = 300,
    [long]$MaximumLogBytes = 10MB,
    [int]$LogBackupCount = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BotRoot = [IO.Path]::GetFullPath($BotRoot)
$MainScript = Join-Path $BotRoot "main.py"
$LogDirectory = Join-Path $BotRoot "logs"
$PidFile = Join-Path $LogDirectory "xiaoqing-bot.pid.json"
$BotLog = Join-Path $LogDirectory "bot-monitor.log"
$BotErrorLog = Join-Path $LogDirectory "bot-monitor-error.log"
$NapCatLog = Join-Path $LogDirectory "napcat-monitor.log"
$NapCatErrorLog = Join-Path $LogDirectory "napcat-monitor-error.log"

if (-not (Test-Path -LiteralPath $MainScript -PathType Leaf)) {
    throw "XiaoQing entry point not found: $MainScript"
}
if (-not (Test-Path -LiteralPath $CondaPath -PathType Leaf)) {
    throw "Conda executable not found: $CondaPath"
}
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null

function Get-MutexName {
    param([string]$Path)

    $bytes = [Text.Encoding]::UTF8.GetBytes($Path.ToLowerInvariant())
    $hash = [Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    $suffix = ([BitConverter]::ToString($hash)).Replace("-", "").Substring(0, 16)
    return "Global\XiaoQing.BotMonitor.$suffix"
}

function Rotate-Log {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if ((Get-Item -LiteralPath $Path).Length -lt $MaximumLogBytes) {
        return
    }

    for ($index = $LogBackupCount; $index -ge 1; $index--) {
        $older = "$Path.$index"
        if ($index -eq $LogBackupCount) {
            Remove-Item -LiteralPath $older -Force -ErrorAction SilentlyContinue
        } elseif (Test-Path -LiteralPath $older) {
            Move-Item -LiteralPath $older -Destination "$Path.$($index + 1)" -Force
        }
    }
    Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
}

function Get-ProcessByIdSafely {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $null
    }
    try {
        return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId"
    } catch {
        return $null
    }
}

function Test-CommandLineContains {
    param(
        [AllowNull()][string]$CommandLine,
        [string]$ExpectedPath
    )

    return $null -ne $CommandLine -and
        $CommandLine.IndexOf($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Get-TrackedBotProcess {
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        return $null
    }
    try {
        $saved = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
        $process = Get-ProcessByIdSafely -ProcessId ([int]$saved.process_id)
        if ($null -ne $process -and (Test-CommandLineContains $process.CommandLine $MainScript)) {
            return $process
        }
    } catch {
        # A partially written/stale PID file must not block recovery.
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    return $null
}

function Start-TrackedBot {
    Rotate-Log $BotLog
    Rotate-Log $BotErrorLog
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $CondaPath `
        -ArgumentList @("run", "-n", "base", "--no-capture-output", "python", "`"$MainScript`"") `
        -WorkingDirectory $BotRoot `
        -RedirectStandardOutput $BotLog `
        -RedirectStandardError $BotErrorLog `
        -PassThru
    [pscustomobject]@{
        process_id = $process.Id
        main_script = $MainScript
        started_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding utf8NoBOM
    return $process
}

function Test-NapCatRunning {
    if (-not (Test-Path -LiteralPath $NapCatPath -PathType Leaf)) {
        return $true
    }
    $expected = [IO.Path]::GetFullPath($NapCatPath)
    return @(
        Get-CimInstance -ClassName Win32_Process -Filter "Name = 'NapCatWinBootMain.exe'" -ErrorAction SilentlyContinue |
            Where-Object { Test-CommandLineContains $_.CommandLine $expected }
    ).Count -gt 0
}

function Start-NapCat {
    Rotate-Log $NapCatLog
    Rotate-Log $NapCatErrorLog
    Start-Process `
        -FilePath $NapCatPath `
        -ArgumentList "1000000001" `
        -WorkingDirectory (Split-Path -Parent $NapCatPath) `
        -RedirectStandardOutput $NapCatLog `
        -RedirectStandardError $NapCatErrorLog | Out-Null
}

[bool]$createdNew = $false
$mutex = [Threading.Mutex]::new($true, (Get-MutexName $BotRoot), [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

try {
    $restartDelaySeconds = $InitialRestartDelaySeconds
    $trackedProcess = Get-TrackedBotProcess
    $startedAt = if ($trackedProcess) { Get-Date } else { $null }
    $wasRunning = $null -ne $trackedProcess

    while ($true) {
        if (-not (Test-NapCatRunning)) {
            Start-NapCat
        }

        $trackedProcess = Get-TrackedBotProcess
        if ($null -eq $trackedProcess) {
            if ($wasRunning) {
                Start-Sleep -Seconds $restartDelaySeconds
                $restartDelaySeconds = [Math]::Min($restartDelaySeconds * 2, $MaximumRestartDelaySeconds)
            }
            $trackedProcess = Start-TrackedBot
            $startedAt = Get-Date
            $wasRunning = $true
        } elseif ($null -ne $startedAt -and ((Get-Date) - $startedAt).TotalSeconds -ge $StableRunSeconds) {
            $restartDelaySeconds = $InitialRestartDelaySeconds
        }

        Start-Sleep -Seconds $MonitorIntervalSeconds
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
