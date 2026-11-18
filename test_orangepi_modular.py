import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from orangepi_profiles import resolve,paths,stream_contract,file_contract
from orangepi_modular import catalogue,collect,evaluate,inspect
from PySide6.QtWidgets import QApplication
from orangepi_panel import OrangePiPanel

class ModularTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def h(self,slots):return {'selection':{'enabled_devices':slots},'devices':{x:{'active':True,'hardware_connected':True,'pipeline_state':'ready'} for x in slots}}
    def test_detection_and_mismatch_never_choose_another_device(self):
        self.assertEqual(resolve(self.h(['ego-std-235']))['family'],'ego')
        for slots,want in [(['umi'],'ego'),(['ego-std-235','umi'],'auto'),([],'auto')]:
            with self.assertRaises(ValueError):resolve(self.h(slots),want)
    def test_ego_paths_never_use_hand_or_urdf_endpoints(self):
        p=resolve(self.h(['ego-std-235']));self.assertTrue(all('/umi/' not in x and '/dex/' not in x for x in paths(p).values()))
        p=resolve(self.h(['dex']));self.assertIn('/api/v2/dex/urdf-calibrations',paths(p).values())
        self.assertFalse(any('/umi/' in x for x in paths(p).values()))
    def test_host_only_has_no_device_cases_or_device_requests(self):
        p={'family':'host','slot':'host','guide':'host'}
        rows=catalogue(p,'host');self.assertTrue(rows and all(x['scope']=='host' for x in rows))
        api=Mock();api.call.return_value={}
        import threading
        collect(api,p,'host',threading.Event())
        self.assertFalse(any('/umi/' in c.args[0] or '/dex/' in c.args[0] or '/camera/' in c.args[0] for c in api.call.call_args_list))
    def test_other_device_session_is_not_reused(self):
        p=resolve(self.h(['ego-std-235']));h=self.h(['ego-std-235']);h['session']={'devices':['umi'],'result':'complete','mcap_path':'umi.mcap'}
        rows={r['id']:r for r in evaluate({'health':h},p,'peripheral')}
        self.assertEqual(rows['ACC-session']['status'],'未测')
        self.assertEqual(rows['ACC-stream_set']['status'],'未测')
    def test_ego_and_umi_file_contracts_are_different(self):
        self.assertEqual(file_contract({'family':'ego'},{'topics':{'/common/camera/video':1}})[0],'失败')
        self.assertEqual(file_contract({'family':'ego'},{'topics':{'/common/camera/video':1,'/common/imu':1}})[0],'通过')
        self.assertEqual(file_contract({'family':'umi'},{'topics':{'/common/gripper/left/state':1}})[0],'通过')
        self.assertEqual(file_contract({'family':'dex'},{'topics':{'/common/camera/video':1}})[0],'待确认')
    def test_stream_contract_does_not_accept_other_family(self):
        _,errors=stream_contract({'family':'ego'},{'/devices/umi/imu':{'message_count':5}})
        self.assertTrue(errors)
    def test_ui_default_auto_and_scope_separation(self):
        p=OrangePiPanel();self.assertEqual(p.device.currentData(),'auto')
        p.record_test.setChecked(True);p.test_scope.setCurrentIndex(1)
        self.assertFalse(p.record_test.isChecked());self.assertFalse(p.device.isEnabled());p.close()
    def test_failure_during_detection_never_starts_recording(self):
        api=Mock();api.call.return_value=self.h(['umi'])
        with tempfile.TemporaryDirectory() as tmp,patch('orangepi_modular.BASE',Path(tmp)),patch('umi_functional.Client',return_value=api):
            session=inspect({'host':'192.0.2.1','acceptance':'ego','test_record':True})
            self.assertEqual(session['state'],'检查未完成')
            self.assertEqual(api.call.call_count,1)
            self.assertTrue(Path(session['report']).exists())

class EgoRecordingTests(unittest.TestCase):
    def test_ego_recording_uses_ego_contract_without_umi_endpoints(self):
        import io
        from mcap.writer import Writer
        from umi_functional import run
        buf=io.BytesIO();writer=Writer(buf);writer.start()
        for topic in ('/common/camera/video','/common/imu'):
            ch=writer.register_channel(topic=topic,message_encoding='json',schema_id=0)
            writer.add_message(channel_id=ch,log_time=1,publish_time=1,data=b'{}')
        writer.finish();raw=buf.getvalue()
        class Fake:
            def __init__(self):self.started=False;self.stopped=False;self.opener=Mock();self.opener.open.side_effect=lambda *a,**k:io.BytesIO(raw);self.base='http://test';self.calls=[]
            def call(self,path,body=None,method=None,timeout=30):
                self.calls.append(path)
                if path=='/api/v2/health':return {'storage':{'state':'healthy','free_bytes':1000000},'selection':{'enabled_devices':['ego-std-235']},'devices':{'ego-std-235':{'hardware_connected':True,'pipeline_state':'ready'}}}
                if path=='/api/v2/collection-task':
                    if body is not None: self.task=body['name']
                    return {'name':''} if body is None else {'name':body['name'],'storage_name':body['name']}
                if path=='/api/v2/session/start':self.started=True
                if path=='/api/v2/session/stop':self.stopped=True
                if path in ('/api/v2/session','/api/v2/session/start','/api/v2/session/stop'):
                    if not self.started:return {'state':'complete','session_id':'old'}
                    return {'state':'complete' if self.stopped else 'recording','session_id':'new','task_storage_name':self.task,'devices':['ego-std-235'],'result':'complete' if self.stopped else None,'mcap_path':'/data/test.mcap','session_directory':'/data','recorder':{'drop_count':0,'streams':{'/devices/ego-std/video':{'message_count':1,'missing_count':0},'/devices/ego-std/imu':{'message_count':1,'missing_count':0}}}}
                if path=='/api/v2/files':return [{'path':'/data/test.mcap','size_bytes':len(raw)}]
                raise AssertionError('Unexpected API '+path)
        fake=Fake()
        with tempfile.TemporaryDirectory() as tmp,patch('umi_functional.Client',return_value=fake):
            result=run({'host':'test','device_profile':{'family':'ego','slot':'ego-std-235'},'record_seconds':0},tmp)
        self.assertFalse(result.get('error'))
        self.assertTrue(any(x['id']=='ACC-data_layout' and x['status']=='通过' for x in result['steps']))
        self.assertFalse(any('/umi/' in x or '/dex/' in x for x in fake.calls))

if __name__=='__main__':unittest.main()
