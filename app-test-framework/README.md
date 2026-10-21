# Android APP 自动化测试框架

Python + pytest + Appium / UiAutomator2。配置、页面操作和业务用例分离；每个用例创建并关闭独立会话。默认只做本地框架自检，显式加 `-Device` 才操作手机。

## 快速开始（PowerShell）

```powershell
cd D:\app-test-framework
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
```

报告在 `reports/每次运行时间及编号/report.html`，同目录包含日志以及失败时的截图和页面 XML。截图/XML 可能包含个人信息，分享前检查。

## 真机环境

准备兼容当前 Appium 的 Node.js、JDK、Android SDK，并设置 ANDROID_HOME、JAVA_HOME。首次安装服务器：

```powershell
npm install -g appium
appium driver install uiautomator2
appium driver doctor uiautomator2
adb devices -l
appium --address 127.0.0.1 --port 4723
```

手机打开 USB 调试并完成授权。在另一个终端执行：

```powershell
cd D:\app-test-framework
$env:ANDROID_UDID = '替换为 adb 显示的序列号'
.\run.ps1 -Device
```

默认示例启动系统设置并断言前台包名。真机运行会部署 Appium 自动化辅助组件；测试目标 APP 默认保留数据（noReset）。每台设备串行运行，当前版本不支持多设备并行。

## 接入自己的 APP

复制 `config/android.yaml` 为 `config/local.yaml`，修改 appPackage、appActivity，再运行 `./run.ps1 -Device -Config ./config/local.yaml`。也可用环境变量 APP_PACKAGE、APP_ACTIVITY、APPIUM_URL 覆盖。APP_APK 支持指定本地 APK；仍建议填预期包名以完成启动断言。APK 安装路径由 Appium 服务器所在机器解析。

1. `pages/`：每个业务页面写一个类，继承 `framework/page.py` 的 BasePage。
2. 用 resource-id 或 accessibility id 定位；`pages/settings.py` 仅展示页面写法，不同手机的设置定位不同。
3. `tests/`：编写带 `@pytest.mark.device` 的业务用例，通过 driver fixture 操作页面。
4. 用 `@pytest.mark.parametrize` 增加多组测试数据。账号从环境变量读取，不提交真实密码。

```python
import pytest
from appium.webdriver.common.appiumby import AppiumBy
from framework.page import BasePage

class HomePage(BasePage):
    TITLE = (AppiumBy.ID, 'com.example.app:id/title')

@pytest.mark.device
def test_home_title(driver, app_config):
    page = HomePage(driver, app_config['wait_seconds'])
    assert page.text(page.TITLE) == '首页'
```

占位定位和文案需要替换成真实 APP 信息；此示例不计为已执行业务测试。不自动重试，避免掩盖偶发问题。每个测试需自行建立可重复的初始业务状态；noReset 不会清理账号和历史数据。驱动会话创建失败时无法截屏，请查看日志和 Appium 终端。

## 目录与扩展

`framework/` 通用配置和等待操作；`pages/` 页面对象；`tests/` 用例；`config/` 环境；`conftest.py` 会话管理和失败取证；`run.ps1` 入口。

已增加 LivFlow 任务、设置、蓝牙配网、参数恢复、短采集、MCAP校验和测试文件上传的扩展流程，见 [扩展巡检脚本](docs/扩展巡检脚本.md)。需要人工登录或插卡时等待继续，写入、录制和上传默认不授权。未覆盖固件刷写、删除、长期稳定性、全部故障注入和iOS。

官方参考：https://github.com/appium/appium-uiautomator2-driver 、https://github.com/appium/python-client
