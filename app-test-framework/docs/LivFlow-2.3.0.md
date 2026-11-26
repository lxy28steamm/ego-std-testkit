# LivFlow 2.3.0 / 111 专用入口

目标安装包：`LivFlow_2.3.0_111_production_no_wristband.apk`。
SHA256：`505332dc1aa0db3d6ad0965f02497c028de0b5d9f2446db2418a58f349dd2f3a`。

在本目录的上级目录执行：

```powershell
.\run-230.ps1 -Serial 81f61cb2 -Suite guest
.\run-230.ps1 -Serial 81f61cb2 -Suite usb
.\run-230.ps1 -Serial 81f61cb2 -Suite settings
.\run-230.ps1 -Serial 81f61cb2 -Suite tasks
```

每次运行先只读核对目标设备安装包名、versionName=2.3.0 和 versionCode=111。
版本不符会阻止测试 fixture 操作设备。版本号不能独立证明手环变体或签名相同。
本入口复用现有游客、USB、设置和任务用例，不强制重启应用、不退出已有账号。
已有登录状态、缺少硬件或业务前置条件时应保留跳过结果，不能算作通过。
设置测试可能切换语言或导出日志，任务测试可能操作筛选；开始前请保持应用空闲。

真实设备连接、参数修改、采集和上传仍使用原框架显式授权选项与目标配置，
可在原 pytest 命令增加 `--release-230` 启用版本检查。
本专用入口不自动安装 APK，也不把已有用例视为新版全部功能覆盖。
报告目录 `reports/<运行编号>/` 中包含 HTML、日志和 `release.json` 版本证据。
