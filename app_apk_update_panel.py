"""Async local APK selection / metadata check / explicit-tablet update controls."""
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QThread,Signal
from PySide6.QtWidgets import QGroupBox,QVBoxLayout,QHBoxLayout,QLineEdit,QPushButton,QLabel,QCheckBox,QFileDialog,QMessageBox
from app_patrol_engine import BASE

class UpdateWorker(QThread):
    result=Signal(dict)
    error=Signal(str)
    progress=Signal(str)
    def __init__(self,serial,path,install=False,expected=None,parent=None):
        super().__init__(parent);self.serial=serial;self.path=path;self.install=install;self.expected=expected
    def run(self):
        try:
            import app_apk_update as backend
            if self.install:
                folder=BASE/'out/app-updates'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])
                result=backend.update(self.serial,self.path,folder,self.expected,self.progress.emit)
                result['report']=str(folder/'update.json')
            else:result=backend.inspect(self.serial,self.path)
            self.result.emit(result)
        except Exception as exc:self.error.emit(str(exc))

class ApkUpdatePanel(QGroupBox):
    busyChanged=Signal(bool)
    def __init__(self,selected_serial,patrol_busy,parent=None):
        super().__init__('APP 版本更新 · 选择本机 APK',parent)
        self.selected_serial=selected_serial;self.patrol_busy=patrol_busy
        self.worker=None;self.metadata=None
        root=QVBoxLayout(self);row=QHBoxLayout()
        self.path=QLineEdit();self.path.setReadOnly(True);self.path.setPlaceholderText('选择要更新的 LivFlow APK，保留原文件')
        self.browse=QPushButton('选择 APK');self.check=QPushButton('检查版本');self.install=QPushButton('安装更新')
        self.browse.clicked.connect(self.choose);self.check.clicked.connect(lambda:self.start(False));self.install.clicked.connect(lambda:self.start(True))
        for w in (self.path,self.browse,self.check,self.install):row.addWidget(w)
        root.addLayout(row)
        self.idle=QCheckBox('已确认平板 APP 没有正在采集或上传，可以覆盖更新并启动')
        root.addWidget(self.idle)
        self.info=QLabel('使用上方明确选择的手机/平板；只支持覆盖更新，不卸载、不清除数据、不自动降级。')
        self.info.setWordWrap(True);root.addWidget(self.info)
    @property
    def busy(self):return self.worker is not None and self.worker.isRunning()
    def choose(self):
        if self.busy or self.patrol_busy():return
        path,_=QFileDialog.getOpenFileName(self,'选择新版 LivFlow APK',str(Path.home()/'Downloads'),'Android 安装包 (*.apk)')
        if path:self.path.setText(path);self.metadata=None;self.info.setText('已选择文件，请先检查版本。')
    def start(self,install):
        if self.busy or self.patrol_busy():
            QMessageBox.information(self,'操作进行中','请等待当前巡检或更新结束，不能同时操作同一平板。');return
        serial=self.selected_serial()
        if not serial or not self.path.text():
            QMessageBox.warning(self,'缺少目标或 APK','请在上方选择明确的平板，并选择 APK。');return
        if install:
            if not self.idle.isChecked():
                QMessageBox.warning(self,'请确认平板空闲','覆盖安装会结束 APP 当前进程；先确认没有采集或上传。');return
            if not self.metadata or self.metadata.get('serial')!=serial or self.metadata['apk']['path']!=str(Path(self.path.text()).resolve()):
                QMessageBox.warning(self,'请先检查版本','APK 或平板目标发生变化，请先点击检查版本。');return
        if self.worker:self.worker.deleteLater()
        self.worker=UpdateWorker(serial,self.path.text(),install,self.metadata,self)
        self.worker.result.connect(self.show_result);self.worker.error.connect(self.failed);self.worker.progress.connect(self.info.setText)
        self.worker.finished.connect(self.finished)
        for w in (self.browse,self.check,self.install,self.idle):w.setEnabled(False)
        self.info.setText('正在安装，请等待设备提示并在平板上完成必要的安装授权…' if install else '正在读取 APK 包名/版本和目标平板当前版本…')
        self.busyChanged.emit(True);self.worker.start()
    def show_result(self,result):
        self.metadata=result
        apk=result['apk'];before=result['before'];after=result.get('after')
        text=f"目标 {result['serial']}；包名 {apk['package']}\n当前 {before['version_name']} / {before['version_code']} → APK {apk['version_name']} / {apk['version_code']}"
        if after:text+=f"\n回读 {after['version_name']} / {after['version_code']}；{result['state']}\n记录：{result.get('report','')}"
        self.info.setText(text)
    def failed(self,message):
        self.metadata=None;self.info.setText('更新/检查未完成：'+message+'\n不会自动卸载或清除数据；签名不一致需提供同签名 APK。')
    def finished(self):
        for w in (self.browse,self.check,self.install,self.idle):w.setEnabled(True)
        self.busyChanged.emit(False)
