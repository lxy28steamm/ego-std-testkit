@echo off
chcp 936 >nul
cd /d "%~dp0"
title 推送到 GitHub

echo ============================================================
echo   把 ego-std-test 推到 GitHub 私有仓库
echo ============================================================
echo.
echo   需要先准备一个 Token：
echo     https://github.com/settings/tokens/new
echo     Note 随便填，Expiration 选 7 days，只勾最上面的 repo
echo     点 Generate token，复制那串 ghp_ 开头的字符
echo.

set "PY=%~dp0.venv-gui\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ------------------------------------------------------------
echo  [1] 本地准备检查
echo ------------------------------------------------------------
"%PY%" "%~dp0push_github.py" --check
if errorlevel 1 (
  echo.
  echo   上面有 [X] 项，先把它们解决再推。
  pause
  exit /b 1
)

echo.
echo ------------------------------------------------------------
echo  [2] 开始推送
echo ------------------------------------------------------------
echo   把 Token 粘贴到下面（右键粘贴），然后回车：
echo.
"%PY%" "%~dp0push_github.py"
echo.
pause
