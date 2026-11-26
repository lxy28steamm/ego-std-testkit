"""Guest checks with logout preparation for empty-login validation."""
import os
from pathlib import Path
import pytest
from framework.livflow_flow import Flow, configuration

pytestmark = [pytest.mark.device, pytest.mark.flow_guest]


@pytest.fixture(scope='session')
def guest_flow(pytestconfig):
    config_path=os.environ.get('APP_PATROL_FLOW_CONFIG',str(Path(__file__).parents[1]/'config/patrol.json'))
    return Flow(configuration(config_path),os.environ.get('ANDROID_UDID',''),pytestconfig.artifact_dir)


def guest_tasks(flow):
    root=flow.ui.tree()
    if flow.ui.find('暂不登录，继续采集',tree=root):
        inputs=[n for n in root.iter('node') if n.get('class')=='android.widget.EditText']
        if any(n.get('text','') for n in inputs):
            pytest.skip('登录页面已有输入，不替用户切换或清除输入')
        flow.ui.tap('暂不登录，继续采集')
    else:
        flow.home()
    flow.ui.tap('任务')
    text=flow.ui.wait(lambda value:'任务中心' in value)
    if '请先登录' not in text:
        pytest.skip('当前不是游客状态；不自动退出账号')
    return text


def test_guest_empty_login(guest_flow,request):
    flow=guest_flow
    flow.item=request.node
    flow.prepare_login_form()
    # Entering the login form from the task page can open the software keyboard,
    # which otherwise obscures the submit button on landscape devices.
    flow.ui.hide_keyboard()
    root=flow.ui.tree()
    inputs=[n for n in root.iter('node') if n.get('class')=='android.widget.EditText']
    if len(inputs)<2 or any(n.get('text','') for n in inputs):
        pytest.skip('登录输入框已有内容或无法确认为空，不清除用户输入')
    try:
        action='登录' if flow.ui.find('登录') else '登录并继续上传'
        flow.ui.tap(action)
        flow.ui.wait(lambda value:'请输入手机号和密码' in value)
        flow.note('空手机号和密码未通过校验；登录页面不保存截图或输入内容')
    finally:
        if flow.ui.find('暂不登录，继续采集'):
            flow.ui.tap('暂不登录，继续采集')


def test_guest_task_center(guest_flow,request):
    flow=guest_flow
    flow.item=request.node
    text=guest_tasks(flow)
    assert '任务中心' in text and '请先登录' in text
    if '登录已失效' in text:
        flow.note('游客进入任务页出现“登录已失效，请重新登录”，需确认是否符合产品预期',observation=True)
    flow.capture('guest_task_center')


def test_guest_device_modes(guest_flow,request):
    flow=guest_flow
    flow.item=request.node
    guest_tasks(flow)
    flow.ui.tap('设备')
    text=flow.ui.wait(lambda value:'我的设备' in value and '蓝牙设备' in value)
    assert '开始扫描' in text
    flow.ui.tap('USB 相机')
    text=flow.ui.wait(lambda value:'USB 设备' in value)
    assert '开始扫描' in text
    flow.capture('guest_usb_mode')
    flow.ui.tap('蓝牙设备')
    flow.ui.wait(lambda value:'附近设备' in value)
    flow.note('蓝牙与USB入口可切换；未凭页面显示判定外设连接或采集通过')


def test_guest_profile_entries(guest_flow,request):
    flow=guest_flow
    flow.item=request.node
    guest_tasks(flow)
    flow.ui.tap('我的')
    text=flow.ui.wait(lambda value:'未登录' in value and '设备参数' in value)
    for label in ('网络设置','固件升级','语言','设备日志'):
        assert label in text, '游客“我的”页缺少入口：'+label
    flow.capture('guest_profile')
