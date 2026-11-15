# يفتح برنامج مراقبة العهد: يشغّل السيرفر لو مش شغال، ويفتح البرنامج في نافذة لوحده.
param([int]$Port = 8000)
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$url = "http://localhost:$Port"

function Test-Up {
    try { (Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $false }
}

if (-not (Test-Up)) {
    $py = Join-Path $root ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path $py)) { $py = Join-Path $root ".venv\Scripts\python.exe" }
    if (-not (Test-Path $py)) {
        # أول مرة: start.bat بيجهز كل حاجة
        Start-Process -FilePath (Join-Path $root "start.bat") -WorkingDirectory $root
    } else {
        $log = Join-Path $root "data\server.log"
        New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null
        Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden `
            -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "$Port" `
            -RedirectStandardError $log
    }
    for ($i = 0; $i -lt 40 -and -not (Test-Up); $i++) { Start-Sleep -Milliseconds 500 }
}

# Edge أو Chrome في وضع التطبيق (نافذة من غير شريط عناوين)، ولو مش موجودين المتصفح العادي
$browsers = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$b = $browsers | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($b) { Start-Process -FilePath $b -ArgumentList "--app=$url" } else { Start-Process $url }
