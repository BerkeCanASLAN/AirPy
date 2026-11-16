@echo off
rem Starts AirPy without a console window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: setting up first...
    call "%~dp0install.bat"
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0AirPy.pyw" %*
