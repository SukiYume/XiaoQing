<#
.SYNOPSIS
    在 Windows 上看护 XiaoQing 与本机 NapCat 进程。

监控器使用仓库级互斥量和 PID 文件保证单实例，通过有上限的退避重启异常退出
的子进程，并把 stdout/stderr 轮转交给 Python 日志泵。NapCat QQ 账号和可选
Bot 进程设置来自 config/config.json；脚本不内置生产环境专属值。
#>

[CmdletBinding()]
param(
    [AllowEmptyString()]
    [string]$BotRoot = "",
    [AllowEmptyString()]
    [string]$NapCatPath = "",
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
if ([string]::IsNullOrWhiteSpace($ScriptDirectory)) {
    $ScriptPath = $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrWhiteSpace($ScriptPath)) {
        throw "Unable to resolve XiaoQing monitor script directory"
    }
    $ScriptDirectory = Split-Path -Parent $ScriptPath
}
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

# ---------- 已启动 Bot 的识别与回收 ----------

function Get-TrackedBotProcess {
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        return $null
    }

    # 文件系统读取失败不是“旧 PID”，不能吞掉后继续启动第二份 Bot。
    try {
        $savedText = [IO.File]::ReadAllText($PidFile, [Text.Encoding]::UTF8)
    } catch {
        throw "Unable to read tracked Bot PID file: $PidFile ($($_.Exception.Message))"
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
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    # CIM 查询错误或无法读取命令行时无法证明 PID 已陈旧，必须失败关闭，防止
    # 临时 WMI/权限故障导致同一个仓库重复拉起 Bot。
    $process = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter "ProcessId = $processId" `
        -ErrorAction Stop
    if ($null -eq $process) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine)) {
        throw "Unable to verify command line for tracked Bot process $processId"
    }
    if ((Test-CommandLineContains $process.CommandLine $LogPumpScript) -and
        (Test-CommandLineContains $process.CommandLine $MainScript)) {
        return $process
    }

    # PID 已被其他程序复用时删除旧记录，让监控循环创建新的受管 Bot。
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    return $null
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
        $record = [pscustomobject]@{
            process_id = $process.Id
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

# ---------- 单实例监控循环 ----------

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
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
