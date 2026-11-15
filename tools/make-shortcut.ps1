# يعمل أيقونة "مراقبة العهد" على الديسك توب. مثال: powershell -ExecutionPolicy Bypass -File tools\make-shortcut.ps1 -Port 8001
param([int]$Port = 8000)
$here = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
# WScript.Shell مبيعرفش يحفظ باسم عربي، فبنحفظ باسم إنجليزي ونغيّره بعدها
$tmp = Join-Path $desktop "custody-monitor.lnk"
# الاسم من أكواد الحروف عشان يشتغل حتى لو الملف اتقري من غير UTF-8
$name = -join ([char[]]@(1605,1585,1575,1602,1576,1577,32,1575,1604,1593,1607,1583,46,108,110,107))
$final = Join-Path $desktop $name
$lnk = $ws.CreateShortcut($tmp)
$lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$lnk.Arguments = """$here\launch.vbs"" $Port"
$lnk.WorkingDirectory = Split-Path -Parent $here
$lnk.IconLocation = "$here\icon.ico,0"
$lnk.Description = "مراقبة العهد"
$lnk.Save()
if (Test-Path -LiteralPath $final) { Remove-Item -LiteralPath $final -Force }
Rename-Item -LiteralPath $tmp -NewName $name
if (Test-Path -LiteralPath $final) { Write-Output "Shortcut created on Desktop (port $Port)" }
else { Write-Error "Shortcut was not created" }
