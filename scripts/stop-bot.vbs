' XiaoQing Windows 双击停服入口。
' 停服逻辑复用监控器的仓库级互斥量、PID 和命令行身份校验；本文件只负责
' 调用相对路径脚本、等待停服完成并向桌面用户显示结果。
Option Explicit

Dim ws, fso, scriptDir, monitorScript, command
Dim process, exitCode, stdoutText, stderrText, details
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
monitorScript = fso.BuildPath(scriptDir, "run-bot-monitor.ps1")

If Not fso.FileExists(monitorScript) Then
    WScript.Echo "XiaoQing monitor script not found: " & monitorScript
    WScript.Quit 1
End If

command = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass " & _
    "-WindowStyle Hidden -File """ & monitorScript & """ -Stop"
Set process = ws.Exec(command)
Do While process.Status = 0
    WScript.Sleep 100
Loop

exitCode = process.ExitCode
stdoutText = Trim(process.StdOut.ReadAll)
stderrText = Trim(process.StdErr.ReadAll)

If exitCode = 0 Then
    WScript.Echo "XiaoQing monitor, Bot, and NapCat have stopped."
Else
    details = stderrText
    If Len(details) = 0 Then details = stdoutText
    If Len(details) > 1200 Then details = Left(details, 1200) & "..."
    If Len(details) > 0 Then
        WScript.Echo "XiaoQing stop failed (exit code " & exitCode & ")." & _
            vbCrLf & vbCrLf & details
    Else
        WScript.Echo "XiaoQing stop failed (exit code " & exitCode & ")."
    End If
End If
WScript.Quit exitCode
