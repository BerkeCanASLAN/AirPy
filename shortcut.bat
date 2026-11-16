@echo off
rem Creates an AirPy shortcut on the desktop (updates it if it already exists).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "from airpy import shortcut; print('Shortcut created:', shortcut.create())"
pause
