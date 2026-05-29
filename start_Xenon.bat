@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "venv\Scripts\pythonw.exe" (
    "venv\Scripts\pythonw.exe" "launcher.py"
) else if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" "launcher.py"
) else (
    python "launcher.py"
)

if errorlevel 1 pause
