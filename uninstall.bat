@echo off
rem Removes AirPy: quits the running app, deletes the Windows start-up entry and the desktop shortcut.
setlocal
cd /d "%~dp0"

echo Quitting AirPy...
rem Ask it to quit cleanly first (stops the streams), then end any leftover process
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -m airpy --quit
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*-m airpy*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Removing the Windows start-up entry...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AirPy /f >nul 2>&1

if exist ".venv\Scripts\python.exe" (
    rem Also turn it off in the settings so a later start does not add the entry back; delete the shortcut
    ".venv\Scripts\python.exe" -c "from airpy.config import Config; Config().set('autostart', False); from airpy import shortcut; shortcut.remove()"
)

echo.
choice /c YN /n /m "Delete the settings and log files (%APPDATA%\AirPy)? [Y/N] "
if errorlevel 2 goto :venv
if exist "%APPDATA%\AirPy\config.json" rmdir /s /q "%APPDATA%\AirPy"

:venv
choice /c YN /n /m "Delete the virtual environment (.venv, installed libraries)? [Y/N] "
if errorlevel 2 goto :done
if exist ".venv\Scripts\python.exe" rmdir /s /q ".venv"

:done
echo.
echo AirPy was removed. Files received from the phone (Downloads\AirPy) and this folder were not deleted.
pause
endlocal
