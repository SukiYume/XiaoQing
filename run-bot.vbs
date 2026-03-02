' XiaoQing Bot Auto-Start Script v2.0

Option Explicit
On Error Resume Next

Const INTERVAL = 3600
Const BOT_DIR = "C:\Users\torch\Desktop\XiaoQing\XiaoQing_V3"
Const NAPCAT_DIR = "C:\Users\torch\Desktop\XiaoQing\NapCat.Shell"
Const CONDA_PATH = "C:\Users\torch\miniconda3\Scripts\conda.exe"

Dim ws, fso
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

If LCase(Right(WScript.FullName, 11)) = "wscript.exe" Then
    ws.Run "cscript //NoLogo """ & WScript.ScriptFullName & """", 0
    WScript.Quit
End If

WScript.Echo FormatDateTime(Now) & " - Monitor started"

Do While True
    If Not IsRunning("NapCatWinBootMain.exe") Then
        ws.CurrentDirectory = NAPCAT_DIR
        ws.Run "cmd /c chcp 65001 >nul && NapCatWinBootMain.exe 3288849221 >> run-bot.log 2>&1", 0
        WScript.Echo FormatDateTime(Now) & " - NapCat started"
        WScript.Sleep 10000
    End If
    
    If Not IsBotRunning() Then
        ws.CurrentDirectory = BOT_DIR
        ws.Run "cmd /c chcp 65001 >nul && """ & CONDA_PATH & """ run -n base --no-capture-output python main.py >> bot.log 2>&1", 0
        WScript.Echo FormatDateTime(Now) & " - XiaoQing Bot started"
    End If
    
    WScript.Sleep 1000 * INTERVAL
Loop

Function IsRunning(name)
    Dim ps
    IsRunning = False
    Set ps = ws.Exec("tasklist /FI ""IMAGENAME eq " & name & """ /NH")
    If InStr(ps.StdOut.ReadAll, name) > 0 Then IsRunning = True
    Set ps = Nothing
End Function

Function IsBotRunning()
    Dim ps, re
    IsBotRunning = False
    Set ps = ws.Exec("wmic process where ""name='python.exe'"" get commandline /format:list")
    Set re = New RegExp
    re.IgnoreCase = True
    re.Pattern = "main\.py"
    If re.Test(ps.StdOut.ReadAll) Then IsBotRunning = True
    Set re = Nothing
    Set ps = Nothing
End Function