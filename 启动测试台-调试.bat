@echo off
chcp 936 >nul
title Ego-Std 交付测试台（调试）
cd /d "%~dp0"

set "PY=%~dp0.venv-gui\Scripts\python.exe"

if not exist "%PY%" (
  echo [错误] 没找到运行环境 .venv-gui
  pause
  exit /b 1
)

echo 正在启动…关闭本窗口即退出程序。
"%PY%" "%~dp0app.py"
echo.
echo 程序已退出（退出码 %ERRORLEVEL%）。
pause
