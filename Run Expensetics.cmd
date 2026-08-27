@echo off
setlocal
cd /d "%~dp0"

echo Checking Expensetics environment...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup.ps1"
if errorlevel 1 (
    echo.
    echo Setup did not complete. Review the message above.
    pause
    exit /b 1
)

echo Starting Expensetics. Keep this window open while using the app.
echo Close this window to stop Expensetics.
".venv\Scripts\python.exe" app.py

if errorlevel 1 (
    echo.
    echo Expensetics stopped because of an error.
    pause
    exit /b 1
)
