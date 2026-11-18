import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from mcap.writer import Writer
from umi_functional import run,validate_mcap
import orangepi_inspection as engine

class FunctionalTests(unittest.TestCase):
    def test_busy_device_is_never_stopped_or_modified(self):
        api=Mock();api.call.return_value={'state':'recording','session_id':'other'}
        with tempfile.TemporaryDirectory() as tmp,patch('umi_functional.Client',return_value=api):
            result=run({'host':'192.0.2.1'},tmp)
            self.assertIn('已有采集任务',result['error'])
            self.assertEqual(api.call.call_count,1)
            self.assertEqual(api.call.call_args.args,('/api/v2/session',))
    def test_crc_valid_file_and_truncated_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'sample.mcap'
            with path.open('wb') as f:
                w=Writer(f);w.start();ch=w.register_channel(topic='/test',message_encoding='json',schema_id=0)
                w.add_message(channel_id=ch,log_time=1,publish_time=1,data=b'{}');w.finish()
            self.assertEqual(validate_mcap(path)['message_count'],1)
            path.write_bytes(path.read_bytes()[:-4])
            with self.assertRaises(ValueError):validate_mcap(path)
    def test_api_mode_merges_current_results_without_ssh_or_secrets(self):
        api=Mock();api.call.side_effect=[{'cloud':{'device_sn':'test-sn'}},{'info':{'version':'test-version'}},{}]
        with tempfile.TemporaryDirectory() as tmp,patch.object(engine,'BASE',Path(tmp)),patch('umi_functional.Client',return_value=api),patch('umi_functional.run',return_value={'session_id':'current','steps':[{'id':'ACC-record','status':'通过','detail':'current evidence'}]}),patch.object(engine.paramiko,'SSHClient') as ssh:
            cfg=engine.settings('192.0.2.1','tester');cfg.update(acceptance='umi',test_record=True)
            result=engine.inspect(cfg,password='unit-secret')
            ssh.return_value.connect.assert_not_called()
            record=next(r for r in result['cases'] if r['id']=='ACC-record')
            self.assertEqual(record['status'],'通过')
            self.assertEqual(result['device_info']['sn'],'test-sn')
            for p in Path(tmp).rglob('*'):
                if p.is_file():self.assertNotIn('unit-secret',p.read_text(encoding='utf-8-sig'))

if __name__=='__main__':unittest.main()
