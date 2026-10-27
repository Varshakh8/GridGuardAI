@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo Create the virtual environment first. See README.md.
    pause
    exit /b 1
)
start "GridGuard Billing - 5000" cmd /k "cd /d ""%~dp0"" && call venv\Scripts\activate && python app.py"
start "GridGuard Test Generator - 5002" cmd /k "cd /d ""%~dp0"" && call venv\Scripts\activate && python ai_assistant.py"
start "GridGuard QA Dashboard - 5001" cmd /k "cd /d ""%~dp0"" && call venv\Scripts\activate && python qa_dashboard.py"
timeout /t 3 >nul
start http://127.0.0.1:5000
start http://127.0.0.1:5002
start http://127.0.0.1:5001
endlocal
