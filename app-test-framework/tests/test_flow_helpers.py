import hashlib
import json
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest
from mcap.writer import Writer
from framework.livflow_flow import Flow, UI, Cancelled, configuration
from framework.recording_check import inspect, compare


def config():
    return dict(record_seconds=5,background_seconds=1,human_timeout=10,wait_timeout=5)


def recording(folder, *, metadata=False, payload=b'frame', times=(1000,2000)):
    folder.mkdir(parents=True)
    part = folder/'part0001'
    part.mkdir()
    source = [('livflow.camera_calibration.v1','camera.yaml',b'camera: 1\n'),
              ('livflow.imu_calibration.v1','imu.yaml',b'imu: 2\n'),
              ('livflow.capture_meta.v1','meta.json',b'{"completed":true}')]
    for _,name,data in source:
        (folder/name).write_bytes(data)
    path = part/'test.mcap'
    with path.open('wb') as f:
        w=Writer(f)
        w.start()
        channel=w.register_channel('/camera/left','raw',0)
        for i,t in enumerate(times):
            w.add_message(channel,t,payload,t,sequence=i)
        if metadata:
            for name,filename,data in source:
                w.add_metadata(name,{'filename':filename,'content':data.decode(),
                    'sha256':hashlib.sha256(data).hexdigest()})
        w.finish()
    return path


def test_mcap_metadata_does_not_mask_payload_changes(tmp_path):
    before=recording(tmp_path/'before')
    after=recording(tmp_path/'after',metadata=True)
    result=compare(before,after)
    assert result['messages_unchanged']
    assert result['source_content_matches']
    changed=recording(tmp_path/'changed',metadata=True,payload=b'different')
    assert not compare(before,changed)['messages_unchanged']


def test_timestamp_backwards_is_detected(tmp_path):
    result=inspect(recording(tmp_path/'bad',times=(2000,1000)))
    assert result['topics']['/camera/left']['backward']==1


def test_partial_or_offscreen_control_cannot_match_hidden_bounds():
    tree=ET.fromstring('<hierarchy><node bounds="[0,0][100,100]">'
        '<node content-desc="save" bounds="[0,-60][30,-20]"/>'
        '<node content-desc="save" bounds="[0,60][30,90]"/></node></hierarchy>')
    u=UI('test-serial')
    found=u.find('save',tree=tree)
    assert found[1]==[0,60,30,90]


def test_navigation_behind_system_home_bar_is_never_tapped():
    tree=ET.fromstring('<hierarchy><node bounds="[0,0][1340,800]">'
        '<node content-desc="我的" bounds="[838,734][1174,790]"/>'
        '<node resource-id="android:id/navigationBarBackground" bounds="[0,737][1340,800]"/>'
        '</node></hierarchy>')
    assert UI('fake').find('我的',tree=tree) is None


def test_human_gate_rejects_old_resume(tmp_path,monkeypatch):
    events=[]
    monkeypatch.setattr('framework.livflow_flow.event',lambda kind,**v:events.append((kind,v)))
    f=Flow(config(),'fake',tmp_path)
    error=[]
    def run():
        try:
            f.pause('insert card')
        except Exception as exc:
            error.append(exc)
    worker=threading.Thread(target=run)
    worker.start()
    deadline=time.monotonic()+2
    while not events and time.monotonic()<deadline:
        time.sleep(.01)
    (tmp_path/'resume.json').write_text('{"gate":"old"}')
    time.sleep(.3)
    assert worker.is_alive()
    (tmp_path/'resume.json').write_text(json.dumps({'gate':events[0][1]['gate']}))
    worker.join(2)
    assert not worker.is_alive()
    assert not error


def test_cancel_during_recording_still_stops_device(tmp_path):
    f=Flow(config(),'fake',tmp_path)
    f.connect=Mock()
    f.pause=Mock()
    f.detail=Mock()
    f.capture=Mock()
    f.ui=Mock()
    f.ui.find.return_value=True
    f.sleep=Mock(side_effect=Cancelled('stop'))
    with pytest.raises(Cancelled):
        f.record()
    assert any(call.args==('结束采集',) for call in f.ui.tap.call_args_list)
    assert not f.record_requested


def test_configuration_rejects_unbounded_recording_and_shell_task(tmp_path):
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'task_id':'x;reboot'}))
    with pytest.raises(ValueError):
        configuration(path)
    path.write_text(json.dumps({'record_seconds':3600}))
    with pytest.raises(ValueError):
        configuration(path)
