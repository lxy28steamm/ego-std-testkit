@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV=%~dp0.venv-gui\Scripts\python.exe"
if exist "%VENV%" (set "PY=%VENV%") else (set "PY=python")

set "HOST=%1"
if not "%HOST%"=="" goto have_host
if exist "device_ip.txt" set /p HOST=<"device_ip.txt"
if "%HOST%"=="" for /f "usebackq delims=" %%i in (`%PY% find_device.py --quiet`) do set "HOST=%%i"
if "%HOST%"=="" set "HOST=未发现设备"
:have_host

:menu
cls
echo ==========================================
echo   Ego-Std 测试工具
echo   解释器: %PY%
echo   设备IP: %HOST%
echo ==========================================
echo   7  启动图形界面 (推荐)
echo   ------------------------------------------
echo   1  只读体检 (不动设备状态)
echo   2  体检 + 实测采集启停
echo   3  18条用例交互引导
echo   4  生成 HTML 报告
echo   5  导出 Excel
echo   6  批量巡检 (多台设备, 并发)
echo   8  自动扫描设备IP并记住
echo   9  修改设备IP (当前 %HOST%)
echo   0  退出
echo ==========================================
set "CH="
set /p "CH=请输入序号: "
if "%CH%"=="7" goto c7
if "%CH%"=="1" goto c1
if "%CH%"=="2" goto c2
if "%CH%"=="3" goto c3
if "%CH%"=="4" goto c4
if "%CH%"=="5" goto c5
if "%CH%"=="6" goto c6
if "%CH%"=="8" goto c8
if "%CH%"=="9" goto c9
if "%CH%"=="0" goto end
goto menu

:c7
chcp 936 >nul
if not exist "%~dp0.venv-gui\Scripts\python.exe" (
  echo [提示] 还没安装依赖，请先运行「安装依赖.bat」。
  pause
  goto menu
)
start "" "%~dp0启动测试台.bat"
goto end

:c1
"%PY%" ego_api_test.py --host %HOST% --model 235 --no-manual
chcp 936 >nul
goto pause

:c2
"%PY%" ego_api_test.py --host %HOST% --model 235 --do-collect
chcp 936 >nul
goto pause

:c3
set "DEV="
set /p "DEV=设备编号(如 DUT-01): "
if "%DEV%"=="" set "DEV=DUT-01"
set "WHO="
set /p "WHO=测试人(可留空): "
if "%WHO%"=="" set "WHO="
"%PY%" ego_test.py run -d %DEV% --tester %WHO% --fw 4.0.34
chcp 936 >nul
goto pause

:c4
set "DEV="
set /p "DEV=设备编号(需先跑过 3): "
"%PY%" ego_test.py report -d %DEV%
chcp 936 >nul
goto pause

:c5
set "DEV="
set /p "DEV=设备编号(需先跑过 3): "
"%PY%" ego_test.py export -d %DEV%
chcp 936 >nul
goto pause

:c6
chcp 65001 >nul
set "BIPS="
set /p "BIPS=设备IP(留空=自动扫描本机网段, 多台用逗号分隔): "
set "BW=5"
set /p "BW=并发数(默认5, 3-10为宜): "
if "%BW%"=="" set "BW=5"
echo 正在批量巡检，请稍候...
if "%BIPS%"=="" (
  "%PY%" batch_check.py --discover --workers %BW%
) else (
  "%PY%" batch_check.py --ip "%BIPS%" --workers %BW%
)
chcp 936 >nul
goto pause

:c8
chcp 65001 >nul
"%PY%" find_device.py --save
chcp 936 >nul
if exist "device_ip.txt" set /p HOST=<"device_ip.txt"
echo.
pause
goto menu

:c9
set "NEWH="
set /p "NEWH=新的设备IP: "
if not "%NEWH%"=="" set "HOST=%NEWH%"
goto menu

:pause
echo.
echo 结果在 out\ 目录
pause
goto menu

:end
endlocal
