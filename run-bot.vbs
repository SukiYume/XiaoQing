' XiaoQing Bot launcher.
' The monitor owns the single-instance lock; this wrapper only keeps the
' original double-click/hidden-window startup experience.
Option Explicit

Dim ws, fso, rootDir, monitorScript
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
monitorScript = fso.BuildPath(rootDir, "scripts\run-bot-monitor.ps1")

If Not fso.FileExists(monitorScript) Then
    WScript.Echo "XiaoQing monitor script not found: " & monitorScript
    WScript.Quit 1
End If

ws.Run "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & monitorScript & """", 0, False
