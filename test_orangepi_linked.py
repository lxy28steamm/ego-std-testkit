import unittest
from orangepi_profiles import resolve
from umi_functional import linked_required_topics

class LinkedTests(unittest.TestCase):
    def test_linked_capture_downloads_mapped_mcap_and_restores_task(self):
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch
        from mcap.writer import Writer
        from umi_functional import run

        targets = ['dex', 'ego-plus']
        health = {
            'selection': {'enabled_devices': targets},
            'storage': {'state': 'healthy', 'free_bytes': 1000000},
            'devices': {target: {'pipeline_state': 'ready', 'hardware_connected': True} for target in targets},
        }
        sources = linked_required_topics(['right'])
        mapping = {topic: '/common/stream_' + str(i) for i, topic in enumerate(sources)}
        buf = io.BytesIO()
        writer = Writer(buf)
        writer.start()
        writer.add_metadata('common.stream_mapping', mapping)
        for topic in mapping.values():
            channel = writer.register_channel(topic, 'json', 0)
            writer.add_message(channel, 1, b'{}', 1)
        writer.finish()
        raw = buf.getvalue()
        remote = '/data/linked.mcap'

        class Device:
            base = 'http://test'
            task = 'original'
            state = 'idle'

            def call(self, path, body=None, method=None, timeout=30):
                if path == '/api/v2/health':
                    return health
                if path == '/api/v2/dex/bindings':
                    return {'mode': 'single', 'single_hand': 'right', 'single_port': '/dev/test'}
                if path == '/api/v2/dex/serial-devices':
                    return [{'path': '/dev/test'}]
                if path == '/api/v2/dex/realtime?hand=right':
                    return {'connected': True, 'data_fresh': True, 'joints': [1]}
                if path in ('/api/v2/collection-task', '/api/v2/collection-task/select'):
                    if body:
                        self.task = body['name']
                    return {'name': self.task, 'storage_name': self.task}
                if path == '/api/v2/files':
                    return [{'path': remote, 'size_bytes': len(raw)}]
                if path == '/api/v2/session/start':
                    self.state = 'recording'
                elif path == '/api/v2/session/stop':
                    self.state = 'complete'
                elif path != '/api/v2/session':
                    raise AssertionError(path)
                return {'state': self.state, 'session_id': 'old' if self.state == 'idle' else 'new',
                        'devices': targets, 'task_storage_name': self.task, 'result': 'complete',
                        'mcap_path': remote, 'session_directory': '/data',
                        'recorder': {'drop_count': 0, 'streams': dict.fromkeys(sources, {'message_count': 1})}}

        device = Device()
        device.opener = Mock()
        preview_states = []

        def open_response(url, timeout):
            if '/preview/snapshot.jpg' in url:
                preview_states.append(device.state)
                return io.BytesIO(b'\xff\xd8test\xff\xd9')
            self.assertIn('/files/download?', url)
            self.assertEqual(device.state, 'complete')
            return io.BytesIO(raw)

        device.opener.open.side_effect = open_response
        with tempfile.TemporaryDirectory() as folder, patch('umi_functional.Client', return_value=device):
            result = run({'host': 'test', 'capture_preview': True, 'record_seconds': 1,
                          'device_profile': resolve(health)}, folder)
            self.assertEqual(Path(folder, 'linked.mcap').read_bytes(), raw)
        self.assertNotIn('error', result)
        self.assertEqual(preview_states, ['idle'])
        statuses = {step['id']: step['status'] for step in result['steps']}
        for key in ('preview', 'sensor', 'record', 'stream_set', 'data_layout'):
            self.assertEqual(statuses['ACC-' + key], '通过')
        self.assertEqual((device.task, device.state), ('original', 'complete'))

    def test_file_topics_resolved_from_capture_metadata(self):
        from umi_functional import linked_file_contract
        required = linked_required_topics(['right'])
        mapping = {source: '/common/stream_' + str(i) for i, source in enumerate(required)}
        topics = dict.fromkeys(mapping.values(), 12)
        status, _ = linked_file_contract(required, {'topics': topics, 'stream_mapping': mapping})
        self.assertEqual(status, '通过')
        status, _ = linked_file_contract(required, {'topics': dict.fromkeys(required, 1)})
        self.assertEqual(status, '通过')

    def test_missing_empty_or_shared_destination_cannot_pass(self):
        from umi_functional import linked_file_contract
        required = linked_required_topics(['right'])
        mapping = {source: '/common/stream_' + str(i) for i, source in enumerate(required)}
        topics = dict.fromkeys(mapping.values(), 12)
        for evidence in (
            {'topics': topics},
            {'topics': dict(topics, **{mapping[required[0]]: 0}), 'stream_mapping': mapping},
            {'topics': topics, 'stream_mapping': dict(mapping, **{required[0]: mapping[required[1]]})},
        ):
            with self.subTest(evidence=evidence):
                status, _ = linked_file_contract(required, evidence)
                self.assertNotEqual(status, '通过')

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
