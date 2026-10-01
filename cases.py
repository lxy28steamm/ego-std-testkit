# -*- coding: utf-8 -*-
"""LivUmi-Ego-Std 出厂测试用例集（源：飞书 wiki 表格 Sheet1）

字段说明：
  id      用例编号
  module  所属模块（Datacube / Ego-std / Livstudio）
  func    测试功能
  steps   测试步骤
  crit    通过标准
  hint    执行要点 / 易错提醒（脚本会在交互时提示）
  page    若该用例在控制台网页上操作，给出相对路径，配合 --ip 可直接打开
"""
from __future__ import annotations

CASES: list[dict[str, str]] = [
    {
        "id": "E-D-001", "module": "Datacube", "func": "电量功能",
        "steps": "开机后观察电量显示，接入充电插口",
        "crit": "电量显示无异常，接入充电插口后能够正常充电",
        "hint": "重点看插拔瞬间电量是否跳变、是否出现负电量或 0%",
        "page": "",
    },
    {
        "id": "E-D-002", "module": "Datacube", "func": "网络功能",
        "steps": "点开右上角网络，删除网络，输入密码重新连接",
        "crit": "网络正常连接，可以正常删除网络",
        "hint": "删完要能重新扫到并重连；顺带确认重连后控制台仍能打开",
        "page": "",
    },
    {
        "id": "E-D-003", "module": "Datacube", "func": "头环功能",
        "steps": "点击选择对应的头环相机，然后点击实时画面",
        "crit": "头环正常连接，实时画面正常，无黑边，无模糊",
        "hint": "黑边看四周边缘，模糊看画面细节；换一个头环再重复一次",
        "page": "",
    },
    {
        "id": "E-D-004", "module": "Datacube", "func": "采集控制台",
        "steps": "扫码或输入ip",
        "crit": "均可访问采集控制台",
        "hint": "扫码和手输 IP 两种方式都要试；IP 从设备概览页读",
        "page": "/",
    },
    {
        "id": "E-D-005", "module": "Datacube", "func": "采集功能",
        "steps": "点击开始采集，点击停止采集",
        "crit": "可以正常启停",
        "hint": "停止后确认 SSD 里落了文件且能解析；连续启停 3 次看是否卡死",
        "page": "/",
    },
    {
        "id": "E-D-006", "module": "Datacube", "func": "SSD存储",
        "steps": "点击刷新，选择SSD存储，点击弹出存储",
        "crit": "可以正常检出SSD存储，点击可切换SSD存储，切换之后点击弹出无报错",
        "hint": "不插盘点刷新应提示未检出；弹出后再采集应回落内置存储或报错提示",
        "page": "/",
    },
    {
        "id": "E-D-007", "module": "Datacube", "func": "网线功能",
        "steps": "连接网线到交换机，看控制台页面能否正确显示网线ip",
        "crit": "控制台界面可以正确显示网线ip",
        "hint": "对比控制台显示的 IP 和系统里 ifconfig 的实际 IP 是否一致",
        "page": "/",
    },
    {
        "id": "E-E-008", "module": "Ego-std", "func": "画面检查",
        "steps": "连接头环进入标定软件",
        "crit": "切换到对应分辨率看实时画面，画面清晰无黑边，画面流畅无模糊，无色差",
        "hint": "每个分辨率档位都要切一遍；色差看白平衡和边缘偏色",
        "page": "",
    },
    {
        "id": "E-E-009", "module": "Ego-std", "func": "标定检验",
        "steps": "连接头环进入标定软件",
        "crit": "看分辨率是否正常(235 1600*1200;233 1920*1080)",
        "hint": "认准型号对应分辨率：235 -> 1600x1200，233 -> 1920x1080，不符即 FAIL",
        "page": "",
    },
    {
        "id": "E-L-010", "module": "Livstudio", "func": "功能检验",
        "steps": "通过扫描二维码或输入控制台页面的ip，进入Livstuio",
        "crit": "能够顺利进入页面",
        "hint": "扫码和手输 IP 各试一次；页面打不开时先 ping 设备 IP",
        "page": "/",
    },
    {
        "id": "E-L-011", "module": "Livstudio", "func": "页面检查",
        "steps": "检查页面的内容无异常",
        "crit": "版本号，设备激活状态，登录信息，设备离线状态",
        "hint": "四项逐一核对：版本号、激活状态、登录信息、离线状态显示是否正确",
        "page": "/",
    },
    {
        "id": "E-L-012", "module": "Livstudio", "func": "登录功能",
        "steps": "进行登录功能测试",
        "crit": "能够正常登录，正常激活，输入错误密码，账号无法登录",
        "hint": "正向登录 + 错误密码被拒，两个方向都要测",
        "page": "/",
    },
    {
        "id": "E-L-013", "module": "Livstudio", "func": "SSD存储",
        "steps": "点击刷新，选择SSD存储，点击弹出存储",
        "crit": "可以正常检出SSD存储，点击可切换SSD存储，切换之后点击弹出无报错",
        "hint": "与 E-D-006 同源，两侧表现应一致；注意切换后正在采集的任务是否受影响",
        "page": "/",
    },
    {
        "id": "E-L-014", "module": "Livstudio", "func": "设备概览",
        "steps": "检查界面信息",
        "crit": "该界面信息无误，可以正常切换删除WIFI",
        "hint": "SN、固件版本、存储余量重点核对；切 WiFi 后看概览是否刷新",
        "page": "/",
    },
    {
        "id": "E-L-015", "module": "Livstudio", "func": "相机检测",
        "steps": "将相机连接到设备上，点击相机检测",
        "crit": "点击相机检测后，能正确检测到相机",
        "hint": "拔掉相机再点检测，应提示未检测到；插上后数量与型号都要对",
        "page": "/",
    },
    {
        "id": "E-L-016", "module": "Livstudio", "func": "参数设置",
        "steps": "设置各个参数",
        "crit": "调整参数后，设备能正常运行，采集无异常",
        "hint": "改完参数一定要实采一次验证；改完记得恢复默认值再测其他项",
        "page": "/",
    },
    {
        "id": "E-L-017", "module": "Livstudio", "func": "画面预览",
        "steps": "点击捕获画面",
        "crit": "点击捕获画面后能正常展示画面，画面无异常",
        "hint": "多镜头都要捕获一次；卡住不出图或黑屏即 FAIL",
        "page": "/",
    },
    {
        "id": "E-L-018", "module": "Livstudio", "func": "采集启停",
        "steps": "点击采集开始/结束",
        "crit": "点击采集开始/结束后，采集正常启动/停止",
        "hint": "连做 3 轮启停，第 2/3 轮最容易暴露队列或句柄泄漏问题",
        "page": "/",
    },
]

# 飞书表格定位信息（回填结果用）
FEISHU = {
    "wiki_url": "https://hwods8kg6v3.feishu.cn/wiki/HxumwrEKxiNeQ3kD0YHcOM8LnKb",
    "sheet_token": "RxTxsF4HQh5w3EtIlxQcO0ENnRh",
    "sheet_id": "1c0b4a",
    "result_col": "F",   # 测试结果
    "note_col": "G",     # 备注
    "first_row": 2,      # 首条用例所在行
}
