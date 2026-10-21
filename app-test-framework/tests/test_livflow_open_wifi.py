"""Read-only assertions for an already connected passwordless LivFlow preview."""
import os
import subprocess
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.device


@pytest.fixture(scope='module')
def preview(pytestconfig):
    serial = os.environ.get('ANDROID_UDID')
    if not serial:
        pytest.fail('Set ANDROID_UDID explicitly')

    def adb(*args):
        return subprocess.run(['adb', '-s', serial, *args], check=True,
                              capture_output=True, timeout=40).stdout

    wifi = adb('shell', 'cmd', 'wifi', 'status').decode('utf-8')
    adb('shell', 'uiautomator', 'dump', '/sdcard/codex-livflow.xml')
    xml = adb('shell', 'cat', '/sdcard/codex-livflow.xml')
    folder = pytestconfig.artifact_dir
    (folder / 'open-wifi-status.txt').write_text(wifi, encoding='utf-8')
    (folder / 'preview.xml').write_bytes(xml)
    (folder / 'preview.png').write_bytes(adb('exec-out', 'screencap', '-p'))
    labels = '\n'.join(n.get('content-desc', '') for n in ET.fromstring(xml).iter('node'))
    return wifi, labels


def test_phone_connected_to_open_wifi(preview):
    wifi, _ = preview
    assert 'Wifi is connected to "灵生测试"' in wifi
    assert 'Security type: 0,' in wifi
    assert 'Supplicant state: COMPLETED' in wifi


def test_stereo_preview(preview):
    _, labels = preview
    assert '视频预览' in labels
    assert '视频通道 · 预览中' in labels
    assert '左目 · 1600 × 1200' in labels
    assert '右目 · 1600 × 1200' in labels


def test_audio_and_imu_status(preview):
    _, labels = preview
    assert '音频状态 · 收流正常' in labels
    assert 'IMU 加速度计（m/s²） · 收流正常' in labels
    assert 'IMU 陀螺仪（rad/s） · 收流正常' in labels
