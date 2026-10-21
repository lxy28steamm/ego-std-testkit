"""Orange Pi inspection UI. Authentication is memory-only and separate from Ego IP."""
import json
from pathlib import Path
import threading

from PySide6.QtCore import QThread, Signal, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLineEdit,QSpinBox,
    QPushButton,QLabel,QTableWidget,QTableWidgetItem,QHeaderView,QProgressBar,QFileDialog,
    QMessageBox,QScrollArea,QLayout,QFrame)

from app_patrol_engine import BASE
from orangepi_inspection import inspect, settings, UnknownHostKey


class InspectionWorker(QThread):
    updated=Signal(dict)
    done=Signal(dict)
    error=Signal(str)
    trust_required=Signal(str)

    def __init__(self,config,password,key_file,parent=None):
        super().__init__(parent)
        self.config=config
        self._password=password
        self.key_file=key_file
        self.cancel=threading.Event()

    def run(self):
        try:
            result=inspect(self.config,self._password,self.key_file,self.cancel,
                           lambda value:self.updated.emit(json.loads(json.dumps(value))))
            self.done.emit(result)
        except UnknownHostKey as exc:
            self.trust_required.emit(exc.fingerprint)
        except Exception as exc:
            self.error.emit('巡检未能完成：'+type(exc).__name__)
        finally:
            self._password=''


class OrangePiPanel(QWidget):
    title='香橙派巡检'

    def __init__(self):
        super().__init__()
        self.worker=None
        self.session=None
        self._trusted={}
        self._pending_fingerprint=''
        outer=QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content=QWidget()
        root=QVBoxLayout(content)
        root.setContentsMargins(18,16,18,14)
        root.setSpacing(10)
        root.setSizeConstraint(QLayout.SetMinimumSize)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        heading=QLabel('香橙派巡检')
        heading.setStyleSheet('font-size:17px;font-weight:600')
        root.addWidget(heading)
        note=QLabel('通过 SSH 只读检查 Linux 系统、资源、温度、存储、网络和服务。不会安装软件、修改配置、重启或操作引脚。此处地址独立于顶部 Ego 设备 IP。')
        note.setWordWrap(True)
        root.addWidget(note)
        form=QFormLayout()
        form.setVerticalSpacing(8)
        self.host=QLineEdit()
        self.host.setPlaceholderText('香橙派 IP 或主机名')
        self.port=QSpinBox()
        self.port.setRange(1,65535)
        self.port.setValue(22)
        self.username=QLineEdit('orangepi')
        self.password=QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText('只用于当前连接，不保存；SSH密钥登录可留空')
        self.key_file=QLineEdit()
        self.key_file.setPlaceholderText('可选：本机 SSH 私钥路径，不复制私钥')
        self.services=QLineEdit('ssh.service')
        self.services.setPlaceholderText('用逗号分隔，例如 ssh.service,my-app.service')
        for label,control in [('设备地址',self.host),('SSH端口',self.port),('SSH用户名',self.username),
                              ('SSH密码',self.password),('SSH私钥',self.key_file),('关注的服务',self.services)]:
            form.addRow(label,control)
        root.addLayout(form)
        limits=QHBoxLayout()
        self.temperature=QSpinBox()
        self.disk=QSpinBox()
        self.memory=QSpinBox()
        self.cpu=QSpinBox()
        for label,control,value,suffix in [('温度告警',self.temperature,80,' ℃'),('磁盘告警',self.disk,85,' %'),
                                         ('内存告警',self.memory,90,' %'),('CPU告警',self.cpu,95,' %')]:
            control.setRange(1,100)
            control.setValue(value)
            control.setSuffix(suffix)
            limits.addWidget(QLabel(label))
            limits.addWidget(control)
        root.addLayout(limits)
        buttons=QHBoxLayout()
        self.start=QPushButton('开始香橙派巡检')
        self.start.setStyleSheet('background:#173c68;color:white;border-radius:5px;padding:6px 12px')
        self.stop=QPushButton('停止巡检')
        self.report=QPushButton('打开本轮报告')
        self.history=QPushButton('加载历史记录')
        self.stop.setEnabled(False)
        self.report.setEnabled(False)
        self.start.clicked.connect(self.start_run)
        self.stop.clicked.connect(self.stop_run)
        self.report.clicked.connect(self.show_report)
        self.history.clicked.connect(self.load_history)
        for button in (self.start,self.stop,self.report,self.history):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self.summary=QLabel('尚未执行。摄像头、串口、GPIO及GPU/NPU功能需专项测试，不会仅凭节点存在判为通过。')
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        root.addWidget(self.summary)
        self.progress=QProgressBar()
        self.progress.setValue(0)
        root.addWidget(self.progress)
        self.table=QTableWidget(0,4)
        self.table.setMinimumHeight(200)
        self.table.setHorizontalHeaderLabels(['模块','检查项','结果','记录'])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(True)
        for column in (0,2):
            self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeToContents)
        for column in (1,3):
            self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.Stretch)
        root.addWidget(self.table,1)

    @property
    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def controls(self,busy):
        for widget in (self.start,self.host,self.port,self.username,self.password,self.key_file,self.services,
                       self.temperature,self.disk,self.memory,self.cpu,self.history):
            widget.setEnabled(not busy)
        self.stop.setEnabled(busy)

    def start_run(self):
        if self.busy:
            return
        try:
            target=(self.host.text().strip(),self.port.value())
            config=settings(target[0],self.username.text(),target[1],self.services.text(),
                            self.temperature.value(),self.disk.value(),self.memory.value(),self.cpu.value(),self._trusted.get(target,''))
            key=self.key_file.text().strip()
            if key and not Path(key).is_file():
                raise ValueError('SSH私钥文件不存在')
        except ValueError as exc:
            QMessageBox.warning(self,'请检查连接参数',str(exc))
            return
        if self.worker:
            self.worker.deleteLater()
        self.session=None
        self.table.setRowCount(0)
        self._pending_fingerprint=''
        self.worker=InspectionWorker(config,self.password.text(),key,self)
        self.worker.updated.connect(self.render)
        self.worker.done.connect(self.render)
        self.worker.error.connect(self.summary.setText)
        self.worker.trust_required.connect(self._trust_received)
        self.worker.finished.connect(self.finished)
        self.controls(True)
        self.report.setEnabled(False)
        self.progress.setRange(0,0)
        self.summary.setText('正在连接指定香橙派并读取系统状态…')
        self.worker.start()

    def _trust_received(self,value):
        self._pending_fingerprint=value

    def finished(self):
        self.controls(False)
        if self._pending_fingerprint:
            value=self._pending_fingerprint
            self._pending_fingerprint=''
            answer=QMessageBox.question(self,'首次连接：核对SSH设备指纹',
                '请与设备管理者或设备终端显示的SSH主机指纹核对：\n\n'+value+'\n\n确认后，是否仅在本次工作台会话信任该指纹并继续连接？',
                QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
            if answer==QMessageBox.Yes:
                self._trusted[(self.host.text().strip(),self.port.value())]=value
                self.start_run()
                return
        self.password.clear()
        if self.session:
            self.render(self.session)
        else:
            self.progress.setRange(0,1)
            self.progress.setValue(0)

    def stop_run(self):
        if self.busy:
            self.worker.cancel.set()
            self.stop.setEnabled(False)
            self.summary.setText('正在停止只读探测并保存已有结果；连接阶段可能需等待超时结束。')

    def render(self,session):
        self.session=session
        rows=session.get('cases',[])
        self.table.setRowCount(len(rows))
        colors={'通过':'#176441','失败':'#ac2938','待确认':'#8b5b13','未测':'#64748b','未执行':'#64748b'}
        for index,row in enumerate(rows):
            for column,key in enumerate(('module','title','status','detail')):
                item=QTableWidgetItem(row.get(key,''))
                item.setToolTip(row.get(key,''))
                if key=='status':
                    item.setForeground(QColor(colors.get(row[key],'#64748b')))
                self.table.setItem(index,column,item)
        self.table.resizeRowsToContents()
        counts=session.get('counts',{})
        done=len(rows)-counts.get('未执行',0)
        self.progress.setRange(0,max(1,len(rows)))
        self.progress.setValue(done)
        self.summary.setText(session['state']+' · '+' · '.join(f'{key} {value}' for key,value in counts.items()))
        self.report.setEnabled(Path(session.get('report','')).is_file())

    def show_report(self):
        if self.session:
            path=Path(self.session['report'])
            if path.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def load_history(self):
        path,_=QFileDialog.getOpenFileName(self,'选择香橙派历史记录',str(BASE/'out/orangepi'),'巡检记录 (session.json)')
        if path:
            try:
                value=json.loads(Path(path).read_text(encoding='utf-8'))
                if value.get('mode')!='orangepi' or not isinstance(value.get('cases'),list):
                    raise ValueError('不是香橙派巡检记录')
                value['folder']=str(Path(path).resolve().parent)
                value['report']=str(Path(path).resolve().parent/'summary.html')
                self.render(value)
            except (OSError,ValueError,KeyError,TypeError):
                QMessageBox.warning(self,'无法加载','请确认选择了完整的香橙派session.json记录。')
