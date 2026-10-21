"""APP patrol panel, deliberately independent from the Ego network device IP."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QPushButton, QComboBox, QCheckBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QPlainTextEdit, QProgressBar, QFileDialog, QMessageBox,
    QDialog, QFormLayout, QDialogButtonBox, QSpinBox)

from app_patrol_engine import BASE, RUNS, PREFIX, manual_session, write_session


def default_framework():
    bundled = BASE/'app-test-framework'
    if (bundled/'.venv/Scripts/python.exe').is_file():
        return bundled
    legacy = Path('D:/app-test-framework')
    return legacy if (legacy/'.venv/Scripts/python.exe').is_file() else bundled


class AppPatrolPanel(QWidget):
    title = 'APP 巡检'

    def __init__(self):
        super().__init__()
        self.session = None
        self.read_only = False
        self.cancel_file = None
        self._buffer = b''
        self._error = ''
        self._busy = False
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._process_error)
        self.discovery = QProcess(self)
        self.discovery.finished.connect(self._devices_ready)
        self.discovery.errorOccurred.connect(self._discovery_error)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        title = QLabel('APP 巡检')
        title.setStyleSheet('font-size:17px;font-weight:600;color:#14181f')
        root.addWidget(title)
        desc = QLabel('全量巡检一键运行框架与设备流程；登录、换卡时按提示继续。Android 手机在本页选择。')
        desc.setWordWrap(True)
        root.addWidget(desc)
        grid = QGridLayout()
        self.framework = QLineEdit(str(default_framework()))
        browse = QPushButton('选择框架')
        self.browse = browse
        browse.clicked.connect(self._browse)
        grid.addWidget(QLabel('测试框架'), 0, 0)
        grid.addWidget(self.framework, 0, 1)
        grid.addWidget(browse, 0, 2)
        self.devices = QComboBox()
        self.devices.setEditable(True)
        self.devices.setPlaceholderText('刷新后选择手机，或输入明确的序列号')
        self.devices.setMinimumWidth(220)
        self.refresh = QPushButton('刷新手机')
        self.refresh.clicked.connect(self.refresh_devices)
        grid.addWidget(QLabel('Android 手机'), 1, 0)
        grid.addWidget(self.devices, 1, 1)
        grid.addWidget(self.refresh, 1, 2)
        self.mode = QComboBox()
        self.mode.addItem('框架自检 · 3 项，不操作手机', 'selfcheck')
        self.mode.addItem('LivFlow 基础巡检 · 5 项，会重启 APP', 'smoke')
        self.mode.addItem('LivFlow 预览检查 · 3 项，只读当前页面', 'preview')
        self.mode.addItem('全量自动巡检 · 3 项框架 + 9 项设备流程', 'flow_full')
        for label,value in [('任务搜索、筛选、详情与统计','flow_tasks'),('语言、版本与日志导出','flow_settings'),
            ('指定 EGO 连接、配网与预览','flow_connect'),('IMU 参数写入与恢复','flow_parameters'),
            ('实际短采集与后台返回','flow_record'),('测试文件写入、上传与防重复','flow_upload'),
            ('扩展流程巡检（按下方勾选允许实际操作）','flow_all'),('本地 MCAP 文件校验','files')]:
            self.mode.addItem(label,value)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        grid.addWidget(QLabel('自动检查'), 2, 0)
        grid.addWidget(self.mode, 2, 1, 1, 2)
        root.addLayout(grid)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet('color:#5b6472')
        root.addWidget(self.hint)
        self.idle = QCheckBox('已确认 APP 没有正在采集或上传，可以重启进行基础巡检')
        root.addWidget(self.idle)
        permissions = QHBoxLayout()
        self.allow_parameters = QCheckBox('允许临时修改 IMU 并恢复')
        self.allow_record = QCheckBox('允许实际短采集')
        self.allow_upload = QCheckBox('允许备份、改写测试 MCAP 并上传')
        for widget in (self.allow_parameters,self.allow_record,self.allow_upload):
            permissions.addWidget(widget)
        root.addLayout(permissions)
        self.flow_config_path = BASE/'out/app-patrol/flow-config.json'
        buttons = QHBoxLayout()
        self.full = QPushButton('全量巡检')
        self.full.setStyleSheet('background:#173c68;color:white;border-radius:5px;padding:6px 16px;font-weight:600')
        self.full.setToolTip('运行3项框架检查和9项设备流程。实际操作按下方勾选；未允许的项目记为未测。')
        self.full.clicked.connect(self.start_full_run)
        root.addWidget(self.full)
        self.start = QPushButton('执行自动检查')
        self.start.setStyleSheet('background:#1f6feb;color:white;border-radius:5px;padding:6px 12px')
        self.start.clicked.connect(self.start_run)
        self.stop = QPushButton('停止本次检查')
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.stop_run)
        self.new_manual = QPushButton('新建完整巡检清单')
        self.new_manual.clicked.connect(self.create_manual)
        self.open_report = QPushButton('打开本轮报告')
        self.open_report.setEnabled(False)
        self.open_report.clicked.connect(self.show_report)
        self.configure = QPushButton('流程配置')
        self.configure.clicked.connect(self.configure_flow)
        self.resume = QPushButton('已完成，继续')
        self.resume.setEnabled(False)
        self.resume.clicked.connect(self.resume_flow)
        for w in (self.start, self.stop, self.configure, self.resume, self.new_manual, self.open_report):
            buttons.addWidget(w)
        root.addLayout(buttons)
        history_row = QHBoxLayout()
        self.history = QComboBox()
        self.history.setMinimumWidth(270)
        self.history.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.load = QPushButton('加载记录')
        self.load.clicked.connect(self.load_history)
        self.rescan = QPushButton('刷新记录')
        self.rescan.clicked.connect(self.refresh_history)
        history_row.addWidget(QLabel('历史巡检'))
        history_row.addWidget(self.history, 1)
        history_row.addWidget(self.load)
        history_row.addWidget(self.rescan)
        root.addLayout(history_row)
        self.summary = QLabel('尚未执行 · 自动检查和完整清单分别记录，不复用历史通过结果')
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet('background:#eef4fc;border-radius:6px;padding:8px;color:#244b82')
        root.addWidget(self.summary)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        root.addWidget(self.progress)
        self.table = QTableWidget(0, 4)
        self.table.setMinimumHeight(180)
        self.table.setHorizontalHeaderLabels(['模块', '检查项', '结果', '记录'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._selection)
        root.addWidget(self.table, 3)
        edit = QHBoxLayout()
        self.status = QComboBox()
        self.status.addItems(['未执行', '通过', '失败', '待确认', '未测'])
        self.note = QLineEdit()
        self.note.setPlaceholderText('选中清单项目后填写实际结果；不要填写账号密码')
        self.save = QPushButton('保存本项')
        self.save.clicked.connect(self.save_case)
        edit.addWidget(self.status)
        edit.addWidget(self.note, 1)
        edit.addWidget(self.save)
        root.addLayout(edit)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log.setMaximumHeight(60)
        self.log.setPlaceholderText('运行信息；历史巡检可加载查看，新建清单全部从未执行开始。')
        root.addWidget(self.log)
        self._mode_changed()
        self.refresh_history()
        self._enable_edit(False)

    @property
    def busy(self):
        return self._busy

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, '选择 APP 测试框架', self.framework.text())
        if folder:
            self.framework.setText(folder)
            self.refresh_history()

    def _mode_changed(self):
        mode = self.mode.currentData()
        self.idle.setVisible(mode == 'smoke' or mode.startswith('flow_'))
        self.idle.setText('已确认没有正在采集或上传，允许执行本轮已选择的操作' if mode.startswith('flow_') else
                          '已确认 APP 没有正在采集或上传，可以重启进行基础巡检')
        for widget,specific in ((self.allow_parameters,'flow_parameters'),(self.allow_record,'flow_record'),(self.allow_upload,'flow_upload')):
            widget.setVisible(mode in (specific,'flow_all','flow_full'))
        self.hint.setText({'selfcheck': '检查配置校验逻辑，不连接或操作手机。',
            'smoke': '前提：LivFlow 已安装、已退出账号、界面语言为中文。会重启 APP，检查空表单、游客导航和语言切换。',
            'preview': '前提：手机与设备已接入无密码“灵生测试”Wi-Fi，LivFlow 正在中文双目预览页。不会自动连接、配网或开始采集。',
            'files':'在流程配置选择本地 MCAP；可选择写入前文件作逐消息及元数据对比，不操作手机。'
            ,'flow_full':'全量范围：3项框架 + 9项设备流程。未勾选的实际操作记未测。游客登录5项、独立预览3项需另选模式；65项人工清单另行记录。先核对流程配置，登录或换卡时按提示继续。'
            }.get(mode,'先点“流程配置”指定任务编号、EGO序列号等。需要登录、换网络或插卡时会等待，完成后点“已完成，继续”。不执行固件升级和删除。'))

    def configure_flow(self):
        default = Path(self.framework.text().strip())/'config/patrol.json'
        path = self.flow_config_path if self.flow_config_path.is_file() else default
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            config = {}
        dialog = QDialog(self)
        dialog.setWindowTitle('本轮流程配置')
        form = QFormLayout(dialog)
        controls = {}
        fields = [('task_id','测试任务编号'),('device_sn','指定 EGO 序列号'),('ssid','测试 Wi-Fi 名称'),
                  ('recording_dir','手机 TF 卡测试目录（可选）'),('mcap_file','本地 MCAP 文件（文件校验用）'),
                  ('before_mcap','写入前 MCAP 文件（可选对比）')]
        for key,label in fields:
            entry = QLineEdit(str(config.get(key,'灵生测试' if key=='ssid' else '')))
            entry.setMinimumWidth(380)
            controls[key]=entry
            if key in ('mcap_file','before_mcap'):
                row = QHBoxLayout()
                row.addWidget(entry)
                choose = QPushButton('选择文件')
                def select_file(_checked=False, target=entry):
                    file,_ = QFileDialog.getOpenFileName(dialog,'选择 MCAP',target.text(),'MCAP (*.mcap)')
                    if file:
                        target.setText(file)
                choose.clicked.connect(select_file)
                row.addWidget(choose)
                form.addRow(label,row)
            else:
                form.addRow(label,entry)
        for key,label,lo,hi,value in [('record_seconds','短采集秒数',5,120,15),('background_seconds','切后台秒数',0,30,5)]:
            entry = QSpinBox()
            entry.setRange(lo,hi)
            entry.setValue(int(config.get(key,value)))
            controls[key]=entry
            form.addRow(label,entry)
        note = QLabel('账号密码只在手机输入。本页不保存密码。上传只处理你明确选择的一条测试记录，改写前自动备份。')
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            for key,entry in controls.items():
                config[key]=entry.value() if isinstance(entry,QSpinBox) else entry.text().strip()
            config.setdefault('human_timeout',900)
            config.setdefault('wait_timeout',90)
            self.flow_config_path.parent.mkdir(parents=True,exist_ok=True)
            self.flow_config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
            self.log.appendPlainText('本轮流程配置已保存（不含账号密码）')

    def resume_flow(self):
        if self.session and self.session.get('waiting') and self.busy:
            target = Path(self.session['folder'])/'resume.json'
            temp = target.with_suffix('.tmp')
            temp.write_text(json.dumps({'gate':self.session['waiting']['gate']}),encoding='utf-8')
            temp.replace(target)
            self.resume.setEnabled(False)

    def refresh_devices(self):
        if self.discovery.state() != QProcess.NotRunning:
            return
        adb = shutil.which('adb') or 'D:/Android/Sdk/platform-tools/adb.exe'
        self.refresh.setEnabled(False)
        self.discovery.start(adb, ['devices', '-l'])

    def _discovery_error(self, error):
        self.refresh.setEnabled(not self.busy)
        if error == QProcess.FailedToStart:
            self.log.appendPlainText('找不到 ADB；请安装 Android SDK 或将 adb 加入 PATH。')

    def _devices_ready(self, code, _status):
        self.refresh.setEnabled(not self.busy)
        current = self.selected_serial()
        self.devices.clear()
        text = bytes(self.discovery.readAllStandardOutput()).decode('utf-8', errors='replace')
        found = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                model = next((x[6:] for x in parts if x.startswith('model:')), 'Android')
                self.devices.addItem(f'{parts[0]} · {model}', parts[0])
                found.append(parts[0])
        if current in found:
            self.devices.setCurrentIndex(found.index(current))
        if code or not found:
            self.log.appendPlainText('未发现已授权手机，请连接 USB 并允许 USB 调试。')

    def selected_serial(self):
        text = self.devices.currentText().strip()
        data = self.devices.currentData()
        return data if data and text == self.devices.itemText(self.devices.currentIndex()) else text

    def _controls(self, busy):
        self._busy = busy
        for w in (self.start, self.full, self.new_manual, self.load, self.framework, self.mode, self.devices, self.refresh, self.rescan, self.browse, self.history,self.configure,self.allow_parameters,self.allow_record,self.allow_upload):
            w.setEnabled(not busy)
        self.stop.setEnabled(busy)
        self.idle.setEnabled(not busy)
        self._enable_edit(not busy and self.session is not None and not self.read_only and self.session['mode'] == 'manual')

    def start_full_run(self):
        if self.busy:
            return
        self.mode.setCurrentIndex(self.mode.findData('flow_full'))
        self.start_run()

    def start_run(self):
        if self.busy:
            return
        root = Path(self.framework.text().strip()).resolve()
        if not (root / '.venv/Scripts/python.exe').is_file():
            QMessageBox.warning(self, '框架路径无效', '请选择包含 .venv 的 app-test-framework 文件夹。')
            return
        mode = self.mode.currentData()
        for key,widget,label in (('flow_parameters',self.allow_parameters,'允许临时修改 IMU 并恢复'),
                                  ('flow_record',self.allow_record,'允许实际短采集'),
                                  ('flow_upload',self.allow_upload,'允许备份、改写测试 MCAP 并上传')):
            if mode==key and not widget.isChecked():
                QMessageBox.information(self,'本流程尚未允许实际操作','请确认测试条件并勾选“'+label+'”后执行。')
                return
        if (mode == 'smoke' or mode.startswith('flow_')) and not self.idle.isChecked():
            QMessageBox.warning(self, '请确认手机空闲', '本轮会控制 LivFlow 页面。请结束已有采集和上传后勾选确认。')
            return
        config_path = self.flow_config_path if self.flow_config_path.is_file() else root/'config/patrol.json'
        if mode.startswith('flow_') or mode=='files':
            try:
                settings = json.loads(config_path.read_text(encoding='utf-8'))
            except (OSError,ValueError):
                QMessageBox.warning(self,'缺少流程配置','请点击“流程配置”填写本轮参数。')
                return
            required = []
            if mode in ('flow_tasks','flow_record','flow_upload','flow_all','flow_full'):
                required.append(('task_id','测试任务编号'))
            if mode in ('flow_connect','flow_parameters','flow_record','flow_all','flow_full'):
                required.append(('device_sn','指定 EGO 序列号'))
            if mode=='files':
                required.append(('mcap_file','本地 MCAP 文件'))
            missing = [label for key,label in required if not settings.get(key)]
            if missing:
                QMessageBox.warning(self,'请补全流程配置','请在流程配置填写：'+ '、'.join(missing))
                return
            if mode == 'flow_full' and self.allow_record.isChecked() and self.allow_upload.isChecked() and settings.get('recording_dir'):
                QMessageBox.warning(self,'需要选择本轮新记录','请先在流程配置清空“手机 TF 卡测试目录”。全量巡检会产生新录制，运行到上传步骤时再选择本轮新目录，避免上传旧记录。')
                return
        serial = self.selected_serial() if mode not in ('selfcheck','files') else ''
        if mode not in ('selfcheck','files') and not serial:
            QMessageBox.warning(self, '没有选择手机', '请刷新并选择手机，或输入明确的序列号。')
            return
        RUNS.mkdir(parents=True, exist_ok=True)
        self.cancel_file = RUNS / ('cancel_' + uuid4().hex)
        self.session = None
        self.read_only = False
        self.table.setRowCount(0)
        self.log.clear()
        self._buffer = b''
        self._error = ''
        self.open_report.setEnabled(False)
        self.progress.setRange(0, 0)
        self.summary.setText('正在启动本轮检查…')
        self._controls(True)
        env = QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONIOENCODING', 'utf-8')
        env.insert('PYTHONUTF8', '1')
        env.remove('PYTHONPATH')
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(str(BASE))
        args = [str(BASE / 'app_patrol_engine.py'),'--framework',str(root),'--mode',mode,'--serial',serial,
                '--cancel-file',str(self.cancel_file)]
        config = self.flow_config_path if self.flow_config_path.is_file() else root/'config/patrol.json'
        if mode.startswith('flow_') or mode == 'files':
            args += ['--flow-config',str(config)]
        for widget,flag,specific in ((self.allow_parameters,'--allow-parameters','flow_parameters'),(self.allow_record,'--allow-record','flow_record'),(self.allow_upload,'--allow-upload','flow_upload')):
            if widget.isChecked() and mode in (specific,'flow_all','flow_full'):
                args.append(flag)
        self.resume.setEnabled(False)
        self.proc.start(sys.executable,args)

    def stop_run(self):
        if self.busy and self.cancel_file:
            self.cancel_file.write_text('cancel', encoding='utf-8')
            self.stop.setEnabled(False)
            self.summary.setText('正在停止本轮检查并保存已有结果…')

    def _read(self):
        self._buffer += bytes(self.proc.readAllStandardOutput())
        while b'\n' in self._buffer:
            raw, self._buffer = self._buffer.split(b'\n', 1)
            self._line(raw.decode('utf-8', errors='replace').rstrip())

    def _line(self, line):
        if not line.startswith(PREFIX):
            self.log.appendPlainText(line)
            return
        try:
            event = json.loads(line[len(PREFIX):])
        except ValueError:
            self.log.appendPlainText(line)
            return
        kind = event.get('type')
        if kind in ('session', 'update', 'finished'):
            self.session = event['session']
            self._render()
        elif kind == 'log':
            self.log.appendPlainText(event['message'])
        elif kind == 'error':
            self._error = event['message']
            self.log.appendPlainText('未能执行：' + self._error)

    def _finished(self, code, _status):
        self._read()
        if self._buffer:
            self._line(self._buffer.decode('utf-8', errors='replace').strip())
            self._buffer = b''
        if self.session and self.session['state'] in ('运行中','等待人工操作'):
            self.session['state'] = '执行异常中断'
            for row in self.session['cases']:
                if row['status'] == '未执行':
                    row.update(status='未测', detail='进程异常中断，尚未执行')
            write_session(self.session)
        self._controls(False)
        self.resume.setEnabled(False)
        if self.session:
            self._render()
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.summary.setText('未能执行：' + (self._error or f'进程退出码 {code}，请查看运行信息'))
        if self.cancel_file:
            self.cancel_file.unlink(missing_ok=True)
        self.refresh_history()
        if self.session:
            index = self.history.findData(str(Path(self.session['folder']) / 'session.json'))
            if index >= 0:
                self.history.setCurrentIndex(index)

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            self._error = '执行进程未能启动'
            self._finished(-1, None)

    def create_manual(self):
        self.session = manual_session(self.framework.text().strip())
        self.session['serial'] = self.selected_serial()
        self.read_only = False
        write_session(self.session)
        self._render()
        self._enable_edit(True)
        self.refresh_history()
        self.log.appendPlainText('新建完整清单：全部为未执行。实际采集、上传等步骤需按本轮环境执行后手工记录。')

    def _render(self):
        if not self.session:
            return
        selected = self.table.currentRow()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.session['cases']))
        colors = {'通过': '#1a7f37', '失败': '#c62828', '待确认': '#b26a00', '未测': '#6b7280', '未执行': '#6b7280'}
        for i, row in enumerate(self.session['cases']):
            for j, key in enumerate(('module', 'title', 'status', 'detail')):
                item = QTableWidgetItem(row.get(key, ''))
                item.setToolTip(row.get(key, ''))
                if key == 'status':
                    item.setForeground(QColor(colors.get(row[key], '#6b7280')))
                self.table.setItem(i, j, item)
        self.table.blockSignals(False)
        if 0 <= selected < self.table.rowCount():
            self.table.selectRow(selected)
            self._selection()
        else:
            self.note.clear()
            self.status.setCurrentText('未执行')
        counts = {}
        for row in self.session['cases']:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        total = len(self.session['cases'])
        done = total - counts.get('未执行', 0)
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        text = ' · '.join(f'{key} {counts[key]}' for key in ('通过', '失败', '待确认', '未测', '未执行') if counts.get(key))
        label = '历史结果（只读）' if self.read_only else self.session['state']
        self.summary.setText(f'{label} · 已记录 {done}/{total} 项 · {text}')
        waiting = self.session.get('waiting')
        self.resume.setEnabled(bool(waiting) and self.busy)
        if waiting:
            self.summary.setText('等待人工操作：'+waiting['message'])
        self.open_report.setEnabled(bool(self.session.get('report')))
        self._enable_edit(not self.busy and not self.read_only and self.session['mode'] == 'manual')

    def _enable_edit(self, enabled):
        for w in (self.status, self.note, self.save):
            w.setEnabled(enabled)

    def _selection(self):
        row = self.table.currentRow()
        if self.session and 0 <= row < len(self.session['cases']):
            item = self.session['cases'][row]
            self.status.setCurrentText(item['status'])
            self.note.setText(item.get('detail', ''))
            scope = item.get('scope', '')
            self.note.setToolTip('历史参考范围（不代表本轮结果）：' + scope if scope else '')

    def save_case(self):
        i = self.table.currentRow()
        if self.busy or self.read_only or not self.session or self.session['mode'] != 'manual' or i < 0:
            return
        row = self.session['cases'][i]
        row.update(status=self.status.currentText(), detail=self.note.text().strip())
        pending = any(x['status'] == '未执行' for x in self.session['cases'])
        self.session['state'] = '人工巡检进行中' if pending else '人工巡检已记录'
        write_session(self.session)
        self._render()
        self.table.selectRow(i)

    def refresh_history(self):
        current = self.history.currentData()
        self.history.clear()
        if RUNS.is_dir():
            paths = sorted(RUNS.glob('*/session.json'), reverse=True)
            for path in paths[:100]:
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    label = '完整清单' if data['mode'] == 'manual' else '自动检查'
                    self.history.addItem(f"{data['created']} · {label} · {data['state']}", str(path))
                except (OSError, ValueError, KeyError):
                    continue
        original = Path(self.framework.text().strip()) / 'reports/livflow-inspection-20261021/巡检清单.json'
        if original.is_file():
            self.history.addItem('2026-10-21 · LivFlow 完整巡检原始记录（只读）', str(original))
        idx = self.history.findData(current)
        if idx >= 0:
            self.history.setCurrentIndex(idx)

    def load_history(self):
        path = self.history.currentData()
        if not path or self.busy:
            return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            if 'schema' in data and 'folder' in data:
                # Use the selected session's actual location, never a JSON-supplied write destination.
                data['folder'] = str(Path(path).resolve().parent)
                data['report'] = str(Path(path).resolve().parent / 'summary.html')
                self.read_only = data['mode'] != 'manual'
            else:
                data = dict(mode='history', state='历史巡检', cases=[dict(x, detail=x.get('actual', '')) for x in data['cases']],
                            report=str(Path(path).parent / '巡检报告.html'))
                self.read_only = True
            self.session = data
            self._render()
            self.log.appendPlainText('已加载：' + str(path))
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, '读取记录失败', str(exc))

    def show_report(self):
        if self.session and self.session.get('report'):
            path = Path(self.session['report'])
            if path.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            else:
                QMessageBox.information(self, '报告尚未生成', '等待本轮启动完成后再打开报告。')
