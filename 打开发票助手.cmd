@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-invoice-assistant.ps1"
if errorlevel 1 pause
