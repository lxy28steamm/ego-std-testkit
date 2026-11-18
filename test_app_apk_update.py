import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import io,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import Mock,patch
import app_apk_update as backend
from PySide6.QtWidgets import QApplication,QMessageBox
from app_apk_update_panel import ApkUpdatePanel

class ApkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def metadata(self):return dict(path='C:/new.apk',package=backend.PACKAGE,version_name='1.2.0',version_code=50,sha256='known',size_bytes=10)
    def test_wrong_package_and_downgrade_never_install(self):
        for meta,before in [(dict(self.metadata(),package='other.package'),{}),(self.metadata(),{'version_code':51})]:
            with patch.object(backend,'read_apk',return_value=meta),patch.object(backend,'installed',return_value=before),patch.object(backend,'command') as cmd:
                with self.assertRaises(ValueError):backend.inspect('tablet','new.apk')
                cmd.assert_not_called()
    def test_changed_apk_and_version_readback_mismatch_are_rejected(self):
        result={'serial':'tablet','apk':self.metadata(),'before':{'version_code':49,'version_name':'old'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(backend,'inspect',return_value=result),patch.object(backend,'adb_path',return_value='adb'),patch.object(backend,'command',return_value='Success'),patch.object(backend,'installed',return_value={'version_code':49,'version_name':'old'}):
            with self.assertRaisesRegex(ValueError,'发生变化'):backend._update('tablet','new.apk',tmp,expected={'apk':{'sha256':'different'}})
            with self.assertRaisesRegex(RuntimeError,'回读不一致'):backend._update('tablet','new.apk',tmp)
    def test_install_failure_never_uninstalls_or_clears_data(self):
        result={'serial':'tablet','apk':self.metadata(),'before':{'version_code':49,'version_name':'old'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(backend,'inspect',return_value=result),patch.object(backend,'adb_path',return_value='adb'),patch.object(backend,'command',side_effect=RuntimeError('INSTALL_FAILED_UPDATE_INCOMPATIBLE')) as command:
            with self.assertRaises(RuntimeError):backend._update('tablet','new.apk',tmp)
            args=command.call_args.args[0]
            self.assertEqual(args[:6],['adb','-s','tablet','install','-r','--no-streaming'])
            self.assertTrue(Path(tmp,'update.json').is_file());self.assertEqual(command.call_count,1)
    def test_success_requires_install_version_and_launch(self):
        result={'serial':'tablet','apk':self.metadata(),'before':{'version_code':49,'version_name':'old'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(backend,'inspect',return_value=result),patch.object(backend,'adb_path',return_value='adb'),patch.object(backend,'installed',return_value={'version_code':50,'version_name':'1.2.0'}),patch.object(backend,'command',side_effect=['Success',backend.PACKAGE+'/.MainActivity','Status: ok','1234']):
            self.assertEqual(backend._update('tablet','new.apk',tmp)['state'],'更新及启动验证通过')
    def test_invalid_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'broken.apk'
            with zipfile.ZipFile(path,'w') as z:z.writestr('AndroidManifest.xml',b'not binary manifest')
            with self.assertRaises(ValueError):backend.read_apk(path)
    def test_ui_busy_and_target_change_blocks_install(self):
        p=ApkUpdatePanel(lambda:'tablet',lambda:True)
        with patch.object(QMessageBox,'information') as dialog:
            p.start(True);dialog.assert_called_once();self.assertIsNone(p.worker)
        p=ApkUpdatePanel(lambda:'different-tablet',lambda:False);p.path.setText('C:/new.apk');p.idle.setChecked(True);p.metadata={'serial':'old-tablet','apk':self.metadata()}
        with patch.object(QMessageBox,'warning') as dialog:
            p.start(True);dialog.assert_called_once();self.assertIsNone(p.worker)

if __name__=='__main__':unittest.main()
