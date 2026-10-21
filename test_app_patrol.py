"""Offline integration tests; do not operate an Android device."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

import app_patrol_engine as engine
from app_patrol_panel import AppPatrolPanel, default_framework

FRAMEWORK = str(default_framework())


class PatrolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_new_manual_never_reuses_historical_passes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'RUNS', Path(tmp)):
            data = engine.manual_session(FRAMEWORK)
            self.assertEqual(65, len(data['cases']))
            self.assertEqual({'未执行': 65}, data['counts'])
            self.assertTrue(all(x['detail'] == '' for x in data['cases']))
            data['cases'][0].update(status='失败', detail='<script>alert(1)</script>')
            engine.write_session(data)
            report = Path(data['report']).read_text(encoding='utf-8')
            self.assertIn('&lt;script&gt;', report)
            self.assertNotIn('<script>alert', report)
            restored = json.loads((Path(data['folder']) / 'session.json').read_text(encoding='utf-8'))
            self.assertEqual('失败', restored['cases'][0]['status'])
            self.assertEqual(64, restored['counts']['未执行'])

    def test_navigation_keeps_app_patrol_visible(self):
        import app as workstation
        window = workstation.MainWindow()
        self.assertEqual(11, window.stack.count())
        window.nav.setCurrentRow(7)
        window._toggle_advanced(False)
        self.assertEqual(7, window.nav.currentRow())
        self.assertFalse(window.nav.item(7).isHidden())
        self.assertTrue(window.nav.item(8).isHidden())
        window.close()

    def test_patrol_controls_do_not_overlap_in_small_workbench(self):
        import app as workstation
        window = workstation.MainWindow()
        window.nav.setCurrentRow(7)
        panel = window.app_patrol
        window.show()
        for width,height in ((1040,700),(1180,760)):
            window.resize(width,height)
            for mode in ('selfcheck','flow_full','flow_upload'):
                panel.mode.setCurrentIndex(panel.mode.findData(mode))
                self.app.processEvents()
                controls = (panel.framework,panel.devices,panel.mode)
                for current,following in zip(controls,controls[1:]):
                    self.assertGreater(following.y(),current.y()+current.height())
                    self.assertGreaterEqual(current.height(),current.minimumSizeHint().height())
                self.assertGreaterEqual(panel.scroll.widget().height(), panel.scroll.widget().minimumSizeHint().height())
        window.close()

    def test_history_is_read_only_and_manual_edits_persist(self):
        panel = AppPatrolPanel()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'RUNS', Path(tmp)):
            fixture = Path(tmp)/'historical-fixture.json'
            cases = json.loads((engine.BASE/'app_patrol_catalog.json').read_text(encoding='utf-8'))
            fixture.write_text(json.dumps({'cases':[dict(x,status='未执行',actual='') for x in cases]}),encoding='utf-8')
            panel.history.addItem('原始记录测试',str(fixture))
            panel.history.setCurrentIndex(panel.history.count()-1)
            panel.load_history()
            self.assertEqual(65, panel.table.rowCount())
            self.assertTrue(panel.read_only)
            self.assertFalse(panel.save.isEnabled())
            panel.create_manual()
            panel.table.selectRow(0)
            panel.status.setCurrentText('待确认')
            panel.note.setText('本轮人工检查记录')
            panel.save_case()
            saved = json.loads((Path(panel.session['folder']) / 'session.json').read_text(encoding='utf-8'))
            self.assertEqual('待确认', saved['cases'][0]['status'])
            self.assertEqual('本轮人工检查记录', saved['cases'][0]['detail'])
        panel.close()

    def test_gui_process_selfcheck_results(self):
        panel = AppPatrolPanel()
        panel.mode.setCurrentIndex(0)
        panel.start_run()
        deadline = time.monotonic() + 30
        while panel.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.02)
        self.assertFalse(panel.busy, 'QProcess did not finish')
        self.assertIsNotNone(panel.session)
        self.assertEqual('已完成', panel.session['state'])
        self.assertEqual({'通过': 3}, panel.session['counts'])
        self.assertEqual(3, panel.table.rowCount())
        self.assertTrue(Path(panel.session['report']).is_file())
        self.assertTrue((Path(panel.session['folder']) / 'junit.xml').is_file())
        panel.close()

    def test_immediate_cancel_is_not_reported_as_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = Path(tmp) / 'stop'
            cancel.write_text('cancel', encoding='utf-8')
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
            proc = subprocess.run([sys.executable, str(engine.BASE / 'app_patrol_engine.py'),
                '--framework', FRAMEWORK, '--mode', 'selfcheck', '--cancel-file', str(cancel)],
                capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
            events = [json.loads(x[len(engine.PREFIX):]) for x in proc.stdout.splitlines() if x.startswith(engine.PREFIX)]
            self.assertEqual(130, proc.returncode)
            last = next(e for e in reversed(events) if e['type'] == 'finished')
            self.assertEqual('已停止', last['session']['state'])
            self.assertNotEqual('已完成', last['session']['state'])

    def test_second_workstation_cannot_start_concurrently(self):
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        with engine.execution_lock():
            proc = subprocess.run([sys.executable, str(engine.BASE / 'app_patrol_engine.py'),
                '--framework', FRAMEWORK, '--mode', 'selfcheck'],
                capture_output=True, text=True, encoding='utf-8', env=env, timeout=15)
        self.assertEqual(2, proc.returncode)
        self.assertIn('另一个工作台', proc.stdout)

    def test_human_continue_writes_current_gate_only(self):
        panel=AppPatrolPanel()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine,'RUNS',Path(tmp)):
            panel.session=engine.new_session('flow_tasks')
            panel.session['state']='等待人工操作'
            panel.session['waiting']={'gate':'current-gate','message':'请完成登录'}
            panel._controls(True)
            panel._render()
            self.assertTrue(panel.resume.isEnabled())
            panel.resume_flow()
            resume=Path(panel.session['folder'])/'resume.json'
            self.assertEqual({'gate':'current-gate'},json.loads(resume.read_text()))
            self.assertFalse(panel.resume.isEnabled())
            panel._controls(False)
        panel.close()

    def test_full_button_selects_full_mode_without_enabling_write_actions(self):
        panel = AppPatrolPanel()
        with patch.object(panel, 'start_run') as start:
            panel.full.click()
            start.assert_called_once()
        self.assertEqual('flow_full', panel.mode.currentData())
        self.assertFalse(panel.allow_parameters.isChecked())
        self.assertFalse(panel.allow_record.isChecked())
        self.assertFalse(panel.allow_upload.isChecked())
        panel._controls(True)
        self.assertFalse(panel.full.isEnabled())
        panel._controls(False)
        panel.close()

    def test_report_distinguishes_unfinished_and_escapes_evidence(self):
        from app_patrol_report import render_report
        with tempfile.TemporaryDirectory() as tmp:
            session = dict(folder=tmp, mode='flow_full', state='已结束（含未测或待确认）',
                cases=[dict(id='safe',title='检查一',module='自动检查',status='通过',detail='自动断言通过'),
                       dict(id='skip',title='未允许上传',module='自动检查',status='未测',detail='<script>alert(1)</script>')])
            page = render_report(session, '全量自动巡检')
            self.assertIn('1/2（50%）',page)
            self.assertIn('部分项目尚未完成',page)
            self.assertIn('3 项框架检查和 9 项设备流程',page)
            self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',page)
            self.assertNotIn('<script>alert(1)</script>',page)
            self.assertIn('id="search"',page)
            self.assertIn('id="status"',page)

    def test_full_dispatch_collects_twelve_without_operating_device(self):
        original_popen = subprocess.Popen
        commands = []
        def collect_only(args, **kwargs):
            commands.append(args[:])
            return original_popen(args + ['--collect-only'], **kwargs)
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine,'RUNS',Path(tmp)), \
             patch.object(engine.subprocess,'Popen',side_effect=collect_only), patch.object(engine,'emit'):
            code = engine.run('flow_full',FRAMEWORK,'offline-collect-only',None)
            saved = json.loads(next(Path(tmp).glob('*/session.json')).read_text(encoding='utf-8'))
            self.assertEqual(0,code)
            self.assertEqual(12,len(saved['cases']))
            self.assertEqual({'未测':12},saved['counts'])
            self.assertNotEqual('已完成',saved['state'])
            self.assertIn('--device',commands[0])
            self.assertNotIn('-m',commands[0][3:])


if __name__ == '__main__':
    unittest.main()
