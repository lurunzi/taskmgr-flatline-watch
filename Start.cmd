@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Python environment is missing. Run Setup.cmd first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start.ps1"
