@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo Setting up for the first time, please wait...
  python -m venv .venv || (echo Python is not installed. Install it from python.org and tick "Add to PATH". & pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
)
echo.
echo Open on this laptop:  http://localhost:8000
echo On phones (same Wi-Fi), use this laptop's IP address:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo    http://%%a:8000
echo.
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
