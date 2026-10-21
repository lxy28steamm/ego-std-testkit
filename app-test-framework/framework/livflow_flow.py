"""Guarded LivFlow workflows. Never accepts credentials or upgrades firmware."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

PACKAGE = 'com.livsyn.livsyn_collection_app'
PREFIX = '@@APP_PATROL@@'


class Cancelled(Exception):
    pass


def event(kind, **data):
    print(PREFIX + json.dumps(dict(type=kind, **data), ensure_ascii=False), flush=True)


def configuration(path):
    values = json.loads(Path(path).read_text(encoding='utf-8'))
    for key in ('task_id', 'device_sn'):
        value = values.get(key, '')
        if value and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
            raise ValueError(f'{key} 只能包含字母、数字、下划线和短横线')
    for key, low, high in [('record_seconds', 5, 120), ('background_seconds', 0, 30),
                            ('human_timeout', 10, 3600), ('wait_timeout', 5, 300)]:
        values[key] = int(values.get(key, {'record_seconds':15, 'background_seconds':5,
                                          'human_timeout':900, 'wait_timeout':90}[key]))
        if not low <= values[key] <= high:
            raise ValueError(f'{key} 必须在 {low}～{high} 范围内')
    return values


class UI:
    def __init__(self, serial, check_cancel=lambda: None):
        if not serial:
            raise ValueError('必须明确指定 ANDROID_UDID')
        self.serial = serial
        self.check_cancel = check_cancel

    def adb(self, *args, timeout=40):
        self.check_cancel()
        return subprocess.run(['adb', '-s', self.serial, *args], check=True, capture_output=True,
                              timeout=timeout).stdout

    def tree(self):
        self.adb('shell', 'uiautomator', 'dump', '/sdcard/codex-flow.xml')
        raw = self.adb('shell', 'cat', '/sdcard/codex-flow.xml')
        return ET.fromstring(raw)

    @staticmethod
    def label(node):
        return node.get('content-desc') or node.get('text', '')

    def text(self):
        return '\n'.join(self.label(n) for n in self.tree().iter('node'))

    def find(self, label=None, contains=False, class_name=None, tree=None):
        tree = tree if tree is not None else self.tree()
        tops = [list(map(int,re.findall(r'-?\d+',n.get('bounds','')))) for n in list(tree)]
        screen_right = max((b[2] for b in tops if len(b)==4),default=0)
        screen_bottom = max((b[3] for b in tops if len(b)==4),default=0)
        for n in tree.iter('node'):
            if n.get('resource-id') == 'android:id/navigationBarBackground':
                b=list(map(int,re.findall(r'-?\d+',n.get('bounds',''))))
                if len(b)==4 and b[0]==0 and b[2]>=screen_right and b[1]>0:
                    screen_bottom=min(screen_bottom,b[1])
        for node in tree.iter('node'):
            text = self.label(node)
            if class_name and node.get('class') != class_name:
                continue
            if label is not None and not ((label in text) if contains else label == text):
                continue
            bounds = list(map(int, re.findall(r'-?\d+', node.get('bounds', ''))))
            if len(bounds)==4:
                raw_height=bounds[3]-bounds[1]
                bounds = [max(0,bounds[0]),max(0,bounds[1]),min(screen_right,bounds[2]),min(screen_bottom,bounds[3])]
                if bounds[3]-bounds[1] < raw_height*.5:
                    continue
            if len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                return node, bounds
        return None

    def tap(self, label=None, *, contains=False, class_name=None):
        found = self.find(label, contains=contains, class_name=class_name)
        deadline=time.monotonic()+10
        while not found and time.monotonic()<deadline:
            self.check_cancel()
            time.sleep(.2)
            found=self.find(label,contains=contains,class_name=class_name)
        if not found:
            raise AssertionError(f'当前页面没有可见控件：{label or class_name}')
        _, (x1, y1, x2, y2) = found
        self.adb('shell', 'input', 'tap', str((x1+x2)//2), str((y1+y2)//2))

    def wait_control(self,label,timeout=30):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.check_cancel()
            found=self.find(label)
            if found:
                return found
            time.sleep(.2)
        raise AssertionError('等待控件超时：'+label)

    def hide_keyboard(self):
        info=self.adb('shell','dumpsys','input_method').decode('utf-8',errors='replace')
        if re.search(r'(?:mInputShown|inputShown|isInputViewShown)\s*=\s*true',info):
            self.adb('shell','input','keyevent','4')

    def swipe(self, up=True):
        root = self.tree()
        bounds = list(map(int, re.findall(r'\d+', next(root.iter('node')).get('bounds', ''))))
        w, h = bounds[2], bounds[3]
        start, end = (int(h*.73), int(h*.30)) if up else (int(h*.30), int(h*.73))
        self.adb('shell', 'input', 'swipe', str(int(w*.73)), str(start), str(int(w*.73)), str(end), '400')

    def seek(self, label, up=True, limit=8):
        for _ in range(limit):
            if self.find(label):
                return
            self.swipe(up)
        raise AssertionError(f'滚动后仍未找到：{label}')

    def wait(self, condition, timeout=30, message='页面未达到预期状态'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_cancel()
            text = self.text()
            if condition(text):
                return text
            time.sleep(.3)
        raise AssertionError(message)


class Flow:
    def __init__(self, config, serial, output):
        self.config, self.output = config, Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.suppressed = False
        self.ui = UI(serial, self.check_cancel)
        self.index = 0
        self.item = None
        self.record_requested = False
        self.needs_attention = False
        self.upload_started = False

    def check_cancel(self):
        path = os.environ.get('APP_PATROL_CANCEL_FILE')
        if not self.suppressed and path and Path(path).exists():
            raise Cancelled('用户停止本轮执行')

    @contextmanager
    def cleanup(self):
        old = self.suppressed
        self.suppressed = True
        try:
            yield
        finally:
            self.suppressed = old

    def note(self, text, observation=False):
        event('step', message=text)
        if observation and self.item is not None:
            self.item.patrol_observations = getattr(self.item, 'patrol_observations', []) + [text]

    def capture(self, title):
        self.index += 1
        stem = f'flow_{self.index:03}_{re.sub(r"[^A-Za-z0-9_-]", "_", title)}'
        root = self.ui.tree()
        # Do not persist login forms, password fields or inputted credentials.
        if any(n.get('password') == 'true' for n in root.iter('node')) or '设备日志' in '\n'.join(UI.label(n) for n in root.iter('node')):
            self.note('当前页面含密码输入框，跳过截图取证')
            return
        (self.output/(stem+'.xml')).write_bytes(ET.tostring(root, encoding='utf-8'))
        (self.output/(stem+'.png')).write_bytes(self.ui.adb('exec-out', 'screencap', '-p'))
        self.note('证据已保存：' + stem)

    def pause(self, message):
        gate = uuid.uuid4().hex
        resume = self.output/'resume.json'
        event('waiting', gate=gate, message=message)
        deadline = time.monotonic() + self.config['human_timeout']
        try:
            while time.monotonic() < deadline:
                self.check_cancel()
                if resume.exists():
                    try:
                        if json.loads(resume.read_text(encoding='utf-8')).get('gate') == gate:
                            return
                    except (OSError, ValueError):
                        pass
                time.sleep(.2)
            raise AssertionError('等待人工操作超时：' + message)
        finally:
            event('resumed', gate=gate)

    def home(self):
        self.ui.adb('shell', 'am', 'start', '-W', '-n', PACKAGE+'/.MainActivity')
        self.ui.hide_keyboard()
        for _ in range(7):
            root = self.ui.tree()
            packages={n.get('package','') for n in root.iter('node')}
            if any('documentsui' in package for package in packages):
                self.ui.adb('shell','input','keyevent','4')
                continue
            if self.ui.find('任务', tree=root) and self.ui.find('我的', tree=root):
                return
            if self.ui.find('欢迎登录', tree=root):
                self.pause('请在手机直接登录测试账号并选择身份，进入 APP 主页后点击“已完成，继续”。不要在工作台输入密码。')
                continue
            if self.ui.find('返回', tree=root):
                self.ui.tap('返回')
            else:
                self.pause('请将 LivFlow 切为简体中文并返回带“任务、设备、我的”底部导航的主页，再继续。')
        raise AssertionError('未能回到 APP 主页')

    def tasks(self):
        self.home()
        self.ui.tap('任务')
        text = self.ui.wait(lambda x: '任务中心' in x)
        if '请先登录' in text:
            self.pause('请点击手机任务页的登录，完成测试账号登录后继续。')
            self.home()
            self.ui.tap('任务')
        return self.ui.wait(lambda x: '任务中心' in x and '请先登录' not in x)

    def target_task(self):
        task = self.config.get('task_id', '')
        if not task:
            raise AssertionError('请先在“流程配置”填写本轮测试任务编号，不能自动选择任意任务')
        self.tasks()
        if self.ui.find('清空搜索'):
            self.ui.tap('清空搜索')
        self.ui.tap(class_name='android.widget.EditText')
        self.ui.adb('shell', 'input', 'text', task)
        self.ui.hide_keyboard()
        self.ui.tap('搜索')
        text = self.ui.wait(lambda x: task+'\n' in x and '共 1 个任务' in x)
        assert task+'\n' in text
        return task

    def search_reset(self):
        task = self.target_task()
        self.capture('task_search')
        self.ui.tap('清空搜索')
        self.ui.wait(lambda x: '任务中心' in x and task in x)
        self.capture('task_search_reset')

    def filters(self):
        self.tasks()
        self.ui.tap('状态, 筛选状态\n状态')
        self.ui.wait(lambda x: '全部状态' in x)
        self.ui.tap('待开始')
        self.ui.wait(lambda x: '待开始, 筛选状态' in x)
        self.capture('status_filter')
        self.ui.tap('重置')
        self.ui.wait(lambda x: '状态, 筛选状态\n状态' in x)
        self.ui.tap('时长, 筛选时长\n时长')
        self.ui.wait(lambda x: '全部时长' in x)
        self.ui.tap('<30min')
        self.ui.wait(lambda x: '<30min, 筛选时长' in x)
        self.capture('duration_filter')
        self.ui.tap('重置')
        self.ui.wait(lambda x: '时长, 筛选时长\n时长' in x)

    def detail(self):
        task = self.target_task()
        root = self.ui.tree()
        row = next((n for n in root.iter('node') if UI.label(n).startswith(task+'\n')
                    and len(UI.label(n).splitlines()) <= 5), None)
        assert row is not None, '任务卡片不可见，拒绝点击其他任务'
        self.ui.tap(UI.label(row))
        text = self.ui.wait(lambda x: '任务详情' in x and task in x)
        assert '任务目标时长' in text
        self.ui.tap('刷新统计')
        self.capture('task_detail_statistics')

    def language(self):
        self.home()
        self.ui.tap('我的')
        self.ui.wait(lambda x:'采集统计' in x or '采集无需登录' in x)
        self.ui.seek('语言\n简体中文')
        self.ui.tap('语言\n简体中文')
        self.ui.wait(lambda x: '选择应用显示语言' in x)
        try:
            self.ui.tap('English')
            self.ui.wait(lambda x: 'Changes take effect immediately.' in x)
            self.capture('language_english')
        finally:
            with self.cleanup():
                if self.ui.find('简体中文'):
                    self.ui.tap('简体中文')
        self.ui.wait(lambda x: '选择应用显示语言' in x)

    def about_logs(self):
        self.home()
        self.ui.tap('我的')
        self.ui.wait(lambda x:'采集统计' in x or '采集无需登录' in x)
        self.ui.seek('关于我们\n了解LivFlow')
        self.ui.tap('关于我们\n了解LivFlow')
        package = self.ui.adb('shell','dumpsys','package',PACKAGE).decode('utf-8',errors='replace')
        match = re.search(r'versionName=([^\s]+)',package)
        assert match, '未能读取已安装 APP 的版本'
        version = match[1]
        self.ui.wait(lambda x: '当前版本' in x and 'v'+version in x)
        self.capture('about')
        self.ui.tap('返回')
        self.ui.seek('设备日志\n查看运行与操作记录', up=False)
        self.ui.tap('设备日志\n查看运行与操作记录')
        self.ui.wait(lambda x: '刷新日志' in x)
        self.ui.tap('异常')
        self.ui.wait(lambda x: '刷新日志' in x)
        self.ui.tap('刷新日志')
        # Log bodies may contain sensitive details. Persist filenames, never log text/screenshots.
        self.ui.tap('导出 .log')
        self.ui.wait_control('保存',timeout=self.config['wait_timeout'])
        root = self.ui.tree()
        found = self.ui.find(class_name='android.widget.EditText', tree=root)
        assert found, '日志保存页没有文件名'
        name = UI.label(found[0])
        assert re.fullmatch(r'LivFlow-device-[A-Za-z0-9_-]+[.]log', name), '保存文件名不符合预期，停止以免覆盖其他文件'
        self.ui.tap('保存')
        self.ui.wait(lambda x: '日志已导出' in x or '刷新日志' in x)
        listing = self.ui.adb('shell', 'ls', '/sdcard/Download').decode('utf-8', errors='replace').splitlines()
        assert name in listing or name+'.txt' in listing, '未在下载目录找到导出文件'
        if name+'.txt' in listing:
            self.note('日志导出实际后缀为 .log.txt，与页面显示的 .log 不一致', observation=True)
        self.note('日志已保存到手机；未读取或复制日志正文')

    def connect(self):
        sn = self.config.get('device_sn', '')
        if not sn:
            raise AssertionError('请在流程配置填写指定 EGO 序列号，不能连接任意附近设备')
        self.home()
        self.ui.tap('设备')
        text = self.ui.wait(lambda x: '我的设备' in x)
        if '当前设备' in text:
            assert 'SN: '+sn in text, '当前连接的不是指定 EGO，停止'
            for _ in range(7):
                if self.ui.find('查看设备'):
                    self.ui.tap('查看设备')
                    self.ui.wait(lambda x:'左目 ·' in x and '右目 ·' in x,timeout=self.config['wait_timeout'])
                    self.capture('connected_preview')
                    return
                if self.ui.find('继续配网'):
                    break
                self.ui.swipe()
            assert self.ui.find('继续配网'), '没有找到当前设备的配网或查看入口'
            self.ui.tap('继续配网')
        else:
            if self.ui.find('蓝牙设备'):
                self.ui.tap('蓝牙设备')
            self.ui.tap('开始扫描' if self.ui.find('开始扫描') else '重新扫描')
            self.ui.wait(lambda x: 'SN: '+sn in x, timeout=self.config['wait_timeout'], message='未发现指定 EGO')
            # The flow only permits a single discovered EGO because Flutter merges scan cards.
            text = self.ui.text()
            assert text.count('SN: ') == 1, '附近有多台设备，请在手机手动连接指定 SN 后重新运行'
            self.ui.seek('连接设备')
            self.ui.tap('连接设备')
        self.ui.wait(lambda x: '设备配网' in x, timeout=self.config['wait_timeout'])
        ssid = self.config.get('ssid', '')
        assert ssid, '请配置测试 Wi-Fi 名称'
        current = self.ui.text()
        buttons = [UI.label(n) for n in self.ui.tree().iter('node') if n.get('class') == 'android.widget.Button']
        wifi = next((x for x in buttons if x == ssid or x.startswith(ssid+' ·')), None)
        if not wifi:
            current_button = next((x for x in buttons if '本机已连接' in x), None)
            if not current_button:
                self.pause('请在手机配网页选择本轮指定 Wi-Fi，完成选择后继续。')
            else:
                self.ui.tap(current_button)
                self.ui.wait(lambda x: ssid in x)
                self.ui.tap(ssid)
        self.ui.seek('免密码网络')
        root = self.ui.tree()
        check = self.ui.find('免密码网络', tree=root)
        if not check or check[0].get('checked') != 'true':
            self.pause('请在手机填写该测试网络的密码，或确认它是开放网络后勾选免密码。完成后继续，不要把密码填入工作台。')
        self.ui.tap('连接网络')
        self.note('已请求指定 EGO 配网；若手机不是同一网络，请按提示切换')
        try:
            self.ui.wait(lambda x: '视频预览' in x and '设备配网' not in x, timeout=25)
        except AssertionError:
            self.pause('请确认手机与 EGO 已连接同一个测试 Wi-Fi；若手机有连接提示请处理，APP 进入视频预览后继续。')
        text = self.ui.wait(lambda x: '左目 ·' in x and '右目 ·' in x, timeout=self.config['wait_timeout'])
        if '左右画面暂未同步' in text:
            self.note('APP 提示独立双目预览，左右画面暂未同步；不据此判定录制文件同步情况', observation=True)
        self.capture('connected_preview')

    def stop_recording(self):
        with self.cleanup():
            if self.ui.find('结束采集'):
                self.ui.tap('结束采集')
            try:
                text = self.ui.wait(lambda x: '采集已保存到设备 TF 卡' in x and '开始采集' in x,
                                    timeout=self.config['wait_timeout'], message='未能确认采集结束，请人工检查 EGO')
            except Exception:
                self.needs_attention = True
                raise
            self.record_requested = False
            return text

    def record(self):
        self.pause('请确认 TF 卡已放回指定 EGO、设备可录制。若卡还接在手机上，请先安全弹出并放回设备，再继续。')
        self.connect()
        self.ui.tap('进入任务中心')
        self.detail()
        if self.ui.find('开始任务'):
            self.ui.tap('开始任务')
            self.ui.wait_control('开始采集', timeout=self.config['wait_timeout'])
        self.ui.wait_control('开始采集', timeout=15)
        self.record_requested = True
        try:
            self.ui.tap('开始采集')
            self.ui.wait_control('结束采集', timeout=min(20,self.config['wait_timeout']))
            self.capture('record_started')
            start = time.monotonic()
            background = min(self.config['background_seconds'], self.config['record_seconds'])
            if background:
                self.ui.adb('shell', 'input', 'keyevent', '3')
                self.sleep(background)
                self.ui.adb('shell', 'am', 'start', '-W', '-n', PACKAGE+'/.MainActivity')
                self.ui.wait_control('结束采集', timeout=20)
                self.capture('record_return_from_background')
            self.sleep(max(0, self.config['record_seconds']-(time.monotonic()-start)))
        finally:
            if self.record_requested:
                self.stop_recording()
        self.capture('record_stopped')
        self.note('采集控制流程完成；文件是否完整需要插入 TF 卡后另行校验')

    def sleep(self, seconds):
        until = time.monotonic()+seconds
        while time.monotonic() < until:
            self.check_cancel()
            time.sleep(.15)

    def parameters(self):
        self.connect()
        self.ui.tap('进入任务中心')
        self.home()
        self.ui.tap('我的')
        self.ui.seek('设备参数\n视频、音频、IMU')
        self.ui.tap('设备参数\n视频、音频、IMU')
        self.ui.wait(lambda x: '相机参数' in x)
        self.capture('parameters_loaded')
        original = None
        for _ in range(10):
            root = self.ui.tree()
            original = next((x for x in ('1 kHz', '400 Hz') if self.ui.find(x, tree=root)), None)
            if original:
                break
            self.ui.swipe()
        assert original, '未找到 IMU 频率选项'
        alternate = '400 Hz' if original == '1 kHz' else '1 kHz'
        changed = False
        try:
            self.ui.tap(original)
            self.ui.tap(alternate)
            changed = True
            self.ui.tap('保存参数')
            self.ui.wait(lambda x: '设备参数已应用' in x or '重新读取参数' in x)
            self.ui.seek('重新读取参数',up=False)
            self.ui.tap('重新读取参数')
            self.ui.seek(alternate)
            self.capture('parameters_alternate')
        finally:
            if changed:
                with self.cleanup():
                    try:
                        self.ui.seek(alternate)
                        self.ui.tap(alternate)
                        self.ui.tap(original)
                        self.ui.tap('保存参数')
                        self.ui.wait(lambda x: '重新读取参数' in x)
                        self.ui.seek('重新读取参数', up=False)
                        self.ui.tap('重新读取参数')
                        self.ui.seek(original)
                        self.capture('parameters_restored')
                        self.note('IMU 配置已恢复为 '+original+'，并重新读取；不代表实际采样频率已验证')
                    except Exception:
                        self.needs_attention=True
                        self.note('未能回读确认 IMU 原值，停止后续流程，请人工检查设备')
                        raise

    def upload(self):
        self.detail()
        for _ in range(5):
            self.ui.swipe()
        root = self.ui.tree()
        merged = next((n for n in root.iter('node') if '选择 TF 卡文件 · 查看上传进度与结果' in UI.label(n)), None)
        assert merged is not None, '未识别采集数据卡片，停止'
        # Flutter merges this card into the page. Guard route and text, scroll to bottom,
        # then derive a hit point from the actual content bounds rather than fixed pixels.
        x1,y1,x2,y2 = map(int,re.findall(r'\d+',merged.get('bounds')))
        self.ui.adb('shell','input','tap',str((x1+x2)//2),str(y2-min(60,(y2-y1)//6)))
        self.ui.wait(lambda x: '采集数据' in x and '选择 TF 卡文件' in x)
        self.ui.tap('选择 TF 卡文件')
        self.ui.wait(lambda x: 'TF 卡采集文件' in x)
        self.pause('请把本轮测试 TF 卡接入手机，在 APP 选择本轮可上传的单条采集目录并允许读写；不要选择历史数据或整卡。完成后继续。')
        text = self.ui.text()
        assert 'TF 卡采集文件' in text, '请完成手机上的目录授权后再继续'
        if '已自动跳过' in text and '没有待上传' in text:
            import pytest
            pytest.skip('选择的记录已经上传，未重复提交；本轮实际上传未测')
        assert 'MCAP 1 · MP4 0' in text, '本流程仅允许选择一个 MCAP，避免批量改写历史文件'
        match = re.search(r'([A-Fa-f0-9-]+):/(EGO_[A-Za-z0-9_]+)', text)
        assert match, '没有识别 TF 卡上的明确采集目录'
        source = '/storage/'+match[1]+'/'+match[2]
        configured = self.config.get('recording_dir', '')
        if configured:
            assert source == configured.rstrip('/'), '手机所选目录与本轮配置不一致'
        sn = self.config.get('device_sn', '')
        if sn:
            assert match[2].startswith('EGO_'+sn+'_'), '所选记录不属于指定 EGO'
        before_dir = self.output/'recording_before'
        before_dir.mkdir()
        self.ui.adb('pull', source, str(before_dir), timeout=120)
        originals = list(before_dir.rglob('*.mcap'))
        assert len(originals)==1, '目录中MCAP不唯一，停止'
        from framework.recording_check import inspect, compare
        before = inspect(originals[0])
        self.note('上传前 MCAP 已备份并通过完整读取和 CRC 检查')
        if not self.ui.find('取消全选'):
            self.ui.tap('全选')
        self.ui.wait(lambda x: '已选 1 组采集' in x)
        self.capture('upload_selected')
        action = '写入元数据并上传' if self.ui.find('写入元数据并上传') else '开始任务并上传'
        self.ui.wait_control(action,timeout=15)
        self.ui.tap(action)
        self.upload_started = True
        filename = originals[0].name
        self.ui.wait(lambda x: '采集数据' in x and filename in x, timeout=120)
        self.ui.adb('shell','input','keyevent','3')
        self.sleep(5)
        self.ui.adb('shell','am','start','-W','-n',PACKAGE+'/.MainActivity')
        text = self.ui.wait(lambda x: filename in x and ('0 个上传中' in x or '上传失败' in x),
                            timeout=300, message='上传尚未结束；请保留卡和网络，在APP中确认状态')
        assert '上传失败' not in text
        # The selected filename is the first/new record. Require the corresponding card's state.
        tail = text.split(filename,1)[1].split('EGO_',1)[0]
        assert '已上传' in tail, '没有找到本轮文件的上传完成状态'
        self.upload_started = False
        self.capture('upload_complete')
        after_dir = self.output/'recording_after'
        after_dir.mkdir()
        self.ui.adb('pull',source,str(after_dir),timeout=120)
        after_file = next(after_dir.rglob(filename))
        result = compare(originals[0], after_file)
        (self.output/'recording_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        assert result['messages_unchanged'], '元数据写入前后传感器消息或时间戳不一致'
        assert result['source_content_matches'], '写入的标定/采集元数据与源文件不一致'
        self.ui.tap('选择 TF 卡文件')
        self.ui.wait(lambda x:'已自动跳过 1 个已上传 MCAP 文件' in x)
        self.capture('duplicate_upload_blocked')
        self.note('实际上传及重复拦截完成；云端转换、质检和云端文件哈希未独立验证')
