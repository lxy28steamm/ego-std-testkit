@echo off
chcp 936 >nul
title Ego-Std 交付测试台
cd /d "%~dp0"

set "PYW=%~dp0.venv-gui\Scripts\pythonw.exe"
set "PY=%~dp0.venv-gui\Scripts\python.exe"

if not exist "%PY%" (
  echo [错误] 没找到运行环境 .venv-gui
  echo 请先在本目录执行一次安装（见 README）。
  pause
  exit /b 1
)

start "" "%PYW%" "%~dp0app.py"
exit /b 0
