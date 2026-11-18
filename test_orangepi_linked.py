import unittest
from orangepi_profiles import resolve
from umi_functional import linked_required_topics

class LinkedTests(unittest.TestCase):
    def test_explicit_pair(self):
        p=resolve({'selection':{'enabled_devices':['dex','ego-plus']}},'linked')
        self.assertEqual(set(p['linked_slots']),{'dex','ego-plus'})
    def test_reject_incomplete_or_extra_devices(self):
        for slots in (['dex'],['ego-plus'],['ego-plus','dex','umi']):
            with self.assertRaises(ValueError):
                resolve({'selection':{'enabled_devices':slots}},'linked')
    def test_both_hands_and_stereo_required(self):
        topics=linked_required_topics(['left','right'])
        self.assertEqual(len(topics),5)
        self.assertIn('/devices/dex/hands/right/state',topics)
        self.assertIn('/devices/ego-plus/imu',topics)

    def test_disconnected_ego_cannot_pass_selection(self):
        from orangepi_modular import evaluate
        h={'selection':{'enabled_devices':['ego-plus','dex']},'devices':{'dex':{'hardware_connected':True,'pipeline_state':'ready'},'ego-plus':{'hardware_connected':False,'pipeline_state':'fault'}}}
        rows={r['id']:r for r in evaluate({'health':h},resolve(h,'linked'),'peripheral')}
        self.assertNotEqual(rows['ACC-selection']['status'],'通过')
        self.assertIn('ACC-quality',rows)
    def test_bound_dex_camera_required_in_file(self):
        topics=linked_required_topics(['left','right'],{'left_camera':'/dev/video0'})
        self.assertIn('/devices/dex/cameras/left/video',topics)
        self.assertNotIn('/devices/dex/cameras/right/video',topics)

    def test_busy_session_never_stopped(self):
        import tempfile
        from unittest.mock import Mock,patch
        from umi_functional import run
        api=Mock();api.call.return_value={'session_id':'other','state':'recording'}
        with tempfile.TemporaryDirectory() as folder,patch('umi_functional.Client',return_value=api):
            result=run({'host':'test','device_profile':{'family':'dex','slot':'dex','linked_slots':['ego-plus','dex']}},folder)
        self.assertIn('error',result)
        self.assertEqual([c.args[0] for c in api.call.call_args_list],['/api/v2/session'])
    def test_storage_fault_prevents_mutations(self):
        import tempfile
        from unittest.mock import Mock,patch
        from umi_functional import run
        api=Mock();api.call.side_effect=[{'state':'complete','session_id':'old'},{'storage':{'state':'fault','free_bytes':0}}]
        with tempfile.TemporaryDirectory() as folder,patch('umi_functional.Client',return_value=api):
            result=run({'host':'test','device_profile':{'family':'dex','slot':'dex','linked_slots':['ego-plus','dex']}},folder)
        self.assertIn('error',result)
        self.assertEqual(len(api.call.call_args_list),2)
