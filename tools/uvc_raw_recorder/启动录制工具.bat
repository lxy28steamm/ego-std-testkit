@echo off
cd /d "%~dp0"
pythonw app.py
if errorlevel 1 pause
