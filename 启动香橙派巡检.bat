@echo off
setlocal
set "PYTHONPATH="
cd /d "%~dp0"
if not exist "%~dp0.venv-gui\Scripts\pythonw.exe" (
  echo Python GUI environment not found. Run the dependency installer first.
  pause
  exit /b 1
)
start "" "%~dp0.venv-gui\Scripts\pythonw.exe" "%~dp0app.py" --orangepi
endlocal
