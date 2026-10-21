"""LivFlow hardware smoke checks without an Appium server.

Run only this file with --device and ANDROID_UDID explicitly set.
Restarts LivFlow; never run during an active recording/upload.
"""
import os
import hashlib
import re
import subprocess
import time
import xml.etree.ElementTree as ET

import pytest

PACKAGE = 'com.livsyn.livsyn_collection_app'
pytestmark = [pytest.mark.device, pytest.mark.smoke]


class Android:
    def __init__(self, serial):
        self.serial = serial

    def command(self, *args):
        return subprocess.run(['adb', '-s', self.serial, *args],
                              capture_output=True, check=True, timeout=45).stdout

    def tree(self):
        self.command('shell', 'uiautomator', 'dump', '/sdcard/codex-livflow.xml')
        self.xml = self.command('shell', 'cat', '/sdcard/codex-livflow.xml')
        return ET.fromstring(self.xml)

    def labels(self):
        return [n.get('content-desc') or n.get('text', '') for n in self.tree().iter('node')]

    def tap(self, label):
        for n in self.tree().iter('node'):
            if n.get('content-desc') == label or n.get('text') == label:
                x1, y1, x2, y2 = map(int, re.findall(r'\d+', n.get('bounds')))
                self.command('shell', 'input', 'tap', str((x1+x2)//2), str((y1+y2)//2))
                return
        raise AssertionError(f'Missing control: {label}')

    def wait_label(self, label):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if label in self.labels():
                return
        raise AssertionError(f'Expected label not found: {label}')


@pytest.fixture
def android(request, pytestconfig):
    serial = os.environ.get('ANDROID_UDID')
    if not serial:
        pytest.fail('ANDROID_UDID must identify the intended test device')
    device = Android(serial)
    device.command('shell', 'am', 'force-stop', PACKAGE)
    device.command('shell', 'am', 'start', '-W', '-n', PACKAGE + '/.MainActivity')
    try:
        device.wait_label('欢迎登录')
        yield device
    finally:
        safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', request.node.name)
        safe_name += '_' + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:8]
        target = pytestconfig.artifact_dir / safe_name
        try:
            device.tree()
            target.with_suffix('.xml').write_bytes(device.xml)
            target.with_suffix('.png').write_bytes(device.command('exec-out', 'screencap', '-p'))
        except Exception as exc:
            request.node.add_report_section('teardown', 'Evidence failure', str(exc))


def test_empty_login_validation(android):
    android.tap('登录')
    android.wait_label('请输入手机号和密码')


@pytest.mark.parametrize(('tab', 'title'), [('任务', '任务中心'), ('设备', '蓝牙设备'), ('我的', '未登录\n采集无需登录，上传时登录')])
def test_guest_navigation(android, tab, title):
    android.tap('暂不登录，继续采集')
    android.wait_label('任务中心')
    android.tap(tab)
    android.wait_label(title)


def test_language_switch(android):
    android.tap('暂不登录，继续采集')
    android.wait_label('任务中心')
    android.tap('我的')
    android.wait_label('我的')
    android.tap('语言\n简体中文')
    android.wait_label('语言')
    try:
        android.tap('English')
        android.wait_label('Language')
        android.wait_label('Choose the app language. Changes take effect immediately.')
    finally:
        android.tap('简体中文')
    android.wait_label('语言')
