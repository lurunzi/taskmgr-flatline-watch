@echo off
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-flatline.txt
if errorlevel 1 exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup-ProcDump.ps1"
if errorlevel 1 exit /b 1
echo Ready. Run Start.cmd.
pause
