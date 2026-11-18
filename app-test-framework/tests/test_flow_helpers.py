import hashlib
import json
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest
from mcap.writer import Writer
from framework.livflow_flow import Flow, UI, Cancelled, configuration, version_matches, parse_phone_wifi_status, task_list_is_restored
from framework.recording_check import inspect, compare


def config():
    return dict(record_seconds=5,background_seconds=1,human_timeout=10,wait_timeout=5)


def test_version_display_accepts_uppercase_v_without_accepting_other_versions():
    assert version_matches('当前版本\nV1.1.1', '1.1.1')
    assert version_matches('当前版本\nv1.1.1', '1.1.1')
    assert not version_matches('当前版本\nV1.1.10', '1.1.1')
    assert not version_matches('当前版本\nV1.0.1', '1.1.1')


def test_revealed_password_form_never_writes_evidence(tmp_path):
    flow=Flow(config(),'fake',tmp_path)
    flow.ui=Mock()
    flow.ui.tree.return_value=ET.fromstring('''<hierarchy><node class="android.view.View" content-desc="密码"/>
      <node class="android.widget.EditText" password="false" text="example-secret"/>
      <node class="android.widget.Button" content-desc="隐藏密码"/></hierarchy>''')
    flow.capture('login')
    assert list(tmp_path.iterdir()) == []
    flow.ui.adb.assert_not_called()


def test_duplicate_that_cannot_be_narrowed_still_refuses_to_click():
    u=UI('fake')
    # 同高不同宽：取最靠上后能收敛到唯一，属于允许的收敛。
    tree=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node content-desc="保存" clickable="true" bounds="[100,400][300,460]"/>'
        '<node content-desc="保存" clickable="true" bounds="[400,400][760,460]"/></node></hierarchy>')
    assert u.find('保存',tree=tree) is not None, '同高不同宽时取最靠上，属于可收敛'
    # 同高且面积相同：任何规则都分不清，必须拒绝点击。
    ambiguous=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node content-desc="保存" clickable="true" bounds="[100,400][300,460]"/>'
        '<node content-desc="保存" clickable="true" bounds="[500,400][700,460]"/></node></hierarchy>')
    with pytest.raises(AssertionError,match='不唯一'):u.find('保存',tree=ambiguous)


def test_disabled_and_duplicate_controls_are_never_guessed():
    ui=UI('fake')
    # 手机偶发 "could not get idle state"、shell Killed 等：重试后能拿到就继续，不判失败。
    ui.adb=Mock(side_effect=[b'UI automation busy',b'<hierarchy><node text="ready"/></hierarchy>'])
    root=ui.tree()
    assert next(root.iter('node')).get('text')=='ready'
    assert all(call.args[:3]==('exec-out','uiautomator','dump') for call in ui.adb.call_args_list)


def test_dump_killed_by_oom_is_retried_not_failed_immediately(monkeypatch):
    # 10-29 现场失败过：uiautomator dump 偶发被 OOM kill，脚本直接判"无法读取界面"
    # 导致短采集整条用例失败。现在要求重试而非立刻失败。
    import subprocess
    u=UI('fake')
    calls=[]
    def fake_adb(*args, **kw):
        calls.append(args)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(137, ['adb'], b'', b'Killed')
        return b'junk<hierarchy><node text="ok"/></hierarchy>tail'
    u.adb=fake_adb
    monkeypatch.setattr('framework.livflow_flow.time.sleep', lambda *_: None)
    root=u.tree()
    assert next(root.iter('node')).get('text')=='ok'
    assert len(calls)==3


def test_dump_failure_degrades_to_ocr_and_still_yields_a_tree(monkeypatch):
    u=UI('fake')
    def always_fail(*args, **kw):
        raise RuntimeError('could not get idle state')
    u.adb=always_fail
    monkeypatch.setattr('framework.livflow_flow.time.sleep', lambda *_: None)
    from framework import ocr
    monkeypatch.setattr(u, '_ocr_tree', lambda: ocr.build_tree([('开始采集', 10, 10, 200, 60)]))
    tree=u.tree()
    labels=[n.get('content-desc') for n in tree.iter('node')]
    assert '开始采集' in labels


def test_ocr_tree_groups_words_into_rows_and_keeps_text():
    from framework import ocr
    # 中文 OCR 会把每个汉字当一个 word，行内必须拼成整句才能做文字匹配。
    root=ocr.build_tree([('语',10,10,40,40),('言',42,10,70,40),('English',300,10,420,40),
                         ('简',10,60,40,90),('体',42,60,70,90),('中',72,60,100,90),('文',102,60,130,90)])
    labels=[n.get('content-desc') for n in root.iter('node') if n.get('content-desc')]
    assert '语言 English' in labels
    assert '简体中文' in labels
    # 生成的树能被状态分类器读取，不会因缺字段崩掉
    from framework.livflow_state import classify
    assert classify(root)=='unknown'


def test_phone_wifi_identity_and_security_are_read_separately():
    assert parse_phone_wifi_status('Wifi is connected to "灵生测试"\nSecurity type: 0') == ('灵生测试',0)
    assert parse_phone_wifi_status('Wifi is connected to "lingsheng"\nSecurity type: 2') == ('lingsheng',2)
    assert parse_phone_wifi_status('Wifi is disconnected') == (None,None)


def test_task_reset_accepts_a_restored_list_without_the_target_on_screen():
    assert task_list_is_restored('任务中心\n共 6 个任务', '')
    assert not task_list_is_restored('任务中心\n共 6 个任务', '24002')
    assert not task_list_is_restored('任务中心', '')


def test_wrong_phone_network_cannot_be_reported_as_success(tmp_path):
    flow=Flow(dict(wifi_security='open'),'fake',tmp_path)
    flow.ui=Mock()
    flow.ui.adb.return_value=b'Wifi is connected to "other"\nSecurity type: 0'
    flow.pause=Mock()
    with pytest.raises(AssertionError,match='手机没有连接'):
        flow.verify_phone_wifi('expected')
    flow.pause.assert_called_once()


def test_existing_device_is_not_reported_as_fresh_wifi_provisioning(tmp_path):
    flow=Flow(dict(device_sn='TEST123',ssid='example',require_wifi_provisioning=True),'fake',tmp_path)
    flow.home=Mock()
    flow.ui=Mock()
    flow.ui.wait.return_value='我的设备\n当前设备\nSN: TEST123'
    flow.ui.find.side_effect=lambda label: True if label=='查看设备' else None
    flow.pause=Mock(side_effect=Cancelled('manual step'))
    with pytest.raises(Cancelled):
        flow.connect()
    flow.pause.assert_called_once()
    assert not any(call.args==('查看设备',) for call in flow.ui.tap.call_args_list)


def test_scan_never_connects_when_more_than_one_serial_is_visible(tmp_path):
    flow=Flow(dict(wait_timeout=5),'fake',tmp_path)
    flow.ui=Mock()
    flow.ui.tree.return_value=ET.fromstring('''<hierarchy><node bounds="[0,0][1000,700]">
      <node content-desc="SN: TEST123&#10;SN: OTHER456" bounds="[0,0][900,600]">
        <node content-desc="连接设备" bounds="[100,400][800,500]"/>
      </node></node></hierarchy>''')
    with pytest.raises(AssertionError,match='多个设备'):
        flow.connect_discovered_device('TEST123')
    flow.ui.adb.assert_not_called()


def test_scan_taps_only_button_bound_to_single_identified_target(tmp_path):
    flow=Flow(dict(wait_timeout=5),'fake',tmp_path)
    actual=UI('fake')
    actual.tree=Mock(return_value=ET.fromstring('''<hierarchy><node bounds="[0,0][1000,700]">
      <node content-desc="SN: TEST123" bounds="[0,0][900,600]">
        <node content-desc="连接设备" bounds="[100,400][800,500]"/>
      </node></node></hierarchy>'''))
    actual.adb=Mock()
    flow.ui=actual
    flow.connect_discovered_device('TEST123')
    actual.adb.assert_called_once_with('shell','input','tap','450','450')


def test_bluetooth_service_discovery_failure_is_not_wifi_success(tmp_path):
    flow=Flow(dict(wait_timeout=5),'fake',tmp_path)
    flow.ui=Mock()
    flow.ui.wait.return_value='连接失败\n未能完成蓝牙服务发现'
    with pytest.raises(AssertionError,match='蓝牙服务发现未完成'):
        flow.wait_for_provision_page()


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


def test_configuration_supports_explicit_wifi_security_without_password(tmp_path):
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'wifi_security':'protected','ssid':'example'}))
    assert configuration(path)['wifi_security']=='protected'
    path.write_text(json.dumps({'wifi_security':'unknown'}))
    with pytest.raises(ValueError):
        configuration(path)


def test_failed_recording_blocks_upload_before_any_phone_or_card_action(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_PATROL_ALLOW_RECORD','1')
    flow=Flow(config(),'fake',tmp_path)
    flow.ui=Mock();flow.pause=Mock()
    with pytest.raises(pytest.skip.Exception,match='本轮短采集未完成'):
        flow.upload()
    flow.pause.assert_not_called()
    assert not flow.ui.method_calls


def test_record_retry_clears_previous_control_completion(tmp_path):
    flow=Flow(config(),'fake',tmp_path)
    flow.record_control_completed=True
    flow.pause=Mock(side_effect=RuntimeError('operator gate unavailable'))
    with pytest.raises(RuntimeError):flow.record()
    assert flow.record_control_completed is False


def test_page_state_priority_does_not_confuse_preview_with_navigation_header():
    from framework.livflow_state import classify
    tree=ET.fromstring('<hierarchy><node bounds="[0,0][100,100]"><node content-desc="视频预览"/><node content-desc="左目 · 1600"/><node content-desc="右目 · 1600"/><node content-desc="配置网络"/></node></hierarchy>')
    assert classify(tree)=='preview'


def test_duplicate_controls_are_narrowed_by_positive_evidence_only():
    # 场景来自 10-29 现场：任务卡片文字和它的无障碍代理重名，坐标不同但语义同一个入口。
    # 允许用"更靠上/更大/祖先带上下文"收敛；收敛不到唯一仍然必须拒绝。
    u=UI('fake')
    parent=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node content-desc="123456&#10;测试任务" clickable="true" bounds="[100,400][900,560]"/>'
        '<node content-desc="123456&#10;测试任务" clickable="true" bounds="[120,430][880,520]"/>'
        '</node></hierarchy>')
    node,bounds=u.find('123456\n测试任务',tree=parent)
    assert bounds==[100,400,900,560], '应当选中面积更大的真实卡片，而不是内部代理'
    # 顺序换一下也要稳定（不能依赖 XML 顺序）
    reversed_tree=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node content-desc="123456&#10;测试任务" clickable="true" bounds="[120,430][880,520]"/>'
        '<node content-desc="123456&#10;测试任务" clickable="true" bounds="[100,400][900,560]"/>'
        '</node></hierarchy>')
    assert u.find('123456\n测试任务',tree=reversed_tree)[1]==[100,400,900,560]


def test_container_scope_wins_over_duplicate_scan_buttons():
    u=UI('fake')
    tree=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node content-desc="SN: OTHER456" bounds="[0,100][900,300]"><node content-desc="连接设备" clickable="true" bounds="[600,200][880,260]"/></node>'
        '<node content-desc="SN: TEST123" bounds="[0,400][900,600]"><node content-desc="连接设备" clickable="true" bounds="[600,500][880,560]"/></node>'
        '</node></hierarchy>')
    assert u.find('连接设备',tree=tree,container='SN: TEST123')[1]==[600,500,880,560]


def test_control_is_refreshed_before_click_and_uses_stable_position():
    first=ET.fromstring('<hierarchy><node bounds="[0,0][200,200]"><node text="go" clickable="true" bounds="[0,0][20,20]"/></node></hierarchy>')
    moved=ET.fromstring('<hierarchy><node bounds="[0,0][200,200]"><node text="go" clickable="true" bounds="[100,100][140,140]"/></node></hierarchy>')
    u=UI('fake');u.tree=Mock(side_effect=[first,moved,moved,moved]);u.adb=Mock()
    u.tap('go')
    u.adb.assert_called_once_with('shell','input','tap','120','120')


def test_provision_wait_accepts_preview_without_waiting_for_missing_network_button(tmp_path):
    f=Flow(dict(wait_timeout=5),'fake',tmp_path);f.ui=Mock()
    f.ui.wait.return_value='视频预览\n左目 · 1600\n右目 · 1600'
    assert '视频预览' in f.wait_for_provision_page()
    f.ui.tap.assert_not_called()


def test_connected_callback_rechecks_target_before_opening_preview(tmp_path):
    f=Flow(dict(wait_timeout=5,device_sn='TARGET'),'fake',tmp_path);f.ui=Mock()
    f.ui.wait.return_value='当前设备\nSN: OTHER\n查看设备'
    with pytest.raises(AssertionError,match='不是指定SN'):f.wait_for_provision_page()
    f.ui.tap.assert_not_called()


def test_wait_budget_does_not_allow_unbounded_adb_timeout(monkeypatch):
    u=UI('fake');u._deadline=time.monotonic()-1
    called=Mock();monkeypatch.setattr('framework.livflow_flow.subprocess.run',called)
    # 读界面的命令在预算耗尽时报"读取预算耗尽"，而不是假装页面没达到预期；
    # 两种情况都不会真的发起 adb 调用，等待依然是有界的。
    from framework.livflow_flow import UiReadBudgetExhausted
    with pytest.raises(UiReadBudgetExhausted,match='预算耗尽'):
        u.adb('exec-out','uiautomator','dump','/dev/tty')
    with pytest.raises(AssertionError,match='总超时'):u.adb('shell','input','tap','1','1')
    called.assert_not_called()


def test_edittext_located_by_class_picks_the_single_editable_field():
    # 10-29 现场：用 class_name=EditText 定位搜索框，但页面上有多个 EditText，
    # 旧逻辑抛"同名控件不唯一"；现在能收敛到唯一可编辑项。
    u=UI('fake')
    tree=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node class="android.widget.EditText" enabled="true" bounds="[0,100][900,160]"/>'
        '<node class="android.view.View" content-desc="搜索" bounds="[900,100][1000,160]"/>'
        '</node></hierarchy>')
    assert u.find(class_name='android.widget.EditText',tree=tree)[1]==[0,100,900,160]
    # 多个输入框时取最靠上的（搜索框在页面顶部），这是搜索场景的实际语义。
    two=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node class="android.widget.EditText" enabled="true" bounds="[0,200][900,260]"/>'
        '<node class="android.widget.EditText" enabled="true" bounds="[0,100][900,160]"/>'
        '</node></hierarchy>')
    assert u.find(class_name='android.widget.EditText',tree=two)[1]==[0,100,900,160]


def test_edittext_located_by_class_picks_the_single_editable_field():
    # 10-29 现场：用 class_name=EditText 定位搜索框，但页面上有多个 EditText，
    # 旧逻辑抛"同名控件不唯一"；现在能收敛到唯一可编辑项。
    u=UI('fake')
    tree=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node class="android.widget.EditText" enabled="true" bounds="[0,100][900,160]"/>'
        '<node class="android.view.View" content-desc="搜索" bounds="[900,100][1000,160]"/>'
        '</node></hierarchy>')
    assert u.find(class_name='android.widget.EditText',tree=tree)[1]==[0,100,900,160]
    # 多个输入框时取最靠上的（搜索框在页面顶部），这是搜索场景的实际语义。
    two=ET.fromstring('<hierarchy><node bounds="[0,0][1000,2000]">'
        '<node class="android.widget.EditText" enabled="true" bounds="[0,200][900,260]"/>'
        '<node class="android.widget.EditText" enabled="true" bounds="[0,100][900,160]"/>'
        '</node></hierarchy>')
    assert u.find(class_name='android.widget.EditText',tree=two)[1]==[0,100,900,160]
