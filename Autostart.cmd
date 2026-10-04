@echo off
rem Usage: Autostart.cmd on ^| off ^| status
set "state=%~1"
if "%state%"=="" set "state=Status"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Set-Autostart.ps1" -State %state%
