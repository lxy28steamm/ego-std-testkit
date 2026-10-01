@echo off
chcp 936 >nul
title Ego-Std 测试台 - 环境安装
cd /d "%~dp0"

echo ============================================================
echo   Ego-Std 交付测试台 - 首次安装
echo ============================================================
echo.

if exist ".venv-gui\Scripts\python.exe" (
  echo [1/3] 运行环境已存在，跳过创建。
  goto :deps
)

echo [1/3] 正在查找 Python 3.13 ...
set "BASEPY="
for %%V in (3.13 3.12 3.11) do (
  if not defined BASEPY (
    py -%%V -c "import sys" >nul 2>&1 && set "BASEPY=py -%%V"
  )
)
if not defined BASEPY (
  python -c "import sys;assert sys.version_info>=(3,11)" >nul 2>&1 && set "BASEPY=python"
)
if not defined BASEPY (
  echo.
  echo [错误] 没找到 Python 3.11 以上版本。
  echo 请先安装 Python：https://www.python.org/downloads/
  echo 安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo       使用：%BASEPY%
%BASEPY% -m venv .venv-gui
if errorlevel 1 (
  echo [错误] 创建虚拟环境失败。
  pause
  exit /b 1
)

:deps
echo.
echo [2/3] 正在安装依赖（PySide6 / paramiko / openpyxl）…
echo       首次安装需要下载约 150MB，请耐心等待。
echo.
".venv-gui\Scripts\python.exe" -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
".venv-gui\Scripts\python.exe" -m pip install PySide6 paramiko openpyxl -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
if errorlevel 1 (
  echo.
  echo [错误] 依赖安装失败。若是网络问题，重跑一次本脚本即可。
  pause
  exit /b 1
)

echo.
echo [3/3] 安装完成！双击「启动测试台.bat」即可使用。
echo.
pause
