' Runs launch.ps1 without a console window. First argument = port (default 8000)
Dim port, here, cmd
port = "8000"
If WScript.Arguments.Count > 0 Then port = WScript.Arguments(0)
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & here & "\launch.ps1"" -Port " & port
CreateObject("WScript.Shell").Run cmd, 0, False
