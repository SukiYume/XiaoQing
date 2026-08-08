<#
.SYNOPSIS
    在 Windows 上启动、看护或停止 XiaoQing 与本机 NapCat 进程。

监控器使用仓库级互斥量和 PID 文件保证单实例，通过有上限的退避重启异常退出
的子进程，并把 stdout/stderr 轮转交给 Python 日志泵。NapCat QQ 账号和可选
Bot 进程设置来自 config/config.json；-Stop 模式通过相同的进程身份和互斥量
安全收口本仓库的进程树。脚本不内置生产环境专属值。
#>

[CmdletBinding()]
param(
    [AllowEmptyString()]
    [string]$BotRoot = "",
    [AllowEmptyString()]
    [string]$NapCatPath = "",
    [switch]$Stop,
    [switch]$DisableNapCat,
    [string[]]$BotArguments = @(),
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

# ---------- 配置读取与路径规范化 ----------

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

function Read-LauncherConfig {
    param([string]$Path)

    try {
        # Windows PowerShell 5.1 对无 BOM 文件的默认编码不是 UTF-8；这里显式
        # 指定编码，避免配置中其他中文字段被错误解码后连带破坏 JSON 解析。
        $config = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Unable to read XiaoQing config: $Path ($($_.Exception.Message))"
    }
    if ($config -isnot [Management.Automation.PSCustomObject]) {
        throw "XiaoQing config root must be a JSON object: $Path"
    }

    $account = ""
    $accountProperty = $config.PSObject.Properties["napcat_account"]
    if ($null -ne $accountProperty -and $null -ne $accountProperty.Value) {
        if ($accountProperty.Value -isnot [string]) {
            throw "config.json napcat_account must be a string"
        }
        $account = $accountProperty.Value.Trim()
    }
    if ($account -and $account -notmatch '^\d{5,20}$') {
        throw "config.json napcat_account must contain 5 to 20 decimal digits"
    }

    $mklThreadingLayer = ""
    $mklProperty = $config.PSObject.Properties["mkl_threading_layer"]
    if ($null -ne $mklProperty -and $null -ne $mklProperty.Value) {
        if ($mklProperty.Value -isnot [string]) {
            throw "config.json mkl_threading_layer must be a string"
        }
        $mklThreadingLayer = $mklProperty.Value.Trim()
    }
    if ($mklThreadingLayer -and $mklThreadingLayer -notmatch '^[A-Za-z0-9._-]{1,64}$') {
        throw "config.json mkl_threading_layer must be a simple token up to 64 characters"
    }

    return [pscustomobject]@{
        NapCatAccount = $account
        MklThreadingLayer = $mklThreadingLayer
    }
}

# ---------- 部署路径解析与启动前门禁 ----------

# 所有部署路径在这里集中解析。默认值必须等参数绑定完成后再计算，因为 Windows
# PowerShell 5.1 在 param() 默认表达式中可能把 $PSScriptRoot 留空。
$ScriptDirectory = $PSScriptRoot
$MonitorScript = $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($ScriptDirectory)) {
    if ([string]::IsNullOrWhiteSpace($MonitorScript)) {
        throw "Unable to resolve XiaoQing monitor script directory"
    }
    $ScriptDirectory = Split-Path -Parent $MonitorScript
}
if ([string]::IsNullOrWhiteSpace($MonitorScript)) {
    $MonitorScript = Join-Path $ScriptDirectory "run-bot-monitor.ps1"
}
$MonitorScript = [IO.Path]::GetFullPath($MonitorScript)
if ([string]::IsNullOrWhiteSpace($BotRoot)) {
    $BotRoot = Split-Path -Parent $ScriptDirectory
}
$BotRoot = Get-NormalizedDirectoryPath $BotRoot
if ([string]::IsNullOrWhiteSpace($NapCatPath)) {
    $NapCatPath = Join-Path `
        (Split-Path -Parent $BotRoot) `
        "NapCat.Shell\NapCatWinBootMain.exe"
}
$NapCatPath = [IO.Path]::GetFullPath($NapCatPath)

if ($MaximumRestartDelaySeconds -lt $InitialRestartDelaySeconds) {
    throw "MaximumRestartDelaySeconds must be greater than or equal to InitialRestartDelaySeconds"
}
foreach ($argument in @($BotArguments) + @($NapCatArguments)) {
    if ($null -eq $argument) {
        throw "BotArguments and NapCatArguments cannot contain null values"
    }
}

$MainScript = Join-Path $BotRoot "main.py"
$LogPumpScript = Join-Path $BotRoot "scripts\run_process_with_rotating_logs.py"
$ConfigFile = Join-Path $BotRoot "config\config.json"
$LogDirectory = Join-Path $BotRoot "logs"
$PidFile = Join-Path $LogDirectory "xiaoqing-bot.pid.json"
$MonitorPidFile = Join-Path $LogDirectory "xiaoqing-monitor.pid.json"
$BotLog = Join-Path $LogDirectory "bot-monitor.log"
$BotErrorLog = Join-Path $LogDirectory "bot-monitor-error.log"
$NapCatLog = Join-Path $LogDirectory "napcat-monitor.log"
$NapCatErrorLog = Join-Path $LogDirectory "napcat-monitor-error.log"

$NapCatAccount = ""
$MklThreadingLayer = ""
if (-not $Stop) {
    if (-not (Test-Path -LiteralPath $MainScript -PathType Leaf)) {
        throw "XiaoQing entry point not found: $MainScript"
    }
    if (-not (Test-Path -LiteralPath $LogPumpScript -PathType Leaf)) {
        throw "Rotating log helper not found: $LogPumpScript"
    }
    if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
        throw "XiaoQing config not found: $ConfigFile"
    }
    if ($null -eq (Get-Command python -CommandType Application -ErrorAction SilentlyContinue)) {
        throw "Python command not found in PATH"
    }
    if (-not $DisableNapCat -and -not (Test-Path -LiteralPath $NapCatPath -PathType Leaf)) {
        throw "NapCat executable not found: $NapCatPath (use -DisableNapCat only when an external adapter is intentional)"
    }
    $LauncherConfig = Read-LauncherConfig -Path $ConfigFile
    $NapCatAccount = $LauncherConfig.NapCatAccount
    $MklThreadingLayer = $LauncherConfig.MklThreadingLayer
    New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
}

# ---------- 单实例标识与原子状态文件 ----------

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

function Write-ProcessIdFile {
    param(
        [string]$Path,
        [int]$ProcessId
    )

    $record = [pscustomobject]@{
        process_id = $ProcessId
    } | ConvertTo-Json -Compress
    Write-Utf8NoBomAtomically -Path $Path -Content "$record`n"
}

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    # 遵循 CommandLineToArgvW/CRT 引号规则，使路径、引号和末尾反斜杠经过
    # PowerShell 5.1 只能接收字符串的 Start-Process API 后仍能原样到达子进程。
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

# ---------- 日志泵与原生进程参数 ----------

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

function Test-CommandLineContains {
    param(
        [AllowNull()][string]$CommandLine,
        [string]$ExpectedPath
    )

    return $null -ne $CommandLine -and
        $CommandLine.IndexOf($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

# ---------- 已登记进程的识别与回收 ----------

function Get-TrackedProcessFromFile {
    param(
        [string]$Path,
        [string]$Description,
        [string[]]$ExpectedPaths
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }

    # 文件系统读取失败不是“旧 PID”，不能吞掉后继续操作未验证的进程身份。
    try {
        $savedText = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    } catch {
        throw "Unable to read tracked $Description PID file: $Path ($($_.Exception.Message))"
    }

    # 只有畸形/中断写入的 PID 内容可以按陈旧状态恢复。严格拒绝字符串和小数，
    # 避免 PowerShell 的宽松强制转换把错误值映射到另一个真实进程。
    try {
        $saved = $savedText | ConvertFrom-Json
        $processIdProperty = $saved.PSObject.Properties["process_id"]
        if ($null -eq $processIdProperty -or
            ($processIdProperty.Value -isnot [int] -and
                $processIdProperty.Value -isnot [long])) {
            throw "Tracked process ID must be an integer"
        }
        $processIdValue = [long]$processIdProperty.Value
        if ($processIdValue -le 0 -or $processIdValue -gt [int]::MaxValue) {
            throw "Invalid tracked process ID"
        }
        $processId = [int]$processIdValue
    } catch {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        return $null
    }

    # CIM 查询错误或无法读取命令行时无法证明 PID 已陈旧，必须失败关闭，避免
    # 临时 WMI/权限故障导致同一个仓库重复拉起或误停进程。
    $process = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter "ProcessId = $processId" `
        -ErrorAction Stop
    if ($null -eq $process) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        return $null
    }
    if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine)) {
        throw "Unable to verify command line for tracked $Description process $processId"
    }
    $identityMatches = $true
    foreach ($expectedPath in $ExpectedPaths) {
        if (-not (Test-CommandLineContains $process.CommandLine $expectedPath)) {
            $identityMatches = $false
            break
        }
    }
    if ($identityMatches) {
        return $process
    }

    # PID 已被其他程序复用时删除旧记录，调用方随后按当前运行状态恢复。
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return $null
}

function Get-TrackedBotProcess {
    return Get-TrackedProcessFromFile `
        -Path $PidFile `
        -Description "Bot" `
        -ExpectedPaths @($LogPumpScript, $MainScript)
}

function Get-TrackedMonitorProcess {
    $process = Get-TrackedProcessFromFile `
        -Path $MonitorPidFile `
        -Description "monitor" `
        -ExpectedPaths @($MonitorScript)
    if ($null -eq $process) {
        return $null
    }

    # 停服实例也执行同一个脚本。旧 PID 恰好被本次停服进程复用时，绝不能
    # 把当前控制进程当作待停止的监控器。
    if ([int]$process.ProcessId -eq $PID -or
        [string]$process.CommandLine -match '(?i)(?:^|\s)-Stop(?:\s|$)') {
        Remove-Item -LiteralPath $MonitorPidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    return $process
}

function Invoke-TaskKill {
    param([int]$ProcessId)

    $taskKillPath = Join-Path ([Environment]::SystemDirectory) "taskkill.exe"
    if (-not (Test-Path -LiteralPath $taskKillPath -PathType Leaf)) {
        throw "Windows taskkill executable not found: $taskKillPath"
    }

    $killer = $null
    try {
        $killer = Start-Process `
            -FilePath $taskKillPath `
            -ArgumentList @("/PID", $ProcessId.ToString(), "/T", "/F") `
            -WindowStyle Hidden `
            -PassThru
        if (-not $killer.WaitForExit(5000)) {
            $killer.Kill()
            [void]$killer.WaitForExit(1000)
            throw "taskkill timed out for owned process tree $ProcessId"
        }
        if ($killer.ExitCode -ne 0) {
            throw "taskkill failed for owned process tree $ProcessId with exit code $($killer.ExitCode)"
        }
    } finally {
        if ($null -ne $killer) {
            try {
                if (-not $killer.HasExited) {
                    $killer.Kill()
                }
            } catch {
                # finally 中的兜底失败不能覆盖前面更准确的进程树清理结果。
            }
            $killer.Dispose()
        }
    }
}

function Stop-OwnedProcessTree {
    param([Diagnostics.Process]$Process)

    try {
        if ($Process.HasExited) {
            return
        }
    } catch {
        # 该句柄仍属于本次启动尝试；无法读取状态时继续执行回收。
    }

    $treeCleanupError = $null
    try {
        Invoke-TaskKill -ProcessId $Process.Id
    } catch {
        $treeCleanupError = $_
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

# ---------- 精确身份停服 ----------

function Test-ProcessMatchesCommandIdentity {
    param(
        [object]$Process,
        [string[]]$ProcessNames,
        [string[]]$ExpectedPaths,
        [switch]$ExcludeStopInvocation
    )

    if ([int]$Process.ProcessId -eq $PID) {
        return $false
    }
    if ($ProcessNames.Count -gt 0 -and
        $ProcessNames -notcontains [string]$Process.Name) {
        return $false
    }

    $commandLine = [string]$Process.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        return $false
    }
    foreach ($expectedPath in $ExpectedPaths) {
        if (-not (Test-CommandLineContains $commandLine $expectedPath)) {
            return $false
        }
    }
    if ($ExcludeStopInvocation -and
        $commandLine -match '(?i)(?:^|\s)-Stop(?:\s|$)') {
        return $false
    }
    return $true
}

function Get-CommandIdentityProcesses {
    param(
        [string[]]$ProcessNames,
        [string[]]$ExpectedPaths,
        [switch]$ExcludeStopInvocation,
        [AllowNull()]
        [AllowEmptyCollection()]
        [object[]]$ProcessSnapshot = $null
    )

    if ($null -eq $ProcessSnapshot) {
        $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    } else {
        $processes = @($ProcessSnapshot)
    }
    foreach ($process in $processes) {
        if (Test-ProcessMatchesCommandIdentity `
            -Process $process `
            -ProcessNames $ProcessNames `
            -ExpectedPaths $ExpectedPaths `
            -ExcludeStopInvocation:$ExcludeStopInvocation) {
            Write-Output $process
        }
    }
}

function Stop-VerifiedCommandProcess {
    param(
        [object]$Process,
        [string]$Description,
        [string[]]$ProcessNames,
        [string[]]$ExpectedPaths,
        [switch]$ExcludeStopInvocation
    )

    $processId = [int]$Process.ProcessId
    $current = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter "ProcessId = $processId" `
        -ErrorAction Stop
    if ($null -eq $current) {
        return
    }
    if (-not (Test-ProcessMatchesCommandIdentity `
        -Process $current `
        -ProcessNames $ProcessNames `
        -ExpectedPaths $ExpectedPaths `
        -ExcludeStopInvocation:$ExcludeStopInvocation)) {
        throw "$Description process $processId changed identity before it could be stopped"
    }

    try {
        Invoke-TaskKill -ProcessId $processId
    } catch {
        $cleanupError = $_
        $remaining = Get-CimInstance `
            -ClassName Win32_Process `
            -Filter "ProcessId = $processId" `
            -ErrorAction Stop
        if ($null -ne $remaining) {
            throw $cleanupError
        }
    }
}

function Stop-AllCommandIdentityProcesses {
    param(
        [string]$Description,
        [string[]]$ProcessNames,
        [string[]]$ExpectedPaths,
        [switch]$ExcludeStopInvocation,
        [AllowNull()]
        [AllowEmptyCollection()]
        [object[]]$ProcessSnapshot = $null
    )

    $matches = @(Get-CommandIdentityProcesses `
        -ProcessNames $ProcessNames `
        -ExpectedPaths $ExpectedPaths `
        -ExcludeStopInvocation:$ExcludeStopInvocation `
        -ProcessSnapshot $ProcessSnapshot)
    foreach ($process in $matches) {
        Stop-VerifiedCommandProcess `
            -Process $process `
            -Description $Description `
            -ProcessNames $ProcessNames `
            -ExpectedPaths $ExpectedPaths `
            -ExcludeStopInvocation:$ExcludeStopInvocation
    }
}

function Stop-XiaoQingService {
    $mutexName = Get-MutexName $BotRoot
    [bool]$createdNew = $false
    $stopMutex = [Threading.Mutex]::new($true, $mutexName, [ref]$createdNew)
    $ownsMutex = $createdNew
    $powerShellNames = @("powershell.exe", "pwsh.exe")
    $pythonNames = @("python.exe", "pythonw.exe")

    try {
        if (-not $ownsMutex) {
            # 优先使用监控器自己登记的 PID；旧版本没有该文件时，再按脚本绝对
            # 路径扫描。两条路径都会在 taskkill 前重新校验命令行身份。
            $trackedMonitor = Get-TrackedMonitorProcess
            if ($null -ne $trackedMonitor) {
                Stop-VerifiedCommandProcess `
                    -Process $trackedMonitor `
                    -Description "XiaoQing monitor" `
                    -ProcessNames $powerShellNames `
                    -ExpectedPaths @($MonitorScript) `
                    -ExcludeStopInvocation
            }
            Stop-AllCommandIdentityProcesses `
                -Description "XiaoQing monitor" `
                -ProcessNames $powerShellNames `
                -ExpectedPaths @($MonitorScript) `
                -ExcludeStopInvocation

            try {
                $ownsMutex = $stopMutex.WaitOne(30000)
            } catch [Threading.AbandonedMutexException] {
                # taskkill 终止持有者后，等待方已取得这个 abandoned mutex。
                $ownsMutex = $true
            }
            if (-not $ownsMutex) {
                throw "Timed out waiting for the XiaoQing monitor to stop"
            }
        }

        # 持有同一互斥量期间，新监控器会直接退出，因此以下两轮复核不会与自动
        # 重启竞争。第一轮回收日志泵根进程，第二轮清理异常遗留的直接子进程。
        for ($pass = 0; $pass -lt 2; $pass++) {
            $processSnapshot = @(
                Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
            )
            Stop-AllCommandIdentityProcesses `
                -Description "XiaoQing Bot log pump" `
                -ProcessNames $pythonNames `
                -ExpectedPaths @($LogPumpScript, $MainScript) `
                -ProcessSnapshot $processSnapshot
            Stop-AllCommandIdentityProcesses `
                -Description "NapCat log pump" `
                -ProcessNames $pythonNames `
                -ExpectedPaths @($LogPumpScript, $NapCatPath) `
                -ProcessSnapshot $processSnapshot
            Stop-AllCommandIdentityProcesses `
                -Description "XiaoQing Bot" `
                -ProcessNames $pythonNames `
                -ExpectedPaths @($MainScript) `
                -ProcessSnapshot $processSnapshot
            Stop-AllCommandIdentityProcesses `
                -Description "NapCat" `
                -ProcessNames @([IO.Path]::GetFileName($NapCatPath)) `
                -ExpectedPaths @($NapCatPath) `
                -ProcessSnapshot $processSnapshot
        }

        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $MonitorPidFile -Force -ErrorAction SilentlyContinue
        Write-Output "XiaoQing monitor, Bot, and NapCat are stopped."
    } finally {
        if ($ownsMutex) {
            $stopMutex.ReleaseMutex()
        }
        $stopMutex.Dispose()
    }
}

# ---------- Bot 与 NapCat 启动 ----------

function Start-TrackedBot {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $botCommand = @(
        "python",
        $MainScript
    ) + $BotArguments

    # Windows PowerShell 5.1 的 Start-Process 没有进程级环境参数。这里只在
    # 创建 Bot 日志泵进程树时临时注入 MKL 层，随后立即恢复监控器原环境，
    # 因而后面启动的 NapCat 不会意外继承该设置。
    $previousMklThreadingLayer = [Environment]::GetEnvironmentVariable(
        "MKL_THREADING_LAYER",
        [EnvironmentVariableTarget]::Process
    )
    try {
        if ($MklThreadingLayer) {
            [Environment]::SetEnvironmentVariable(
                "MKL_THREADING_LAYER",
                $MklThreadingLayer,
                [EnvironmentVariableTarget]::Process
            )
        }
        $process = Start-LogPumpedProcess `
            -CommandArguments $botCommand `
            -WorkingDirectory $BotRoot `
            -StandardOutputLog $BotLog `
            -StandardErrorLog $BotErrorLog
    } finally {
        if ($MklThreadingLayer) {
            [Environment]::SetEnvironmentVariable(
                "MKL_THREADING_LAYER",
                $previousMklThreadingLayer,
                [EnvironmentVariableTarget]::Process
            )
        }
    }
    try {
        Write-ProcessIdFile -Path $PidFile -ProcessId $process.Id
    } catch {
        $pidCommitError = $_
        try {
            Stop-OwnedProcessTree -Process $process
        } catch {
            throw "PID commit failed ($($pidCommitError.Exception.Message)); owned process cleanup failed ($($_.Exception.Message))"
        }
        throw $pidCommitError
    }
}

function Test-NapCatRunning {
    if ($DisableNapCat) {
        return $true
    }
    if (-not (Test-Path -LiteralPath $NapCatPath -PathType Leaf)) {
        throw "NapCat executable disappeared while monitoring: $NapCatPath"
    }
    $expectedName = [IO.Path]::GetFileName($NapCatPath).Replace("'", "''")
    $processes = @(
        Get-CimInstance `
            -ClassName Win32_Process `
            -Filter "Name = '$expectedName'" `
            -ErrorAction Stop
    )
    foreach ($process in $processes) {
        # 同名进程存在但命令行不可读时，无法排除它就是目标 NapCat；失败关闭
        # 比继续启动第二份登录实例更安全。
        if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine)) {
            throw "Unable to verify command line for a running $expectedName process"
        }
        if (Test-CommandLineContains $process.CommandLine $NapCatPath) {
            return $true
        }
    }
    return $false
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

# ---------- 停服入口与单实例监控循环 ----------

if ($Stop) {
    Stop-XiaoQingService
    exit 0
}

[bool]$createdNew = $false
$mutex = [Threading.Mutex]::new($true, (Get-MutexName $BotRoot), [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

try {
    Write-ProcessIdFile -Path $MonitorPidFile -ProcessId $PID
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
            Start-TrackedBot
            $startedAt = Get-Date
            $wasRunning = $true
        } elseif (
            $null -ne $startedAt -and
            ((Get-Date) - $startedAt).TotalSeconds -ge $StableRunSeconds
        ) {
            $restartDelaySeconds = $InitialRestartDelaySeconds
        }

        Start-Sleep -Seconds $MonitorIntervalSeconds
    }
} finally {
    # 互斥量仍在当前进程手中，此时不会有新监控器覆盖该状态文件。
    Remove-Item -LiteralPath $MonitorPidFile -Force -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
