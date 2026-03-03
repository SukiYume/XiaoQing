' XiaoQing Bot Auto-Start Script v2.0

Option Explicit
On Error Resume Next

Const INTERVAL = 3600
Dim ws, fso
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' --- 路径与环境配置 (使用相对路径与环境变量) ---
Dim SCRIPT_DIR, BOT_DIR, NAPCAT_DIR, CONDA_PATH

' 自动获取当前脚本所在的目录
SCRIPT_DIR = fso.GetParentFolderName(WScript.ScriptFullName)

' 1. 机器人代码目录: 默认为当前脚本所在目录
BOT_DIR = SCRIPT_DIR

' 2. NapCat启动目录: 假设它和当前项目文件夹同级
NAPCAT_DIR = fso.BuildPath(fso.GetParentFolderName(SCRIPT_DIR), "NapCat.Shell")

' 3. Conda 路径: 使用 %USERPROFILE% 自动获取当前用户的根目录 (如 C:\Users\xxx)，避免写死用户名
CONDA_PATH = ws.ExpandEnvironmentStrings("%USERPROFILE%") & "\miniconda3\Scripts\conda.exe"

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