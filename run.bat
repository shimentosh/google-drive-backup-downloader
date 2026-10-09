@echo off
rem Google Drive Backup Downloader - one-click launcher.
rem Creates the virtual environment on first run, then starts the app.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo [error] Python 3.12+ was not found. Install it from https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo [setup] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [error] Dependency install failed.
        pause
        exit /b 1
    )
)

if not exist "credentials.json" if not exist "%LOCALAPPDATA%\GDriveBackup\credentials.json" (
    echo [warning] credentials.json not found.
    echo           The app will open, but "Connect Google Drive" needs it.
    echo           Put it in this folder or in %LOCALAPPDATA%\GDriveBackup\
    echo.
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause
endlocal
