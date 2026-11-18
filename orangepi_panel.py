"""Orange Pi inspection UI. Authentication is memory-only and separate from Ego IP."""
import json
from pathlib import Path
import threading

from PySide6.QtCore import QThread, Signal, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLineEdit,QSpinBox,
    QPushButton,QLabel,QTableWidget,QTableWidgetItem,QHeaderView,QProgressBar,QFileDialog,
    QMessageBox,QScrollArea,QLayout,QFrame,QComboBox,QCheckBox,QInputDialog,QGroupBox)

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
            self.config.pop('login_password',None)
            self.config.pop('login_account',None)


class OrangePiPanel(QWidget):
    title='香橙派整机验收'

    def __init__(self):
        super().__init__()
        self.worker=None
        self.session=None
        self._history=False
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
        heading=QLabel('香橙派整机功能验收')
        heading.setStyleSheet('font-size:17px;font-weight:600')
        root.addWidget(heading)
        note=QLabel('常规测试只检查主机供电、网络、存储和账号；外接专项按 EGO / UMI / DEX 的连接、标定和数据契约分别执行。自动识别只读取生效配置，不切换槽位或抢占串口。勾选实采会创建保留的测试文件。')
        note.setWordWrap(True)
        root.addWidget(note)
        form=QFormLayout()
        form.setVerticalSpacing(8)
        self.device=QComboBox()
        self.device.addItem('自动识别当前生效设备','auto')
        self.device.addItem('EGO Plus + DEX 联动采集','linked')
        self.device.addItem('EGO（USB 相机 / IMU）','ego')
        self.device.addItem('UMI（手侧串口 / 配套相机 / VIVE）','umi')
        self.device.addItem('DEX（手侧串口 / 近手相机 / URDF）','dex')
        self.test_scope=QComboBox()
        for label,value in [('常规 + 外接专项','all'),('仅主机常规测试','host'),('仅外接设备专项','peripheral')]: self.test_scope.addItem(label,value)
        form.addRow('测试模块',self.test_scope)
        self.rules_file=QLineEdit()
        self.rules_file.setPlaceholderText('可选：产品验收标准 JSON；空白时缺失的标准记未测')
        form.addRow('验收标准文件',self.rules_file)
        self.api_port=QSpinBox()
        self.api_port.setRange(1,65535)
        self.api_port.setValue(18000)
        form.addRow('验收对象',self.device)
        form.addRow('业务 API 端口',self.api_port)
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
                              ('SSH密码',self.password),('SSH私钥',self.key_file)]:
            form.addRow(label,control)
        connection_group=QGroupBox('连接与测试模块')
        connection_group.setLayout(form)
        root.addWidget(connection_group)
        self.profile_hint=QLabel('自动识别：从设备端生效配置加载专项；联动实采请选择 EGO Plus + DEX 联动采集。')
        self.profile_hint.setWordWrap(True)
        root.addWidget(self.profile_hint)
        self.login_test=QCheckBox('执行正反向登录测试（仅同一测试账号，会重新登录）')
        self.cloud_account=QLineEdit()
        self.cloud_password=QLineEdit()
        self.cloud_password.setEchoMode(QLineEdit.Password)
        self.cloud_account.setPlaceholderText('专用测试账号；不写入报告')
        self.cloud_password.setPlaceholderText('仅本轮使用，不保存')
        self.record_test=QCheckBox('执行本轮短采集和 MCAP 校验（新增测试数据）')
        self.button_test=QCheckBox('配合本设备按键测试（建议 60 秒，允许提前停录）')
        self.upload_test=QCheckBox('上传本轮测试数据（保留本地文件）')
        self.record_seconds=QSpinBox()
        self.record_seconds.setRange(10,60)
        self.record_seconds.setValue(15)
        self.record_seconds.setSuffix(' 秒')
        self.assigned_task=QLineEdit()
        self.assigned_task.setPlaceholderText('上传需要：已授权的系统分配任务 ID')
        test_form=QFormLayout()
        login_form=QFormLayout()
        login_form.addRow(self.login_test)
        login_form.addRow('测试云账号',self.cloud_account)
        login_form.addRow('测试云密码',self.cloud_password)
        login_group=QGroupBox('主机常规 · 账号专项（可选）')
        login_group.setLayout(login_form)
        root.addWidget(login_group)
        test_form.addRow(self.record_test)
        test_form.addRow(self.button_test)
        test_form.addRow('录制时长',self.record_seconds)
        test_form.addRow(self.upload_test)
        test_form.addRow('上传任务 ID',self.assigned_task)
        peripheral_group=QGroupBox('外接设备专项 · 实采与文件验证（可选）')
        peripheral_group.setLayout(test_form)
        root.addWidget(peripheral_group)
        self.device.currentIndexChanged.connect(self._sync_modules)
        self.test_scope.currentIndexChanged.connect(self._sync_modules)
        hint=QLabel('普通短采集请勿操作实体按键；专项按键测试会触发真实启停。按本设备实际按键方式操作，未捕获动作时不自动判通过，可用设备日志与现场确认补齐。')
        hint.setWordWrap(True)
        root.addWidget(hint)
        ports=QPushButton('打开设备页面连接 / 标定')
        ports.clicked.connect(self.open_device)
        root.addWidget(ports)

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
        # 通用资源阈值不作为 UMI/DEX 功能验收项。
        self.services.hide()
        for index in range(limits.count()):
            limits.itemAt(index).widget().hide()
        buttons=QHBoxLayout()
        self.start=QPushButton('开始整机验收检查')
        self.start.setStyleSheet('background:#173c68;color:white;border-radius:5px;padding:6px 12px')
        self.stop=QPushButton('停止巡检')
        self.report=QPushButton('打开本轮报告')
        self.history=QPushButton('加载历史记录')
        self.confirm_result=QPushButton('记录屏幕/按键现场结果')
        self.confirm_result.clicked.connect(self.confirm_field_result)
        self.stop.setEnabled(False)
        self.report.setEnabled(False)
        self.start.clicked.connect(self.start_run)
        self.stop.clicked.connect(self.stop_run)
        self.report.clicked.connect(self.show_report)
        self.history.clicked.connect(self.load_history)
        for button in (self.start,self.stop,self.report,self.history,self.confirm_result):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self.summary=QLabel('尚未执行。业务功能结果与系统诊断分开；完整验收仍需补齐本轮实采、文件及云端验证。')
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        root.addWidget(self.summary)
        self.progress=QProgressBar()
        self.progress.setValue(0)
        root.addWidget(self.progress)
        self.result_scope=QComboBox()
        for label,value in [('全部结果','all'),('主机常规结果','host'),('外接设备专项结果','peripheral')]:self.result_scope.addItem(label,value)
        self.result_scope.currentIndexChanged.connect(self._filter_results)
        root.addWidget(self.result_scope)
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

    def _sync_modules(self):
        from orangepi_profiles import GUIDES
        family=self.device.currentData()
        self.profile_hint.setText(GUIDES.get(family,'自动识别生效配置；多设备联动时请选择具体专项。不会自动修改设备连接或标定。'))
        host_only=self.test_scope.currentData()=='host'
        peripheral_only=self.test_scope.currentData()=='peripheral'
        self.device.setEnabled(not host_only)
        for control in (self.record_test,self.button_test,self.upload_test,self.record_seconds,self.assigned_task):control.setEnabled(not host_only)
        if host_only:
            self.record_test.setChecked(False);self.upload_test.setChecked(False);self.button_test.setChecked(False)
        self.login_test.setEnabled(not peripheral_only)
        if peripheral_only:self.login_test.setChecked(False)

    def _filter_results(self):
        if not hasattr(self,'table') or not self.session:return
        selected=self.result_scope.currentData()
        for i,row in enumerate(self.session.get('cases',[])):
            self.table.setRowHidden(i,selected!='all' and row.get('scope')!=selected)

    def open_device(self):
        try:
            cfg=settings(self.host.text(),self.username.text(),self.port.value())
        except ValueError as exc:
            QMessageBox.warning(self,'请填写设备地址',str(exc));return
        QDesktopServices.openUrl(QUrl(f"http://{cfg['host']}:{self.api_port.value()}/remote"))

    @property
    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def controls(self,busy):
        for widget in (self.start,self.host,self.port,self.username,self.password,self.key_file,self.services,
                       self.temperature,self.disk,self.memory,self.cpu,self.history,self.test_scope,self.device,self.rules_file,self.api_port,self.login_test,self.cloud_account,self.cloud_password,self.record_test,self.button_test,self.upload_test,self.record_seconds,self.assigned_task):
            widget.setEnabled(not busy)
        self.stop.setEnabled(busy)
        if not busy:self._sync_modules()

    def start_run(self):
        if self.busy:
            return
        try:
            target=(self.host.text().strip(),self.port.value())
            config=settings(target[0],self.username.text(),target[1],self.services.text(),
                            self.temperature.value(),self.disk.value(),self.memory.value(),self.cpu.value(),self._trusted.get(target,''))
            from orangepi_acceptance_extra import validate_rules
            rules_path=self.rules_file.text().strip()
            try:
                rules=json.loads(Path(rules_path).read_text(encoding='utf-8-sig')) if rules_path else {}
                config['acceptance_rules']=validate_rules(rules)
            except (OSError,ValueError,TypeError) as exc:
                raise ValueError('验收标准文件无效：'+str(exc))
            config['framework_v2']=True
            config['test_scope']=self.test_scope.currentData()
            config['acceptance']=self.device.currentData()
            config['api_port']=self.api_port.value()
            config['test_login']=self.login_test.isChecked()
            config['login_account']=self.cloud_account.text().strip()
            config['login_password']=self.cloud_password.text()
            if config['test_login'] and (not config['login_account'] or not config['login_password']):
                raise ValueError('登录测试需填写专用账号和密码')
            config['test_record']=self.record_test.isChecked()
            config['test_upload']=self.upload_test.isChecked()
            config['record_seconds']=self.record_seconds.value()
            config['test_buttons']=self.button_test.isChecked()
            if config['test_buttons'] and not config['test_record']:
                raise ValueError('按键测试需同时启用本轮短采集')
            config['capture_preview']=config['test_record']
            config['assigned_task_id']=self.assigned_task.text().strip()
            if config['test_scope']=='host' and config['test_record']:
                raise ValueError('仅主机模式不执行外接设备实采')
            if config['test_upload'] and not config['test_record']:
                raise ValueError('上传测试需同时选择本轮短采集')
            if config['test_upload'] and (not config['assigned_task_id'].isdigit() or int(config['assigned_task_id'])<=0):
                raise ValueError('请填写允许接收测试数据的系统任务 ID')
            key=self.key_file.text().strip()
            if key and not Path(key).is_file():
                raise ValueError('SSH私钥文件不存在')
        except ValueError as exc:
            QMessageBox.warning(self,'请检查连接参数',str(exc))
            return
        if self.worker:
            self.worker.deleteLater()
        self.session=None
        self._history=False
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
        self.summary.setText('正在读取目标设备业务状态与录制证据…')
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
        self.cloud_password.clear()
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
        self._filter_results()
        counts=session.get('counts',{})
        done=len(rows)-counts.get('未执行',0)
        self.progress.setRange(0,max(1,len(rows)))
        self.progress.setValue(done)
        identity=session.get('device_info',{})
        identity_text=('设备 SN：'+(identity.get('sn') or '未获取')+' · 软件版本：'+(str(identity.get('version') or '未获取'))+'\n') if session.get('acceptance_device') else ''
        profile=session.get('device_profile') or {}
        profile_text=('专项：'+str(profile.get('family','')).upper()+' / '+str(profile.get('slot',''))+'\n') if profile else ''
        self.summary.setText(identity_text+profile_text+session['state']+' · '+' · '.join(f'{key} {value}' for key,value in counts.items()))
        self.report.setEnabled(Path(session.get('report','')).is_file())

    def confirm_field_result(self):
        if self.busy or not self.session or self._history:
            QMessageBox.information(self,'当前不可记录','请完成本轮检查；历史记录保持只读。');return
        index=self.table.currentRow()
        if index<0:
            QMessageBox.information(self,'请选择项目','先选择显示与交互或实体按键行。');return
        row=self.session['cases'][index]
        if row['id'] not in ('ACC-display','ACC-button'):
            QMessageBox.information(self,'仅限现场专项','只允许补录显示或实体按键的现场结果，自动检查结果不能改写。');return
        result,ok=QInputDialog.getItem(self,'记录现场结果',row['title'],['通过','失败','待确认','未测'],0,False)
        if not ok:return
        note,ok=QInputDialog.getMultiLineText(self,'现场证据','请记录观察结果、操作动作及日志/截图位置：')
        if not ok or not note.strip():return
        row.update(status=result,detail='现场确认：'+note.strip())
        self.session['state']='已结束（现场结果已记录）'
        from app_patrol_engine import write_session
        write_session(self.session)
        self.render(self.session)

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
                self._history=True
                self.render(value)
            except (OSError,ValueError,KeyError,TypeError):
                QMessageBox.warning(self,'无法加载','请确认选择了完整的香橙派session.json记录。')
