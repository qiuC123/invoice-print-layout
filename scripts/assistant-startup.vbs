Option Explicit
Dim shell, files, launcher, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
launcher = files.BuildPath(files.GetParentFolderName(WScript.ScriptFullName), "start-invoice-assistant.ps1")
command = Chr(34) & shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe") & Chr(34) _
    & " -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Chr(34) & launcher & Chr(34) _
    & " -NoBrowser -EnsureRunning"
WScript.Quit shell.Run(command, 0, True)
