"""USB camera discovery. Enumeration never connects or starts capture."""
import os
from pathlib import Path
import pytest
from framework.livflow_flow import Flow, configuration

pytestmark = [pytest.mark.device, pytest.mark.flow_usb]


def test_usb_camera_empty_scan(pytestconfig,request):
    path=os.environ.get('APP_PATROL_FLOW_CONFIG',str(Path(__file__).parents[1]/'config/patrol.json'))
    flow=Flow(configuration(path),os.environ.get('ANDROID_UDID',''),pytestconfig.artifact_dir)
    flow.item=request.node
    flow.home()
    flow.ui.tap('设备')
    flow.ui.wait(lambda value:'我的设备' in value)
    flow.ui.seek('USB 相机',up=False)
    flow.ui.tap('USB 相机')
    flow.ui.wait(lambda value:'USB 设备' in value)
    if flow.ui.find('开始扫描'):
        flow.ui.tap('开始扫描')
    elif flow.ui.find('重新扫描'):
        flow.ui.tap('重新扫描')
    else:
        raise AssertionError('USB 页面没有发现扫描入口')
    text=flow.ui.wait(lambda value: '未发现设备' in value or '拍摄照片和录制视频' in value or
                      ('USB 设备' in value and '重新扫描' in value),timeout=45)
    if '拍摄照片和录制视频' in text:
        flow.pause('请在手机上处理 LivFlow 的相机权限请求，返回 USB 设备扫描页后继续。')
        text=flow.ui.wait(lambda value:'USB 设备' in value and ('未发现设备' in value or '重新扫描' in value),timeout=45)
    if '未发现设备' not in text:
        pytest.skip('本轮存在 USB 设备或空设备状态不明确，未验证无设备提示')
    flow.capture('usb_no_device')
    flow.note('未连接 USB 相机时，扫描后显示未发现设备；实际 USB 相机连接与采集未验证')
