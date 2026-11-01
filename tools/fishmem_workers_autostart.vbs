' Fish-memory workers autostart (runs the launcher with no visible window).
'
' Called from the Windows Startup folder via FishMemWorkers.vbs, and
' usable by hand / by a scheduled task if you ever get one working:
'   wscript.exe //nologo //B tools\fishmem_workers_autostart.vbs
'
' Idempotent: fishmem_workers.ps1 skips any worker whose pid file still
' points at a live process, so repeated invocations are cheap.
'
' Every invocation appends one line to logs\autostart.log, which is the
' easiest way to confirm the Startup entry actually fired after a reboot.

Option Explicit

Dim sh, fso, toolsDir, qqbotDir, cmdPath, logPath, logDir, f

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

toolsDir = fso.GetParentFolderName(WScript.ScriptFullName)
qqbotDir = fso.GetParentFolderName(toolsDir)
cmdPath = toolsDir & "\fishmem_workers.cmd"
logDir = qqbotDir & "\data\plugin_data\astrbot_plugin_fish_memory\logs"
logPath = logDir & "\autostart.log"

If Not fso.FileExists(cmdPath) Then
    WScript.Quit 1
End If

' Breadcrumb: never let logging problems stop the launcher.
On Error Resume Next
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
Set f = fso.OpenTextFile(logPath, 8, True)
If Err.Number = 0 Then
    f.WriteLine Now() & "  autostart -> fishmem_workers.cmd start"
    f.Close
End If
Err.Clear
On Error GoTo 0

sh.CurrentDirectory = qqbotDir
sh.Run """" & cmdPath & """ start", 0, True
