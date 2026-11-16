@echo off
rem AirPy setup: creates the virtual environment (.venv) and installs the dependencies.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :install

echo Creating virtual environment...
py -3.14 -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto :install
py -3.13 -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto :install
py -3.12 -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto :install
python -m venv .venv
if exist ".venv\Scripts\python.exe" goto :install

echo.
echo ERROR: Python was not found. Install Python 3.12 - 3.14 from https://www.python.org/downloads/
pause
exit /b 1

:install
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Could not install the dependencies. See the error message above.
    pause
    exit /b 1
)

echo.
choice /c YN /n /m "Create an AirPy shortcut on the desktop? [Y/N] "
if errorlevel 2 goto :done
".venv\Scripts\python.exe" -c "from airpy import shortcut; print('Shortcut created:', shortcut.create())"

:done
echo.
echo Setup complete. Run AirPy.bat to start the app.
pause
endlocal
