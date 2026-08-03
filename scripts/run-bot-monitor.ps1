<#
.SYNOPSIS
    Single-instance, fail-safe Windows monitor for XiaoQing and NapCat.

The script never terminates a process it did not create. It recognises the
bot only through this repository's PID file plus the absolute helper/main.py
paths, uses a named mutex to prevent competing monitors, and backs off after
crashes. A Python helper owns stdout/stderr and rotates each active log after
closing its Windows file handle.
#>

[CmdletBinding()]
param(
    [string]$BotRoot = (Split-Path -Parent $PSScriptRoot),
    [string[]]$BotArguments = @(),
    [string]$NapCatPath = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "NapCat.Shell\NapCatWinBootMain.exe"),
    [switch]$DisableNapCat,
    [AllowEmptyString()]
    [string]$NapCatAccount = $env:XIAOQING_NAPCAT_ACCOUNT,
    [string[]]$NapCatArguments = @(),
    [ValidateRange(1, 3600)]
    [int]$MonitorIntervalSeconds = 10,
    [ValidateRange(1, 86400)]
    [int]$InitialRestartDelaySeconds = 10,
    [ValidateRange(1, 86400)]
    [int]$MaximumRestartDelaySeconds = 300,
    [ValidateRange(1, 604800)]
    [int]$StableRunSeconds = 300,
    [ValidateRange(65536, 10737418240)]
    [long]$MaximumLogBytes = 10MB,
    [ValidateRange(1, 100)]
    [int]$LogBackupCount = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NormalizedDirectoryPath {
    param([string]$Path)

    $fullPath = [IO.Path]::GetFullPath($Path)
    $pathRoot = [IO.Path]::GetPathRoot($fullPath)
    $separators = [char[]]@(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $trimmedFullPath = $fullPath.TrimEnd($separators)
    $trimmedPathRoot = $pathRoot.TrimEnd($separators)
    if ([string]::Equals(
        $trimmedFullPath,
        $trimmedPathRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        return $trimmedFullPath + [IO.Path]::DirectorySeparatorChar
    }
    return $trimmedFullPath
}

if ($MaximumRestartDelaySeconds -lt $InitialRestartDelaySeconds) {
    throw "MaximumRestartDelaySeconds must be greater than or equal to InitialRestartDelaySeconds"
}
if (-not $DisableNapCat -and $NapCatAccount -and $NapCatAccount -notmatch '^\d{5,20}$') {
    throw "NapCatAccount must contain 5 to 20 decimal digits"
}
foreach ($argument in @($BotArguments) + @($NapCatArguments)) {
    if ($null -eq $argument) {
        throw "BotArguments and NapCatArguments cannot contain null values"
    }
}

$BotRoot = Get-NormalizedDirectoryPath $BotRoot
$NapCatPath = [IO.Path]::GetFullPath($NapCatPath)
$MainScript = Join-Path $BotRoot "main.py"
$LogPumpScript = Join-Path $BotRoot "scripts\run_process_with_rotating_logs.py"
$LogDirectory = Join-Path $BotRoot "logs"
$PidFile = Join-Path $LogDirectory "xiaoqing-bot.pid.json"
$BotLog = Join-Path $LogDirectory "bot-monitor.log"
$BotErrorLog = Join-Path $LogDirectory "bot-monitor-error.log"
$NapCatLog = Join-Path $LogDirectory "napcat-monitor.log"
$NapCatErrorLog = Join-Path $LogDirectory "napcat-monitor-error.log"

if (-not (Test-Path -LiteralPath $MainScript -PathType Leaf)) {
    throw "XiaoQing entry point not found: $MainScript"
}
if (-not (Test-Path -LiteralPath $LogPumpScript -PathType Leaf)) {
    throw "Rotating log helper not found: $LogPumpScript"
}
if ($null -eq (Get-Command python -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "Python command not found in PATH"
}
if (-not $DisableNapCat -and -not (Test-Path -LiteralPath $NapCatPath -PathType Leaf)) {
    throw "NapCat executable not found: $NapCatPath (use -DisableNapCat only when an external adapter is intentional)"
}
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null

function Get-MutexName {
    param([string]$Path)

    $normalizedPath = Get-NormalizedDirectoryPath $Path
    $bytes = [Text.Encoding]::UTF8.GetBytes($normalizedPath.ToLowerInvariant())
    $hashAlgorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $hashAlgorithm.ComputeHash($bytes)
    } finally {
        $hashAlgorithm.Dispose()
    }
    $suffix = ([BitConverter]::ToString($hash)).Replace("-", "").Substring(0, 16)
    return "Global\XiaoQing.BotMonitor.$suffix"
}

function Write-Utf8NoBomAtomically {
    param(
        [string]$Path,
        [string]$Content
    )

    $absolutePath = [IO.Path]::GetFullPath($Path)
    $directory = [IO.Path]::GetDirectoryName($absolutePath)
    $leaf = [IO.Path]::GetFileName($absolutePath)
    $temporaryPath = Join-Path $directory ".$leaf.$PID.$([Guid]::NewGuid().ToString('N')).tmp"
    $encoding = [Text.UTF8Encoding]::new($false)
    try {
        [IO.File]::WriteAllText($temporaryPath, $Content, $encoding)
        if ([IO.File]::Exists($absolutePath)) {
            [IO.File]::Replace($temporaryPath, $absolutePath, $null)
        } else {
            [IO.File]::Move($temporaryPath, $absolutePath)
        }
    } finally {
        if ([IO.File]::Exists($temporaryPath)) {
            [IO.File]::Delete($temporaryPath)
        }
    }
}

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    # Follow CommandLineToArgvW/CRT quoting rules so paths, quotes and trailing
    # backslashes survive PowerShell 5.1's string-only Start-Process API.
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]'\') {
            $backslashes++
            continue
        }
        if ($character -eq [char]'"') {
            [void]$builder.Append([char]'\', (2 * $backslashes) + 1)
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append([char]'\', $backslashes)
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append([char]'\', 2 * $backslashes)
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Join-NativeArguments {
    param([string[]]$Values)

    return (($Values | ForEach-Object { ConvertTo-NativeArgument $_ }) -join " ")
}

function Start-LogPumpedProcess {
    param(
        [string[]]$CommandArguments,
        [string]$WorkingDirectory,
        [string]$StandardOutputLog,
        [string]$StandardErrorLog
    )

    $pumpArguments = @(
        $LogPumpScript,
        "--stdout-log", $StandardOutputLog,
        "--stderr-log", $StandardErrorLog,
        "--max-bytes", $MaximumLogBytes.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--backup-count", $LogBackupCount.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--cwd", $WorkingDirectory,
        "--"
    ) + $CommandArguments

    return Start-Process `
        -FilePath "python" `
        -ArgumentList (Join-NativeArguments $pumpArguments) `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -PassThru
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
        if ($null -ne $process -and
            (Test-CommandLineContains $process.CommandLine $LogPumpScript) -and
            (Test-CommandLineContains $process.CommandLine $MainScript)) {
            return $process
        }
    } catch {
        # A partially written/stale PID file must not block recovery.
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    return $null
}

function Stop-OwnedProcessTree {
    param([Diagnostics.Process]$Process)

    try {
        if ($Process.HasExited) {
            return
        }
    } catch {
        # The PID is still owned by this start attempt. A failed status query
        # must not silently turn a live helper and its descendants into orphans.
    }

    $taskKillPath = Join-Path ([Environment]::SystemDirectory) "taskkill.exe"
    $treeCleanupError = $null
    $killer = $null
    try {
        if (-not (Test-Path -LiteralPath $taskKillPath -PathType Leaf)) {
            throw "Windows taskkill executable not found: $taskKillPath"
        }
        $killer = Start-Process `
            -FilePath $taskKillPath `
            -ArgumentList @("/PID", $Process.Id.ToString(), "/T", "/F") `
            -WindowStyle Hidden `
            -PassThru
        if (-not $killer.WaitForExit(5000)) {
            $killer.Kill()
            [void]$killer.WaitForExit(1000)
            throw "taskkill timed out for owned process tree $($Process.Id)"
        }
        if ($killer.ExitCode -ne 0) {
            throw "taskkill failed for owned process tree $($Process.Id) with exit code $($killer.ExitCode)"
        }
    } catch {
        $treeCleanupError = $_
    } finally {
        if ($null -ne $killer) {
            try {
                if (-not $killer.HasExited) {
                    $killer.Kill()
                }
            } catch {
                # Preserve the primary tree-cleanup result.
            }
            $killer.Dispose()
        }
    }

    try {
        if (-not $Process.HasExited) {
            $Process.Kill()
        }
        if (-not $Process.WaitForExit(5000)) {
            throw "owned process $($Process.Id) did not exit within 5 seconds"
        }
    } catch {
        if ($null -ne $treeCleanupError) {
            throw "tree cleanup failed ($($treeCleanupError.Exception.Message)); direct cleanup also failed ($($_.Exception.Message))"
        }
        throw
    }
    if ($null -ne $treeCleanupError) {
        throw $treeCleanupError
    }
}

function Start-TrackedBot {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $botCommand = @(
        "python",
        $MainScript
    ) + $BotArguments
    $process = Start-LogPumpedProcess `
        -CommandArguments $botCommand `
        -WorkingDirectory $BotRoot `
        -StandardOutputLog $BotLog `
        -StandardErrorLog $BotErrorLog
    try {
        $record = [pscustomobject]@{
            process_id = $process.Id
            main_script = $MainScript
            log_pump = $LogPumpScript
            started_at = (Get-Date).ToUniversalTime().ToString("o")
        } | ConvertTo-Json -Compress
        Write-Utf8NoBomAtomically -Path $PidFile -Content "$record`n"
    } catch {
        $pidCommitError = $_
        try {
            Stop-OwnedProcessTree -Process $process
        } catch {
            throw "PID commit failed ($($pidCommitError.Exception.Message)); owned process cleanup failed ($($_.Exception.Message))"
        }
        throw $pidCommitError
    }
    return $process
}

function Test-NapCatRunning {
    if ($DisableNapCat) {
        return $true
    }
    if (-not (Test-Path -LiteralPath $NapCatPath -PathType Leaf)) {
        throw "NapCat executable disappeared while monitoring: $NapCatPath"
    }
    $expected = [IO.Path]::GetFullPath($NapCatPath)
    $expectedName = [IO.Path]::GetFileName($expected).Replace("'", "''")
    return @(
        Get-CimInstance -ClassName Win32_Process -Filter "Name = '$expectedName'" -ErrorAction SilentlyContinue |
            Where-Object { Test-CommandLineContains $_.CommandLine $expected }
    ).Count -gt 0
}

function Start-NapCat {
    $napCatCommand = @($NapCatPath)
    if ($NapCatAccount) {
        $napCatCommand += $NapCatAccount
    }
    $napCatCommand += $NapCatArguments
    Start-LogPumpedProcess `
        -CommandArguments $napCatCommand `
        -WorkingDirectory (Split-Path -Parent $NapCatPath) `
        -StandardOutputLog $NapCatLog `
        -StandardErrorLog $NapCatErrorLog | Out-Null
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
