@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0antigravity_account_switcher.py"
    exit /b 0
)
python.exe "%~dp0antigravity_account_switcher.py"
endlocal
