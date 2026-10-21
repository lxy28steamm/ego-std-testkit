"""Offline Orange Pi checks: never opens a network connection or touches hardware."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import paramiko
from PySide6.QtWidgets import QApplication
import orangepi_inspection as engine
from orangepi_panel import OrangePiPanel, InspectionWorker
from app_patrol_report import render_report


def metrics():
    return dict(schema=1,model='RK3588 OPi 5 Max',system={'PRETTY_NAME':'Test Linux'},kernel='test',architecture='aarch64',
        uptime='3600 100',cpu_busy=20,memory={'MemTotal':100,'MemAvailable':80},
        root_fs={'bytes':100,'free':90,'available':85,'readonly':False},
        temperatures=[{'name':'soc','celsius':40}],storage=[{'name':'mmcblk0','bytes':1000}],
        interfaces=[{'ifname':'eth0','flags':['UP'],'addr_info':[{'scope':'global','local':'192.0.2.1'}]}],
        routes=[{'gateway':'192.0.2.254','dev':'eth0'}],utc=1000,ntp={'ok':True,'text':'yes'},
        services={'ssh.service':{'available':True,'LoadState':'loaded','ActiveState':'active','SubState':'running','Result':'success'}},
        failed_services={'available':True,'units':[]},kernel_errors={'available':True,'sample_count':0},
        usb=[],video_nodes=[{'node':'/dev/video0'}],serial_nodes=[],gpio_nodes=['/dev/gpiochip0'],accelerator_nodes=['/dev/dri/renderD128'])


class OrangePiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_missing_metrics_are_unmeasured_and_nodes_do_not_prove_hardware(self):
        config=engine.settings('192.0.2.1','tester')
        rows={r['id']:r for r in engine.evaluate({},config,1000)}
        for key in ('cpu','memory','disk','temperature','usb','camera','serial','accelerator'):
            self.assertEqual('未测',rows['OPI-'+key]['status'])
        rows={r['id']:r for r in engine.evaluate(metrics(),config,1000)}
        for key in ('camera','serial','accelerator'):
            self.assertEqual('未测',rows['OPI-'+key]['status'])

    def test_thresholds_services_and_clock_are_not_false_passes(self):
        data=metrics()
        data['temperatures'][0]['celsius']=95
        data['memory']['MemAvailable']=1
        data['services']['ssh.service']['ActiveState']='failed'
        data['ntp']['text']='no'
        data['kernel_errors']['available']=False
        rows={r['id']:r for r in engine.evaluate(data,engine.settings('192.0.2.1','tester'),1000)}
        for key in ('temperature','memory','clock'):
            self.assertEqual('待确认',rows['OPI-'+key]['status'])
        self.assertEqual('失败',rows['OPI-service-ssh.service']['status'])
        self.assertEqual('未测',rows['OPI-kernel']['status'])

    def test_service_injection_and_invalid_config_are_rejected(self):
        for services in ('ssh;reboot','--force','ssh.service\n$(reboot)'):
            with self.assertRaises(ValueError):
                engine.settings('192.0.2.1','tester',services=services)
        with self.assertRaises(ValueError):
            engine.settings('192.0.2.1','tester',temperature=0)

    def test_unknown_or_changed_fingerprint_is_not_silently_trusted(self):
        key=Mock()
        key.asbytes.return_value=b'offline-public-key'
        with self.assertRaises(engine.UnknownHostKey):
            engine.PinnedHostKey().missing_host_key(None,'test',key)
        with self.assertRaises(engine.UnknownHostKey):
            engine.PinnedHostKey('SHA256:different').missing_host_key(None,'test',key)
        engine.PinnedHostKey(engine.fingerprint(key)).missing_host_key(None,'test',key)

    def test_auth_failure_does_not_write_password_or_mark_tests_passed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine,'BASE',Path(tmp)), \
             patch.object(engine.paramiko,'SSHClient') as factory:
            factory.return_value.connect.side_effect=paramiko.AuthenticationException('mock-secret-do-not-persist')
            result=engine.inspect(engine.settings('192.0.2.1','tester'),password='mock-secret-do-not-persist')
            self.assertEqual('检查失败',result['state'])
            self.assertEqual({'失败':1,'未测':19},result['counts'])
            for path in Path(tmp).rglob('*'):
                if path.is_file():
                    self.assertNotIn('mock-secret-do-not-persist',path.read_text(encoding='utf-8-sig'))

    def test_cancel_before_connection_never_connects(self):
        cancel=threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine,'BASE',Path(tmp)), \
             patch.object(engine.paramiko,'SSHClient') as factory:
            result=engine.inspect(engine.settings('192.0.2.1','tester'),cancel=cancel)
            factory.return_value.connect.assert_not_called()
            self.assertEqual('已停止',result['state'])
            self.assertEqual({'未测':20},result['counts'])

    def test_orangepi_report_has_correct_title_and_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=dict(folder=tmp,mode='orangepi',state='已结束（含未测或待确认）',
                         cases=engine.evaluate(metrics(),engine.settings('192.0.2.1','tester'),1000))
            report=render_report(session,'香橙派巡检报告')
            self.assertIn('<title>香橙派巡检报告</title>',report)
            self.assertIn('SSH目标',report)
            self.assertNotIn('APP 的上传完成',report)
            self.assertIn('未做实际功能测试',report)

    def test_gui_worker_clears_credentials_and_small_form_does_not_overlap(self):
        worker=InspectionWorker(engine.settings('192.0.2.1','tester'),'mock-password','')
        with patch('orangepi_panel.inspect',return_value={'cases':[]}):
            worker.run()
        self.assertEqual('',worker._password)
        panel=OrangePiPanel()
        panel.resize(820,580)
        panel.show()
        self.app.processEvents()
        for first,second in zip((panel.host,panel.port,panel.username,panel.password),
                                (panel.port,panel.username,panel.password,panel.key_file)):
            self.assertGreater(second.y(),first.y()+first.height())
        self.assertFalse(panel.password.text())
        panel.close()


if __name__=='__main__':
    unittest.main()
