@echo off
rem Options Trading Assistant - Windows launcher. Double-click to run.
cd /d "%~dp0"
title Options Trading Assistant

rem Find Python (py launcher first, then python)
set PY=py
%PY% --version >nul 2>nul || set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo IMPORTANT: check "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)

echo Installing dependencies (first run takes a minute)...
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Dependency install failed - see the error above.
  pause
  exit /b 1
)

echo Starting the app... your browser will open at http://127.0.0.1:8000
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
%PY% -m uvicorn app.main:app --port 8000
echo.
echo Server stopped.
pause
