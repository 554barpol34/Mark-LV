@echo off
cd /d "%~dp0"
python -m jarvis
if errorlevel 1 pause
