# -*- coding: utf-8 -*-
"""Ego-Std 交付测试台（桌面版）

把 ego-std-test 目录下的一堆命令行脚本，包成一个大窗口：
    左边选功能 → 右边填参数 → 点开始 → 实时输出 + 红绿灯结论。

设计原则（给团队新人用）：
    1. 不需要记任何命令，也不用手敲 --host
    2. 跑完直接给「能不能交」的结论，不靠人读日志
    3. 危险操作（写文件、真采集）默认不勾，要手动打开

运行：  python app.py
截图：  python app.py --screenshot out.png     （渲染主窗口后退出）
"""
from __future__ import annotations

import os
import re
import sys
import json
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QObject, Signal, QSize
from PySide6.QtGui import QFont, QColor, QTextCursor, QPixmap, QPainter
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox,
    QPlainTextEdit, QTableWidget, QTableWidgetItem, QListWidget, QListWidgetItem,
    QStackedWidget, QSplitter, QGroupBox, QFrame, QFileDialog, QMessageBox,
    QHeaderView, QStatusBar, QSizePolicy, QAbstractItemView,
)

BASE = Path(__file__).resolve().parent
PYEXE = sys.executable
DEVICE_FILE = BASE / "device_ip.txt"
EXPORT_DIR = BASE / "out"          # 与 ego_api_test.py 的报告目录保持一致

# 结果行形如：  [+] E-D-004      采集控制台        PASS  state=healthy
RESULT_RE = re.compile(
    r"^\s*\[([+x!\-])\]\s+(\S+)\s+(.*?)\s+(PASS|FAIL|WARN|SKIP)\b\s*(.*)$"
)
FLAG2STATUS = {"+": "PASS", "x": "FAIL", "!": "WARN", "-": "SKIP"}

C_OK, C_BAD, C_WARN, C_SKIP = "#1a7f37", "#c62828", "#b26a00", "#6b7280"
BG_OK, BG_BAD, BG_WARN, BG_SKIP = "#e8f5e9", "#fdecea", "#fff5e5", "#f2f3f5"

MONO = "Consolas"


# --------------------------------------------------------------------- 设备 IP

def load_device_ip() -> str:
    """优先级：device_ip.txt > 环境变量 EGO_HOST > 空。"""
    for p in (DEVICE_FILE, BASE / "device_ip.txt"):
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    return ln
        except Exception:  # noqa: BLE001
            pass
    return os.environ.get("EGO_HOST", "")


def save_device_ip(ip: str) -> None:
    try:
        DEVICE_FILE.write_text(
            "# ego 设备 IP —— 由测试台自动维护\n" + ip.strip() + "\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def probe_host(ip: str, port: int = 18000, timeout: float = 1.5) -> bool:
    """拉一次 /api/v2/health，通了才算真设备（端口开着不代表是设备）。"""
    import socket
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://{ip}:{port}/api/v2/health", timeout=timeout
        ) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 进程封装

class Runner(QObject):
    """跑一个子进程（调用同目录下的 .py 脚本），把 stdout 按行抛出来。"""

    line = Signal(str)
    finished = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(str(BASE))
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(self._error)
        self._buf = b""

    def start(self, args: list[str]) -> bool:
        if self.running:
            return False
        script = BASE / args[0]
        if not script.exists():
            self.line.emit(f"[错误] 找不到脚本 {script}")
            self.finished.emit(-1)
            return False
        self._buf = b""
        self.line.emit(f"$ {PYEXE} {' '.join(args)}")
        self.line.emit("-" * 68)
        self.proc.setProgram(PYEXE)
        self.proc.setArguments(args)
        self.proc.start()
        return True

    def stop(self) -> None:
        if self.running:
            self.line.emit("[已手动停止]")
            self.proc.kill()

    @property
    def running(self) -> bool:
        return self.proc.state() != QProcess.NotRunning

    def _read(self) -> None:
        self._buf += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self.line.emit(raw.decode("utf-8", "replace").rstrip("\r"))

    def _done(self, code: int, _status) -> None:
        if self._buf:
            self.line.emit(self._buf.decode("utf-8", "replace").rstrip("\r"))
            self._buf = b""
        self.finished.emit(code)

    def _error(self, err) -> None:
        if err == QProcess.FailedToStart:
            self.line.emit("[错误] 子进程启动失败")


# ------------------------------------------------------------------ 基础面板

class Panel(QWidget):
    """一个功能页：标题 + 参数行 + 按钮 + 输出区。子类只需描述参数与命令。"""

    title = "功能"
    desc = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runner = Runner(self)
        self.runner.line.connect(self.append)
        self.runner.finished.connect(self.on_finished)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        h = QLabel(self.title)
        h.setStyleSheet("font-size:17px;font-weight:600;color:#14181f")
        root.addWidget(h)
        if self.desc:
            d = QLabel(self.desc)
            d.setWordWrap(True)
            d.setStyleSheet("color:#5b6472;font-size:12px")
            root.addWidget(d)

        self.ctrl = QWidget()
        self.ctrl_layout = QGridLayout(self.ctrl)
        self.ctrl_layout.setContentsMargins(0, 4, 0, 0)
        self.ctrl_layout.setHorizontalSpacing(10)
        self.ctrl_layout.setVerticalSpacing(8)
        root.addWidget(self.ctrl)
        self.build_controls()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_run = QPushButton("开始")
        self.btn_run.setFixedHeight(34)
        self.btn_run.setMinimumWidth(110)
        self.btn_run.setStyleSheet(
            "QPushButton{background:#1f6feb;color:#fff;border:none;border-radius:6px;"
            "font-size:14px;font-weight:600;padding:0 18px}"
            "QPushButton:hover{background:#1a63d4}"
            "QPushButton:disabled{background:#b9c2cf}"
        )
        self.btn_run.clicked.connect(self.on_run)
        btn_row.addWidget(self.btn_run)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setFixedHeight(34)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#fff;color:#c62828;border:1px solid #e0b4b0;"
            "border-radius:6px;font-size:14px;padding:0 16px}"
            "QPushButton:hover{background:#fdecea}"
            "QPushButton:disabled{color:#b9c2cf;border-color:#e3e6ea}"
        )
        self.btn_stop.clicked.connect(self.runner.stop)
        btn_row.addWidget(self.btn_stop)

        self.btn_clear = QPushButton("清空输出")
        self.btn_clear.setFixedHeight(34)
        self.btn_clear.setStyleSheet(
            "QPushButton{background:#fff;color:#3c4553;border:1px solid #d9dee6;"
            "border-radius:6px;font-size:14px;padding:0 16px}"
            "QPushButton:hover{background:#f5f7fa}"
        )
        self.btn_clear.clicked.connect(lambda: self.out.clear())
        btn_row.addWidget(self.btn_clear)
        btn_row.addStretch(1)

        self.chk_shutdown = QCheckBox("跑完关机")
        self.chk_shutdown.setToolTip(
            "勾选后，本任务结束时自动关机（倒计时 60 秒，期间可用 shutdown /a 取消）\n"
            "适合挂长任务：晚上跑完采样自动断电。"
        )
        self.chk_shutdown.setStyleSheet("color:#c62828;font-size:12px")
        self.chk_shutdown.stateChanged.connect(self._on_shutdown_toggle)
        btn_row.addWidget(self.chk_shutdown)

        root.addLayout(btn_row)

        self.build_middle(root)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(QFont(MONO, 9))
        self.out.setStyleSheet(
            "QPlainTextEdit{background:#0f1419;color:#d7dde5;border:1px solid #263041;"
            "border-radius:8px;padding:10px;selection-background-color:#264f78}"
        )
        self.out.setPlaceholderText("点「开始」后，这里实时显示运行输出…")
        root.addWidget(self.out, 1)

    # --- 子类可覆写 -------------------------------------------------
    def build_controls(self) -> None:
        hint = QLabel("此功能无参数。")
        hint.setStyleSheet("color:#8b93a1;font-size:12px")
        self.ctrl_layout.addWidget(hint, 0, 0)

    def build_middle(self, root) -> None:
        """可覆写：在按钮行与输出框之间插入自定义控件（如结果表）。"""
        return

    def build_args(self) -> list[str]:
        return []

    def _on_shutdown_toggle(self, _state) -> None:
        # 直接读控件状态，避免 Qt5/Qt6 里 stateChanged 参数类型差异
        if self.chk_shutdown.isChecked():
            yes = QMessageBox.question(
                self, "确认开启自动关机",
                "任务结束后将自动关机（倒计时 60 秒）。\n\n"
                "⚠ 执行前请确认设备侧的数据已经写完——\n"
                "先停采集、等落盘，关机命令管不到设备本身。\n\n"
                "确认开启？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if yes != QMessageBox.Yes:
                self.chk_shutdown.setChecked(False)

    def _do_shutdown(self) -> None:
        import subprocess
        try:
            subprocess.Popen(["shutdown", "/s", "/t", "60"])
            QMessageBox.information(
                self, "自动关机已排定",
                "任务已结束，电脑将在 60 秒后关机。\n\n"
                "要取消：在开始菜单「运行」里执行  shutdown /a\n"
                "（或在本机任意终端执行同样命令）"
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "自动关机失败", f"无法排定关机：{e}")

    def on_finished(self, code: int) -> None:
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        color = "#1a7f37" if code == 0 else "#c62828"
        self.append(f"<b style='color:{color}'>—— 结束（退出码 {code}）——</b>")
        if self.chk_shutdown.isChecked():
            self.chk_shutdown.setChecked(False)   # 防重复触发
            self.out.appendHtml(
                "<b style='color:#c62828'>［自动关机］60 秒后关机，shutdown /a 可取消</b>")
            self._do_shutdown()

    def on_run(self) -> None:
        args = self.build_args()
        if not args:
            return
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.out.clear()
        if not self.runner.start(args):
            self.on_finished(-1)

    # --- 输出辅助 ---------------------------------------------------
    def append(self, text: str) -> None:
        """colorize 返回带 <span>/<b> 标签时按 HTML 渲染，否则按纯文本。"""
        rendered = self.colorize(text)
        if "<span" in rendered or "<b " in rendered or "<br" in rendered:
            self.out.appendHtml(rendered)
        else:
            self.out.appendPlainText(rendered)
        self.out.moveCursor(QTextCursor.End)

    @staticmethod
    def colorize(line: str) -> str:
        return line


# ================================================================== 各功能面板

class CheckupPanel(Panel):
    title = "① 一键体检"
    desc = ("对整机做一轮自动巡检：设备在线、相机/存储/网络/登录、IMU 配置与传感器、"
            "采集启停。跑完直接给「能不能交」的结论。")

    def build_controls(self) -> None:
        g = self.ctrl_layout

        g.addWidget(QLabel("头环型号"), 0, 0)
        self.model = QComboBox()
        self.model.addItem("不校验", "")
        self.model.addItem("233（1080P）", "233")
        self.model.addItem("235（2K）", "235")
        self.model.setCurrentIndex(2)
        self.model.setFixedWidth(150)
        g.addWidget(self.model, 0, 1)

        g.addWidget(QLabel("Livstudio 账号"), 0, 2)
        self.acct = QLineEdit()
        self.acct.setPlaceholderText("留空则不测登录功能")
        g.addWidget(self.acct, 0, 3)

        g.addWidget(QLabel("密码"), 1, 2)
        self.pwd = QLineEdit()
        self.pwd.setEchoMode(QLineEdit.Password)
        self.pwd.setPlaceholderText("可留空")
        g.addWidget(self.pwd, 1, 3)

        self.do_collect = QCheckBox("实测一轮采集（会往设备写文件，约 30 秒）")
        g.addWidget(self.do_collect, 1, 0, 1, 2)

        # 型号列固定宽，把多余空间让给输入框
        g.setColumnStretch(1, 0)
        g.setColumnStretch(3, 1)
        g.setColumnMinimumWidth(2, 90)

    def build_middle(self, root) -> None:
        self.banner = QLabel("")
        self.banner.setVisible(False)
        self.banner.setWordWrap(True)
        root.addWidget(self.banner)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "编号", "检查项", "详情"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 34)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setMaximumHeight(0)  # 先藏着，有结果再展开
        root.addWidget(self.table)

    def reset_table(self) -> None:
        self.table.setRowCount(0)
        self.table.setMaximumHeight(0)
        self.banner.setVisible(False)

    def build_args(self) -> list[str]:
        self.reset_table()
        self._tally = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
        args = ["ego_api_test.py", "--no-manual"]
        host = WINDOW.device_ip()
        if host:
            args += ["--host", host]
        m = self.model.currentData()
        if m:
            args += ["--model", m]
        if self.acct.text().strip():
            args += ["--account", self.acct.text().strip(),
                     "--password", self.pwd.text()]
        if self.do_collect.isChecked():
            args.append("--do-collect")
        return args

    def colorize(self, line: str) -> str:
        m = RESULT_RE.match(line)
        if not m:
            return line
        flag, cid, name, status, detail = m.groups()
        self._add_row(status, cid, name.strip(), detail.strip())
        col = {"PASS": C_OK, "FAIL": C_BAD, "WARN": C_WARN, "SKIP": C_SKIP}[status]
        return f"  [{flag}] {cid:<10} {name.strip():<14} {status:<4} {detail.strip()}"

    def _add_row(self, status: str, cid: str, name: str, detail: str) -> None:
        bg = {"PASS": BG_OK, "FAIL": BG_BAD, "WARN": BG_WARN, "SKIP": BG_SKIP}[status]
        col = {"PASS": C_OK, "FAIL": C_BAD, "WARN": C_WARN, "SKIP": C_SKIP}[status]
        r = self.table.rowCount()
        self.table.insertRow(r)
        dot = QTableWidgetItem({"PASS": "●", "FAIL": "●", "WARN": "●", "SKIP": "○"}[status])
        dot.setForeground(QColor(col))
        f = dot.font()
        f.setPointSize(12)
        dot.setFont(f)
        dot.setTextAlignment(Qt.AlignCenter)
        cells = [dot, QTableWidgetItem(cid), QTableWidgetItem(name), QTableWidgetItem(detail)]
        for c, it in enumerate(cells):
            it.setBackground(QColor(bg))
            if c in (1, 2, 3):
                it.setForeground(QColor("#1f2530"))
            self.table.setItem(r, c, it)
        self._tally[status] += 1

    def on_finished(self, code: int) -> None:
        super().on_finished(code)
        self.table.setMaximumHeight(240)
        t = self._tally
        total = sum(t.values())
        if total == 0:
            return
        if t["FAIL"] == 0:
            self._banner(f"✔ 通过　共 {total} 项：{t['PASS']} 通过 / {t['WARN']} 警告 / "
                         f"{t['SKIP']} 跳过　—— 这台可以交付", "#e8f5e9", "#1a7f37")
        else:
            self._banner(f"✘ 未通过　共 {total} 项：{t['FAIL']} 失败 / {t['PASS']} 通过 / "
                         f"{t['WARN']} 警告　—— 先处理失败项再交", "#fdecea", "#c62828")

    def _banner(self, text: str, bg: str, fg: str) -> None:
        self.banner.setText(text)
        self.banner.setStyleSheet(
            f"background:{bg};color:{fg};border-radius:8px;padding:12px 16px;"
            f"font-size:15px;font-weight:600"
        )
        self.banner.setVisible(True)


class FindPanel(Panel):
    title = "② 设备发现"
    desc = "不知道设备 IP 时用这个。扫描本机所在网段，只有能拉通 API 的才算设备。"

    def build_controls(self) -> None:
        g = self.ctrl_layout
        g.addWidget(QLabel("额外网段"), 0, 0)
        self.extra = QLineEdit()
        self.extra.setPlaceholderText("如 192.168.199（留空只扫本机网段）")
        g.addWidget(self.extra, 0, 1, 1, 2)
        self.save = QCheckBox("找到后记入设备栏（下次自动填）")
        self.save.setChecked(True)
        g.addWidget(self.save, 0, 3)
        g.setColumnStretch(1, 1)

    def build_args(self) -> list[str]:
        args = ["find_device.py", "--timeout", "0.3"]
        e = self.extra.text().strip()
        if e:
            args += ["--extra", e]
        args.append("--ssh")
        return args

    def colorize(self, line: str) -> str:
        if "18000" in line or "设备" in line:
            return f"<span style='color:#7ee787'>{line}</span>"
        return line

    def on_finished(self, code: int) -> None:
        super().on_finished(code)
        if self.save.isChecked():
            WINDOW.scan_and_fill()


class WatchPanel(Panel):
    title = "③ IMU 队列监控"
    desc = "实时盯录制队列水位与各数据流频率。掉线前水位会先冲高，这里是抓现行的地方。"

    def build_controls(self) -> None:
        g = self.ctrl_layout
        g.addWidget(QLabel("时长（秒）"), 0, 0)
        self.secs = QDoubleSpinBox()
        self.secs.setRange(0, 3600)
        self.secs.setValue(60)
        self.secs.setDecimals(0)
        self.secs.setSpecialValueText("一直盯")
        g.addWidget(self.secs, 0, 1)

        g.addWidget(QLabel("采样间隔（秒）"), 0, 2)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 10)
        self.interval.setValue(1.0)
        self.interval.setSingleStep(0.5)
        g.addWidget(self.interval, 0, 3)
        g.setColumnStretch(1, 1)

    def build_args(self) -> list[str]:
        args = ["imu_watch.py", "--interval", str(self.interval.value()),
                "--seconds", str(self.secs.value())]
        h = WINDOW.device_ip()
        if h:
            args += ["--host", h]
        return args


class SerialPanel(Panel):
    title = "④ 串口探测"
    desc = ("直接读头环 IMU 串口，统计真实字节率与帧率。用来判断设备实际输出频率是不是"
            "远超配置值（这是采集掉线的根源之一）。")

    def build_controls(self) -> None:
        g = self.ctrl_layout
        g.addWidget(QLabel("串口"), 0, 0)
        self.dev = QLineEdit("/dev/ttyACM0")
        g.addWidget(self.dev, 0, 1)

        g.addWidget(QLabel("采样秒数"), 0, 2)
        self.secs = QDoubleSpinBox()
        self.secs.setRange(1, 120)
        self.secs.setValue(3)
        g.addWidget(self.secs, 0, 3)
        g.setColumnStretch(1, 1)

    def build_args(self) -> list[str]:
        args = ["imu_serial_probe.py", "--device", self.dev.text().strip() or "/dev/ttyACM0",
                "--seconds", str(self.secs.value())]
        h = WINDOW.device_ip()
        if h:
            args += ["--host", h]
        return args


class BatteryPanel(Panel):
    title = "⑤ 电池采样"
    desc = ("连续采样电池电压/电流/电量，结束时判定充不满的根因"
            "（电量计未校准 / 充不进去 / 净放电）。")

    def build_controls(self) -> None:
        g = self.ctrl_layout
        g.addWidget(QLabel("时长（分钟）"), 0, 0)
        self.mins = QDoubleSpinBox()
        self.mins.setRange(0, 1440)
        self.mins.setValue(60)
        self.mins.setSpecialValueText("一直跑")
        g.addWidget(self.mins, 0, 1)

        g.addWidget(QLabel("采样间隔（秒）"), 0, 2)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(6, 600)
        self.interval.setValue(30)
        g.addWidget(self.interval, 0, 3)

        self.csv = QCheckBox("同时存 CSV 到 out/")
        self.csv.setChecked(True)
        g.addWidget(self.csv, 1, 0, 1, 2)
        g.setColumnStretch(1, 1)

    def build_args(self) -> list[str]:
        args = ["battery_watch.py", "--interval", str(self.interval.value()),
                "--minutes", str(self.mins.value())]
        h = WINDOW.device_ip()
        if h:
            args += ["--host", h]
        if self.csv.isChecked():
            EXPORT_DIR.mkdir(exist_ok=True)
            args += ["--csv", str(EXPORT_DIR / "battery.csv")]
        return args


class SrcPanel(Panel):
    title = "⑥ 源码查看"
    desc = ("不装 docker、不要 sudo，直接读设备上正在跑的容器源码。"
            "升级前后对比关键代码，可当验收依据。")

    def build_controls(self) -> None:
        g = self.ctrl_layout
        g.addWidget(QLabel("动作"), 0, 0)
        self.action = QComboBox()
        for k, v in [("版本与路径信息", "info"), ("列目录", "ls"),
                     ("全仓搜索关键字", "grep"), ("读文件片段", "cat"),
                     ("按名字找文件", "find")]:
            self.action.addItem(k, v)
        self.action.currentIndexChanged.connect(self._sync)
        g.addWidget(self.action, 0, 1)

        self.lbl_target = QLabel("关键字")
        g.addWidget(self.lbl_target, 0, 2)
        self.target = QLineEdit()
        self.target.setPlaceholderText("如 _queue_overflow_active")
        g.addWidget(self.target, 0, 3)

        self.lbl_lines = QLabel("行范围")
        self.lines = QLineEdit()
        self.lines.setPlaceholderText("如 280,300")
        g.addWidget(self.lbl_lines, 1, 2)
        g.addWidget(self.lines, 1, 3)

        self.lines.setVisible(False)
        self.lbl_lines.setVisible(False)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)

    def _sync(self) -> None:
        v = self.action.currentData()
        self.lbl_target.setText({"cat": "文件路径", "find": "文件名匹配"}.get(v, "关键字"))
        show_lines = v == "cat"
        self.lines.setVisible(show_lines)
        self.lbl_lines.setVisible(show_lines)

    def build_args(self) -> list[str]:
        v = self.action.currentData()
        args = ["container_src.py", v]
        t = self.target.text().strip()
        if t:
            args.append(t)
        if v == "cat" and self.lines.text().strip():
            args += ["--lines", self.lines.text().strip()]
        h = WINDOW.device_ip()
        if h:
            args += ["--host", h]
        return args


class LogPanel(Panel):
    title = "⑦ 运行日志"
    desc = "探测设备上日志都放在哪（systemd / 应用目录 / 容器 stdout），并列出最近内容。"

    def build_controls(self) -> None:
        hint = QLabel("无参数，直接点开始。探测需要 SSH（ls / ls）。")
        hint.setStyleSheet("color:#8b93a1;font-size:12px")
        self.ctrl_layout.addWidget(hint, 0, 0)

    def build_args(self) -> list[str]:
        args = ["_probe_logs.py"]
        h = WINDOW.device_ip()
        if h:
            args.append(h)
        return args


class ExportPanel(QWidget):
    title = "⑧ 报告与文件"

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)
        h = QLabel("报告与文件")
        h.setStyleSheet("font-size:17px;font-weight:600;color:#14181f")
        root.addWidget(h)
        d = QLabel("脚本生成的 HTML / Excel 报告、电池 CSV 都存在这里，可直接发给别人。")
        d.setStyleSheet("color:#5b6472;font-size:12px")
        root.addWidget(d)

        row = QHBoxLayout()
        b1 = QPushButton("打开 out 目录")
        b2 = QPushButton("打开脚本目录")
        for b in (b1, b2):
            b.setFixedHeight(34)
            b.setStyleSheet(
                "QPushButton{background:#1f6feb;color:#fff;border:none;border-radius:6px;"
                "font-size:14px;font-weight:600;padding:0 18px}"
                "QPushButton:hover{background:#1a63d4}"
            )
        b1.clicked.connect(lambda: open_path(EXPORT_DIR))
        b2.clicked.connect(lambda: open_path(BASE))
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        root.addLayout(row)

        self.listbox = QPlainTextEdit()
        self.listbox.setReadOnly(True)
        self.listbox.setFont(QFont(MONO, 9))
        self.listbox.setStyleSheet(
            "QPlainTextEdit{background:#0f1419;color:#d7dde5;border:1px solid #263041;"
            "border-radius:8px;padding:10px}"
        )
        root.addWidget(self.listbox, 1)
        self.refresh()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.refresh()

    def refresh(self) -> None:
        EXPORT_DIR.mkdir(exist_ok=True)
        files = sorted(EXPORT_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            self.listbox.setPlainText("（out 目录还是空的，跑一次体检或电池采样就会生成）")
            return
        lines = [f"{'时间':<18} {'大小':>10}  文件" for _ in [0]]
        lines.append("-" * 66)
        for f in files[:200]:
            import datetime
            ts = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            kb = f.stat().st_size / 1024
            lines.append(f"{ts:<18} {kb:>9.1f}K  {f.name}")
        self.listbox.setPlainText("\n".join(lines))


def open_path(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(p))  # noqa: S606  (Windows)
    except Exception:  # noqa: BLE001
        QMessageBox.information(None, "路径", str(p))


# ================================================================== 主窗口

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ego-Std 交付测试台")
        self.resize(1180, 760)
        self.setMinimumSize(940, 620)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setFixedWidth(178)
        self.nav.setStyleSheet(
            "QListWidget{background:#f6f7f9;border:none;border-right:1px solid #e3e6ea;"
            "outline:none;padding:8px 0}"
            "QListWidget::item{height:40px;padding-left:16px;color:#3c4553;font-size:14px}"
            "QListWidget::item:selected{background:#e4eefc;color:#1a5fd0;"
            "border-left:3px solid #1f6feb;font-weight:600}"
            "QListWidget::item:hover{background:#eef1f5}"
        )
        body.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:#ffffff")
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        self.panels = [CheckupPanel(), FindPanel(), WatchPanel(), SerialPanel(),
                       BatteryPanel(), SrcPanel(), LogPanel(), ExportPanel()]
        for p in self.panels:
            title = getattr(p, "title", "")
            it = QListWidgetItem(title)
            self.nav.addItem(it)
            self.stack.addWidget(p)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        sb = QStatusBar()
        sb.setStyleSheet("QStatusBar{background:#f6f7f9;border-top:1px solid #e3e6ea;"
                         "color:#5b6472;font-size:12px}")
        sb.showMessage("就绪")
        self.setStatusBar(sb)

        self.refresh_device_status()

    def _build_header(self) -> QWidget:
        w = QFrame()
        w.setFixedHeight(66)
        w.setStyleSheet("QFrame{background:#ffffff;border-bottom:1px solid #e3e6ea}")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(12)

        name = QLabel("Ego-Std 交付测试台")
        name.setStyleSheet("font-size:16px;font-weight:600;color:#14181f")
        lay.addWidget(name)
        lay.addSpacing(16)

        lay.addWidget(QLabel("设备"))
        self.ip = QLineEdit(load_device_ip())
        self.ip.setPlaceholderText("设备 IP，留空则自动查找")
        self.ip.setFixedWidth(170)
        self.ip.setFixedHeight(32)
        self.ip.setStyleSheet(
            "QLineEdit{border:1px solid #d9dee6;border-radius:6px;padding:0 10px;"
            "font-size:13px;background:#fff}"
            "QLineEdit:focus{border-color:#1f6feb}"
        )
        self.ip.editingFinished.connect(self.refresh_device_status)
        lay.addWidget(self.ip)

        btn_find = QPushButton("自动查找")
        btn_find.setFixedHeight(32)
        btn_find.setStyleSheet(
            "QPushButton{background:#fff;color:#1a5fd0;border:1px solid #b9d2f7;"
            "border-radius:6px;font-size:13px;padding:0 14px}"
            "QPushButton:hover{background:#eef5ff}"
        )
        btn_find.clicked.connect(self.auto_find)
        lay.addWidget(btn_find)

        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color:{C_SKIP};font-size:15px")
        lay.addWidget(self.dot)
        self.host_state = QLabel("未连接")
        self.host_state.setStyleSheet("color:#6b7280;font-size:12px")
        lay.addWidget(self.host_state)

        lay.addStretch(1)
        return w

    # --- 设备状态 ---------------------------------------------------
    def device_ip(self) -> str:
        return self.ip.text().strip()

    def refresh_device_status(self) -> None:
        ip = self.device_ip()
        if not ip:
            self.dot.setStyleSheet(f"color:{C_SKIP};font-size:15px")
            self.host_state.setText("未设置设备")
            return
        ok = probe_host(ip)
        self.dot.setStyleSheet(f"color:{C_OK if ok else C_BAD};font-size:15px")
        self.host_state.setText("在线" if ok else "连不上")
        self.host_state.setStyleSheet(
            f"color:{C_OK if ok else C_BAD};font-size:12px;font-weight:600"
        )
        save_device_ip(ip)

    def auto_find(self) -> None:
        self.statusBar().showMessage("正在扫描网段…")
        QApplication.processEvents()
        code, out = run_sync(["find_device.py", "--quiet", "--timeout", "0.3"])
        ips = [l.strip() for l in out.splitlines() if l.strip() and re.match(r"^\d+\.\d+\.\d+\.\d+$", l.strip())]
        if ips:
            self.ip.setText(ips[0])
            save_device_ip(ips[0])
            self.refresh_device_status()
            self.statusBar().showMessage(f"找到 {len(ips)} 台：{', '.join(ips)}")
        else:
            self.refresh_device_status()
            QMessageBox.information(
                self, "没找到设备",
                "当前网段没扫到 ego 设备。\n\n"
                "检查：\n"
                "· 设备和电脑是否在同一个 WiFi / 网段\n"
                "· 设备是否已开机\n\n"
                "也可以在「② 设备发现」里指定额外网段再扫。"
            )

    def scan_and_fill(self) -> None:
        code, out = run_sync(["find_device.py", "--quiet", "--timeout", "0.3"])
        ips = [l.strip() for l in out.splitlines()
               if l.strip() and re.match(r"^\d+\.\d+\.\d+\.\d+$", l.strip())]
        if ips:
            self.ip.setText(ips[0])
            save_device_ip(ips[0])
            self.refresh_device_status()


def run_sync(args: list[str], timeout: int = 60000):
    """同步跑一个脚本，返回 (退出码, 合并输出)。仅用于短任务。"""
    from PySide6.QtCore import QProcess as _QP
    p = _QP()
    env = QProcessEnvironment.systemEnvironment()
    env.insert("PYTHONIOENCODING", "utf-8")
    env.insert("PYTHONUTF8", "1")
    p.setProcessEnvironment(env)
    p.setWorkingDirectory(str(BASE))
    p.setProcessChannelMode(_QP.MergedChannels)
    p.start(PYEXE, args)
    p.waitForFinished(timeout)
    out = bytes(p.readAllStandardOutput()).decode("utf-8", "replace")
    return p.exitCode(), out


# ------------------------------------------------------------------ 截图模式

def screenshot(path: str) -> None:
    """逐个渲染各功能页并保存 PNG（用于文档/预览，也能在无人工干预下验证每个页面能正常绘制）。"""
    global WINDOW
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    w = MainWindow()
    WINDOW = w
    w.resize(1180, 760)
    w.show()
    app.processEvents()

    p = Path(path)
    stem, suffix = p.stem, p.suffix or ".png"
    p.parent.mkdir(parents=True, exist_ok=True)

    saved = []
    for i in range(w.stack.count()):
        w.nav.setCurrentRow(i)
        app.processEvents()
        pm = QPixmap(w.size())
        w.render(pm)
        out = p.parent / f"{stem}_{i + 1}{suffix}"
        pm.save(str(out))
        saved.append(out)
    print(f"已保存 {len(saved)} 张截图：")
    for s in saved:
        print("  ", s)
    w.close()


WINDOW: MainWindow | None = None


def main() -> None:
    global WINDOW
    if "--screenshot" in sys.argv:
        i = sys.argv.index("--screenshot")
        path = sys.argv[i + 1] if len(sys.argv) > i + 1 else "preview.png"
        screenshot(path)
        return

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    w = MainWindow()
    WINDOW = w
    w.show()
    if w.device_ip():
        from PySide6.QtCore import QTimer
        QTimer.singleShot(300, w.refresh_device_status)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
