' XiaoQing Windows 双击启动入口。
' 单实例判断、参数读取和进程看护全部由 PowerShell 监控器负责；本文件仅保留
' 生产环境需要的相对路径和隐藏窗口启动体验，不内置 QQ、Python 或 Conda 路径。
Option Explicit

Dim ws, fso, scriptDir, monitorScript
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
monitorScript = fso.BuildPath(scriptDir, "run-bot-monitor.ps1")

If Not fso.FileExists(monitorScript) Then
    WScript.Echo "XiaoQing monitor script not found: " & monitorScript
    WScript.Quit 1
End If

ws.Run "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & monitorScript & """", 0, False
