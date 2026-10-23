Option Explicit

Dim shell, scriptPath
Set shell = CreateObject("WScript.Shell")
scriptPath = Replace(WScript.ScriptFullName, "启动账号切换器.vbs", "antigravity_account_switcher.py")
shell.Run "pythonw.exe " & Chr(34) & scriptPath & Chr(34), 0, False
