@echo off
chcp 65001 >nul
echo ========================================
echo      Xenon 0.3.6 AI Agent Startup
echo ========================================
echo.

REM Get current script directory
cd /d "%~dp0"

REM Check if virtual environment exists
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found! Please create it first.
    echo.
    pause
    exit /b 1
)

echo [1/2] Activating virtual environment...
call "venv\Scripts\activate.bat"

echo [2/2] Starting Xenon 0.3.6 (with Meta-Cognition)...
echo.
echo ========================================
"venv\Scripts\python.exe" "Xenon.py"

echo.
echo ========================================
echo Program exited
echo ========================================
pause
