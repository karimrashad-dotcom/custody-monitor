# يعمل أيقونة "مراقبة العهد" على الديسك توب. مثال: powershell -ExecutionPolicy Bypass -File tools\make-shortcut.ps1 -Port 8001
param([int]$Port = 8000)
$here = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
# WScript.Shell مبيعرفش يحفظ باسم عربي، فبنحفظ باسم إنجليزي ونغيّره بعدها
$tmp = Join-Path $desktop "custody-monitor.lnk"
$final = Join-Path $desktop "مراقبة العهد.lnk"
$lnk = $ws.CreateShortcut($tmp)
$lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$lnk.Arguments = """$here\launch.vbs"" $Port"
$lnk.WorkingDirectory = Split-Path -Parent $here
$lnk.IconLocation = "$here\icon.ico,0"
$lnk.Description = "مراقبة العهد"
$lnk.Save()
if (Test-Path -LiteralPath $final) { Remove-Item -LiteralPath $final -Force }
Rename-Item -LiteralPath $tmp -NewName "مراقبة العهد.lnk"
if (Test-Path -LiteralPath $final) { Write-Output "Shortcut created on Desktop (port $Port)" }
else { Write-Error "Shortcut was not created" }
