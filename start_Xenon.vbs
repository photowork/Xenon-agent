' start_Xenon.vbs - Launch Xenon UI without console window
' Calls pythonw.exe directly, bypassing cmd
Option Explicit

Dim fso, shell, scriptDir, pythonwExe, launcherPath, cmd, logFile, log

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcherPath = scriptDir & "\launcher.py"
logFile = scriptDir & "\start_xenon_debug.log"

' Open debug log
Set log = fso.CreateTextFile(logFile, True)
log.WriteLine "[" & Now & "] VBS started"
log.WriteLine "scriptDir = " & scriptDir
log.WriteLine "launcherPath = " & launcherPath
log.WriteLine "launcher exists: " & fso.FileExists(launcherPath)

' Prefer venv pythonw.exe, fallback to PATH
If fso.FileExists(scriptDir & "\venv\Scripts\pythonw.exe") Then
    pythonwExe = scriptDir & "\venv\Scripts\pythonw.exe"
Else
    pythonwExe = "pythonw.exe"
End If
log.WriteLine "pythonwExe = " & pythonwExe
log.WriteLine "pythonw exists: " & fso.FileExists(pythonwExe)

' Set working directory
On Error Resume Next
shell.CurrentDirectory = scriptDir
If Err.Number <> 0 Then
    log.WriteLine "Set CurrentDirectory failed: " & Err.Description
    Err.Clear
End If
On Error GoTo 0

' Force UTF-8 for Python
On Error Resume Next
shell.Environment("PROCESS")("PYTHONUTF8") = "1"
shell.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
If Err.Number <> 0 Then
    log.WriteLine "Set env failed: " & Err.Description
    Err.Clear
End If
On Error GoTo 0

cmd = """" & pythonwExe & """ """ & launcherPath & """"
log.WriteLine "cmd = " & cmd

' 0 = fully hidden window, False = do not wait
On Error Resume Next
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    log.WriteLine "Run failed: " & Err.Number & " - " & Err.Description
    log.Close
    MsgBox "Failed to launch Xenon: " & Err.Description & vbCrLf & _
           "Command: " & cmd & vbCrLf & vbCrLf & _
           "See log: " & logFile, vbCritical, "Xenon Launcher"
    WScript.Quit 1
Else
    log.WriteLine "Run OK - process started"
End If
On Error GoTo 0

log.WriteLine "[" & Now & "] VBS finished"
log.Close
