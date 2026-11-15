# يعمل أيقونة "مراقبة العهد" على الديسك توب. مثال: powershell -ExecutionPolicy Bypass -File tools\make-shortcut.ps1 -Port 8001
param([int]$Port = 8000)
$here = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $desktop "مراقبة العهد.lnk"))
$lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$lnk.Arguments = """$here\launch.vbs"" $Port"
$lnk.WorkingDirectory = Split-Path -Parent $here
$lnk.IconLocation = "$here\icon.ico,0"
$lnk.Description = "مراقبة العهد"
$lnk.Save()
Write-Output "Shortcut created on Desktop (port $Port)"
