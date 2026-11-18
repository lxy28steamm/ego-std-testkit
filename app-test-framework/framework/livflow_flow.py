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
from framework.livflow_state import classify, fingerprint, NAMES
from framework import ocr


class Cancelled(Exception):
    pass


class DuplicateControl(AssertionError):
    """同名控件收敛后仍不唯一。保留原始关键字，方便上层判断"是不是目标真的出现了"。"""

    def __init__(self, name, bounds):
        self.name = name
        self.bounds = bounds
        super().__init__('同名控件不唯一，停止点击：%s（%d 个候选，无法用上下文收敛）' % (name, len(bounds)))


class UiReadBudgetExhausted(RuntimeError):
    """读界面本身把等待预算吃完了，不代表页面状态不符合预期。"""


def version_matches(text, version):
    """Match a full installed version with an optional case-insensitive v prefix."""
    return bool(re.search(r'(?<![A-Za-z0-9.])v?'+re.escape(version)+r'(?![0-9.])',text,re.IGNORECASE))


def parse_phone_wifi_status(text):
    ssid = re.search(r'Wifi is connected to "([^"\r\n]+)"',text)
    security = re.search(r'Security type:\s*(\d+)',text)
    return (ssid.group(1) if ssid else None, int(security.group(1)) if security else None)


def task_list_is_restored(text, search_value):
    return ('任务中心' in text and re.search(r'共\s*\d+\s*个任务',text) is not None
            and not search_value.strip())


def event(kind, **data):
    print(PREFIX + json.dumps(dict(type=kind, **data), ensure_ascii=False), flush=True)


def configuration(path):
    values = json.loads(Path(path).read_text(encoding='utf-8'))
    values.setdefault('wifi_security','open')
    if values['wifi_security'] not in ('open','protected'):
        raise ValueError('wifi_security 必须为 open 或 protected')
    values.setdefault('require_wifi_provisioning',False)
    if not isinstance(values['require_wifi_provisioning'],bool):
        raise ValueError('require_wifi_provisioning 必须是布尔值')
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
        self._deadline = None
        self.last_state = None

    def adb(self, *args, timeout=40):
        self.check_cancel()
        if self._deadline is not None:
            remaining=self._deadline-time.monotonic()
            if remaining<=0:
                # 区分"真的没等到目标"和"读界面太慢把预算耗光"。
                # 后者不是业务事实，报成页面超时会让人误以为 APP 卡住。
                if len(args)>=2 and args[0]=='exec-out' and args[1] in ('uiautomator','screencap'):
                    raise UiReadBudgetExhausted('界面读取预算耗尽（页面本身可能正常，只是 dump 太慢）')
                raise AssertionError('页面等待已达到总超时上限')
            timeout=min(timeout,remaining)
        return subprocess.run(['adb', '-s', self.serial, *args], check=True, capture_output=True,
                              timeout=timeout).stdout

    # uiautomator dump 在 Flutter 页面上会偶发失败：
    # "could not get idle state"、shell "Killed"（OOM）、以及上一次 dump 残留的 uidump 锁。
    # 这些都不是"手机没解锁"，以前一失败就 RuntimeError，整条用例直接判失败、丢失真实结论。
    _DUMP_RETRY_MARKERS = (b'could not get idle state', b'ERROR:', b'Killed',
                           b'null root node', b'Access denied')
    _DUMP_ATTEMPTS = 6
    _DUMP_SPACING = (0.3, 0.6, 1.0, 1.5, 2.0)
    _ocr_fallback = False

    def _dump_once(self, timeout):
        raw = self.adb('exec-out', 'uiautomator', 'dump', '/dev/tty', timeout=timeout)
        start, end = raw.find(b'<hierarchy'), raw.rfind(b'</hierarchy>')
        if start >= 0 and end >= start:
            return ET.fromstring(raw[start:end+len(b'</hierarchy>')])
        tail = raw[-260:].decode('utf-8', 'replace').replace('\n', ' ').strip()
        raise RuntimeError('dump 未返回完整 hierarchy：' + (tail or '输出为空'))

    def _ocr_tree(self):
        """控件树彻底拿不到时的兜底：用无障碍窗口转储 + 截图 OCR 认字。
        只在 dump 连续失败后启用，宁可慢也要拿到可判定的结果，而不是直接判失败。"""
        if UI._ocr_fallback:
            raise RuntimeError('界面读取已降级为 OCR 兜底，仍无法识别当前页面')
        words = None
        try:
            words = list(self._ocr_words())
        except Exception:
            words = None
        if not words:
            raise RuntimeError('界面读取失败：uiautomator dump 连续失败，OCR 兜底也不可用')
        UI._ocr_fallback = True
        try:
            return ocr.build_tree(words)
        finally:
            UI._ocr_fallback = False

    def _ocr_words(self):
        import io
        from PIL import Image
        png = io.BytesIO(self.adb('exec-out', 'screencap', '-p', timeout=20))
        image = Image.open(png).convert('RGB')
        return ocr.recognize(image)

    def tree(self):
        # Stream the hierarchy instead of leaving typed account or password
        # fields in a shared-storage XML file on the phone.
        last = ''
        for attempt in range(self._DUMP_ATTEMPTS):
            self.check_cancel()
            try:
                return self._dump_once(20)
            except subprocess.TimeoutExpired:
                last = 'dump 超时'
            except subprocess.CalledProcessError as exc:
                tail = (exc.stderr or b'')[-200:].decode('utf-8', 'replace').replace('\n', ' ').strip()
                last = 'dump 退出码 %s %s' % (exc.returncode, tail)
            except ET.ParseError as exc:
                last = 'XML 解析失败：' + str(exc)
            except RuntimeError as exc:
                last = str(exc)
            if attempt < self._DUMP_ATTEMPTS - 1:
                time.sleep(self._DUMP_SPACING[min(attempt, len(self._DUMP_SPACING) - 1)])
        # dump 六次都拿不到才降级，避免正常页面被 OCR 结果覆盖。
        try:
            return self._ocr_tree()
        except Exception:
            pass
        raise RuntimeError('无法安全读取手机界面（已重试 %d 次）：%s；'
                           '请确认手机已解锁、屏幕常亮，且没有其他工具占用 uiautomator'
                           % (self._DUMP_ATTEMPTS, last))

    @staticmethod
    def label(node):
        return node.get('content-desc') or node.get('text', '')

    def text(self):
        return '\n'.join(self.label(n) for n in self.tree().iter('node'))

    def find(self, label=None, contains=False, class_name=None, tree=None, resource_id=None,
             container=None):
        tree = tree if tree is not None else self.tree()
        tops = [list(map(int,re.findall(r'-?\d+',n.get('bounds','')))) for n in list(tree)]
        screen_right = max((b[2] for b in tops if len(b)==4),default=0)
        screen_bottom = max((b[3] for b in tops if len(b)==4),default=0)
        for n in tree.iter('node'):
            if n.get('resource-id') == 'android:id/navigationBarBackground':
                b=list(map(int,re.findall(r'-?\d+',n.get('bounds',''))))
                if len(b)==4 and b[0]==0 and b[2]>=screen_right and b[1]>0:
                    screen_bottom=min(screen_bottom,b[1])
        candidates=[]
        for node in tree.iter('node'):
            if node.get('enabled')=='false' or node.get('visible-to-user')=='false':continue
            if resource_id and node.get('resource-id')!=resource_id:continue
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
                candidates.append((node,bounds))
        if not candidates:return None
        clickable=[x for x in candidates if x[0].get('clickable')=='true']
        candidates=clickable or candidates
        unique={tuple(b):(n,b) for n,b in candidates}
        if len(unique)==1:
            return next(iter(unique.values()))
        # 重名不再直接判死。Flutter 页面经常出现两个同名控件（比如卡片标题和
        # 它自己的无障碍代理），坐标还不同，但语义上只有一个是真的入口。
        # 先用上下文收敛，收敛不到唯一才拒绝点击。
        narrowed = self._narrow(tree, unique, container, class_name)
        if narrowed is None:
            raise DuplicateControl(label or resource_id or class_name,
                                   [v[1] for v in unique.values()])
        return narrowed

    def _narrow(self, tree, unique, container, class_name=None):
        """Reduce duplicate candidates using positive evidence only.

        1. 祖先里带 container 文字的优先（例如“SN: xxx”下面那个“连接设备”）。
        2. 在可滚动区域内、且竖直方向更靠上的优先（列表入口通常在装饰元素上方）。
        3. 面积最接近常见可点击控件尺寸的优先。
        任何一步仍然并列就返回 None，绝不随机挑一个。
        """
        items = list(unique.values())
        # 按类型定位（如"页面上唯一的输入框"）时，多个同类控件无法用祖先/位置判断，
        # 只能取可编辑且当前有焦点的那个；没有焦点就宁可报错，不猜。
        if class_name and len(items) > 1:
            focused = [x for x in items
                       if x[0].get('focused') == 'true' or x[0].get('focusable') == 'true'
                       and x[0].get('focused') == 'true']
            if len(focused) == 1:
                return focused[0]
            editable = [x for x in items if x[0].get('class', '').endswith('EditText')
                        and x[0].get('enabled', 'true') != 'false']
            if len(editable) == 1:
                return editable[0]
        if container:
            owners = [n for n in tree.iter('node') if container in self.label(n)]
            inside = [x for x in items
                      if any(x[0] in list(owner.iter('node')) for owner in owners)]
            if len(inside) == 1:
                return inside[0]
            if inside:
                items = inside
        if len(items) > 1:
            top = min(x[1][1] for x in items)
            highest = [x for x in items if x[1][1] == top]
            if len(highest) == 1:
                return highest[0]
            items = highest
        if len(items) > 1:
            def area(x):
                b = x[1]
                return (b[2]-b[0]) * (b[3]-b[1])
            largest = max(area(x) for x in items)
            biggest = [x for x in items if area(x) == largest]
            if len(biggest) == 1:
                return biggest[0]
        return None

    @contextmanager
    def budget(self,timeout):
        old=self._deadline
        self._deadline=min(old,time.monotonic()+timeout) if old is not None else time.monotonic()+timeout
        try:yield
        finally:self._deadline=old

    def snapshot(self):
        tree=self.tree();state=classify(tree)
        self.last_state=state
        return tree,state,fingerprint(tree)

    def tap(self, label=None, *, contains=False, class_name=None, resource_id=None, container=None):
        deadline=time.monotonic()+10
        last_dup=None
        with self.budget(10):
            while time.monotonic()<deadline:
                self.check_cancel()
                tree,state,signature=self.snapshot()
                try:
                    found=self.find(label,contains=contains,class_name=class_name,
                                    resource_id=resource_id,tree=tree,container=container)
                except DuplicateControl as exc:
                    # 分不清就等一拍，再读一次；两次都分不清才报，避免一次抖动判死。
                    last_dup=exc
                    time.sleep(.25)
                    continue
                if found:
                    # Refresh just before action; never use coordinates from a stale hierarchy.
                    fresh,new_state,_=self.snapshot()
                    try:
                        again=self.find(label,contains=contains,class_name=class_name,
                                        resource_id=resource_id,tree=fresh,container=container)
                    except DuplicateControl as exc:
                        last_dup=exc
                        again=None
                    if again and again[1]==found[1] and new_state==state:
                        _,(x1,y1,x2,y2)=again
                        self.adb('shell','input','tap',str((x1+x2)//2),str((y1+y2)//2))
                        return
                time.sleep(.2)
        if last_dup is not None:
            self._record_duplicate(last_dup)
            raise last_dup
        raise AssertionError(f'当前页面没有稳定可点击控件：{label or resource_id or class_name}；页面状态={NAMES.get(self.last_state,self.last_state)}')

    def wait_control(self,label,timeout=30):
        deadline=time.monotonic()+timeout
        with self.budget(timeout):
            while time.monotonic()<deadline:
                self.check_cancel()
                try:
                    root,state,_=self.snapshot()
                except UiReadBudgetExhausted:
                    break
                try:
                    found=self.find(label,tree=root)
                except DuplicateControl as exc:
                    # 控件已经出现、只是重名分不清：快照一张留证再报，比空等到超时有价值。
                    self._record_duplicate(exc)
                    raise
                if found:return found
                time.sleep(.2)
        raise AssertionError('等待控件超时：'+label+'；当前='+NAMES.get(self.last_state,'未知'))

    def _record_duplicate(self, exc):
        record = getattr(self, 'on_duplicate', None)
        if record:
            try:
                record(exc)
            except Exception:
                pass

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
        deadline=time.monotonic()+timeout;last=None;unchanged_since=time.monotonic()
        dump_count=0
        with self.budget(timeout):
            while time.monotonic()<deadline:
                self.check_cancel()
                try:
                    root,state,signature=self.snapshot()
                    dump_count+=1
                except UiReadBudgetExhausted:
                    break
                text='\n'.join(self.label(n) for n in root.iter('node'))
                if condition(text):return text
                key=(state,signature)
                if key!=last:
                    event('step',message='页面状态：'+NAMES[state])
                    last=key;unchanged_since=time.monotonic()
                elif state=='unknown' and time.monotonic()-unchanged_since>=min(12,timeout):
                    raise AssertionError(message+'；页面未识别且持续无变化，已停止重复等待')
                time.sleep(.3)
        raise AssertionError(message+'；最后页面='+NAMES.get(self.last_state,'未知')
                             +'；成功读取界面 %d 次' % dump_count)


class Flow:
    def __init__(self, config, serial, output):
        self.config, self.output = config, Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.suppressed = False
        self.ui = UI(serial, self.check_cancel)
        self.ui.on_duplicate = self._duplicate_evidence
        self.index = 0
        self.item = None
        self.record_requested = False
        self.record_control_completed = False
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
        if self.item is not None:
            self.item.patrol_steps = getattr(self.item, 'patrol_steps', []) + [text]
            if observation:
                self.item.patrol_observations = getattr(self.item, 'patrol_observations', []) + [text]

    def _duplicate_evidence(self, exc):
        """同名控件分不清时留证：截图 + XML，文件名带关键字和候选数。
        这不是失败，是"页面结构变了，需要人看一眼换关键字"。"""
        self.index += 1
        stem = 'flow_%03d_duplicate_%s' % (self.index, re.sub(r'[^A-Za-z0-9_-]+', '_', str(exc.name))[:40])
        try:
            root = self.ui.tree()
        except Exception:
            root = None
        if root is not None:
            (self.output/(stem+'.xml')).write_bytes(ET.tostring(root, encoding='utf-8'))
        try:
            (self.output/(stem+'.png')).write_bytes(self.ui.adb('exec-out', 'screencap', '-p', timeout=20))
        except Exception:
            pass
        boxes = '; '.join(str(b) for b in exc.bounds)
        self.note('控件重名无法收敛，已留证 %s：%s（候选坐标 %s）' % (stem, exc.name, boxes),
                  observation=True)

    def capture(self, title):
        self.index += 1
        stem = f'flow_{self.index:03}_{re.sub(r"[^A-Za-z0-9_-]", "_", title)}'
        root = self.ui.tree()
        # Also block the form when a user has temporarily revealed a password,
        # in which case Android may change password=true to password=false.
        nodes = list(root.iter('node'))
        labels = '\n'.join(UI.label(n) for n in nodes if n.get('class') != 'android.widget.EditText')
        password_form = any(n.get('password') == 'true' for n in nodes) or (
            any(n.get('class') == 'android.widget.EditText' for n in nodes) and '密码' in labels)
        if password_form or '设备日志' in labels:
            self.note('当前页面含密码输入或设备日志，跳过截图和页面取证')
            return
        (self.output/(stem+'.xml')).write_bytes(ET.tostring(root, encoding='utf-8'))
        (self.output/(stem+'.png')).write_bytes(self.ui.adb('exec-out', 'screencap', '-p'))
        self.note('证据已保存：' + stem)

    def pause(self, message, ready=None):
        gate = uuid.uuid4().hex
        resume = self.output/'resume.json'
        event('waiting', gate=gate, message=message)
        deadline = time.monotonic() + self.config['human_timeout']
        next_check = 0.0
        ready_count = 0
        try:
            while time.monotonic() < deadline:
                self.check_cancel()
                if resume.exists():
                    try:
                        if json.loads(resume.read_text(encoding='utf-8')).get('gate') == gate:
                            return
                    except (OSError, ValueError):
                        pass
                # Only explicitly supplied, observable conditions can resume a gate.
                # Two observations prevent a transient screen from triggering progress.
                if ready is not None and time.monotonic() >= next_check:
                    next_check = time.monotonic() + 2.0
                    try:
                        ready_count = ready_count + 1 if ready() else 0
                    except Cancelled:
                        raise
                    except Exception:
                        ready_count = 0
                    if ready_count >= 2:
                        self.note('已连续确认所需状态，自动继续')
                        return
                time.sleep(.2)
            raise AssertionError('等待人工操作超时：' + message)
        finally:
            event('resumed', gate=gate)

    def home_is_visible(self):
        root = self.ui.tree()
        return bool(self.ui.find('任务', tree=root) and self.ui.find('我的', tree=root))

    def stereo_preview_is_visible(self):
        text = self.ui.text()
        return all(label in text for label in ('视频预览', '左目 ·', '右目 ·'))

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
            labels={UI.label(n) for n in root.iter('node')}
            if {'任务','我的'} <= labels:
                self.pause('APP 底部导航被系统栏遮挡。请确认没有正在采集或上传后重新打开 APP，回到主页再继续。')
                continue
            if self.ui.find('欢迎登录', tree=root):
                self.pause('请在手机直接登录测试账号并选择身份；进入 APP 主页后会自动继续。不要在工作台输入密码。', ready=self.home_is_visible)
                continue
            if self.ui.find('返回', tree=root):
                self.ui.tap('返回')
            else:
                self.pause('请将 LivFlow 切为简体中文并返回主页，识别到主页后会自动继续。', ready=self.home_is_visible)
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
        self.target_task()
        self.capture('task_search')
        self.ui.tap('清空搜索')
        self.ui.hide_keyboard()
        text = self.ui.wait(lambda x: '任务中心' in x and re.search(r'共\s*\d+\s*个任务',x))
        field = self.ui.find(class_name='android.widget.EditText')
        assert field and task_list_is_restored(text,field[0].get('text','')), '清空搜索后任务列表或输入框未恢复'
        self.capture('task_search_reset')

    def filters(self):
        self.tasks()
        self.ui.hide_keyboard()
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
        # 卡片文字可能和它的无障碍代理重名。先把范围锁在这张卡片内再点，
        # 不要拿整页文字去找唯一性。
        bounds = list(map(int, re.findall(r'-?\d+', row.get('bounds', ''))))
        assert len(bounds) == 4, '任务卡片没有有效坐标'
        x1, y1, x2, y2 = bounds
        self.ui.adb('shell', 'input', 'tap', str((x1+x2)//2), str((y1+y2)//2))
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
        self.ui.wait(lambda x: '当前版本' in x and version_matches(x,version))
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

    def verify_phone_wifi(self, ssid):
        def current():
            value=self.ui.adb('shell','cmd','wifi','status').decode('utf-8',errors='replace')
            return parse_phone_wifi_status(value)
        actual,security=current()
        if actual is None:
            self.note('无法独立读取手机当前 Wi-Fi 名称，需人工确认与设备网络一致',observation=True)
            return
        if actual != ssid:
            self.pause('请将测试手机切换到本轮指定 Wi-Fi“'+ssid+'”；识别到连接后会自动继续。', ready=lambda: current()[0] == ssid)
            actual,security=current()
            assert actual == ssid, '手机没有连接本轮指定 Wi-Fi，停止判定'
        if security is None:
            self.note('未能独立读取手机 Wi-Fi 安全类型，需人工确认',observation=True)
        else:
            want_open=self.config.get('wifi_security','open')=='open'
            assert (security==0)==want_open, '手机实际 Wi-Fi 安全类型与本轮配置不一致'
            self.note('手机已连接指定 Wi-Fi，且安全类型与本轮配置一致')

    def connect_discovered_device(self, sn):
        """Only tap the visible connect button when it belongs to the sole identified target."""
        deadline=time.monotonic()+self.config['wait_timeout']
        attempts=0
        last_scan=time.monotonic()
        while time.monotonic()<deadline:
            root=self.ui.tree()
            text='\n'.join(UI.label(n) for n in root.iter('node'))
            if 'SN: '+sn in text:
                buttons=[n for n in root.iter('node') if UI.label(n)=='连接设备']
                serials=set(re.findall(r'SN:\s*([A-Za-z0-9_-]+)',text))
                if serials != {sn} or len(buttons)!=1:
                    raise AssertionError('发现多个设备或连接入口不唯一，请人工选择指定 SN 后重试')
                button=self.ui.find('连接设备',tree=root)
                owners=[n for n in root.iter('node') if 'SN: '+sn in UI.label(n)]
                if button and any(button[0] in list(owner.iter('node')) for owner in owners):
                    _,(x1,y1,x2,y2)=button
                    self.ui.adb('shell','input','tap',str((x1+x2)//2),str((y1+y2)//2))
                    return
            if '未发现设备' in text and time.monotonic()-last_scan>=8:
                retry=self.ui.find('重新扫描',tree=root)
                if retry:
                    _,(x1,y1,x2,y2)=retry
                    self.ui.adb('shell','input','tap',str((x1+x2)//2),str((y1+y2)//2))
                    last_scan=time.monotonic()
            attempts+=1
            if attempts%2 == 0:
                self.ui.swipe()
            time.sleep(.3)
        raise AssertionError('未发现或未能安全定位指定 EGO，请检查设备是否可被蓝牙发现')

    def wait_for_provision_page(self):
        # A Bluetooth callback may land directly in preview or on an already-connected card.
        sn=self.config.get('device_sn','')
        for attempt in range(3):
            text=self.ui.wait(lambda x:('设备配网' in x or '连接失败' in x or
                ('视频预览' in x and '左目 ·' in x and '右目 ·' in x) or ('当前设备' in x and 'SN:' in x)),
                timeout=self.config['wait_timeout'],message='未到达配网、已连接或预览页面')
            if '连接失败' in text:
                if '蓝牙服务发现' in text:raise AssertionError('目标设备蓝牙服务发现未完成，停止配网依赖项；请查看证据')
                raise AssertionError('APP 提示设备连接失败，停止本步骤')
            if '视频预览' in text and '左目 ·' in text and '右目 ·' in text:
                return text
            if '当前设备' in text:
                assert sn and 'SN: '+sn in text,'当前设备不是指定SN，停止自动推进'
                if self.ui.find('查看设备') and not self.config.get('require_wifi_provisioning'):
                    self.note('连接回调已到已连接页，改为验证指定设备现有预览；不重复配网')
                    self.ui.tap('查看设备')
                    continue
                # Never disconnect an existing device just to force a fixed workflow.
                self.pause('指定设备已连接，但本项要求重新配网。请手动打开重新配网页后继续；脚本不会自动断开设备。')
                continue
            return text
        raise AssertionError('连接页面连续3次未推进，已停止；请核对配网/预览入口')

    def finish_preview(self,text,ssid,fresh_provision=False):
        self.verify_phone_wifi(ssid)
        if self.config.get('require_wifi_provisioning') and not fresh_provision:
            self.note('本轮只确认已有预览，未观察到重新配网完成，配网专项待确认',observation=True)
        if '左右画面暂未同步' in text:
            self.note('左右画面暂未同步；不能据此判定录制文件同步',observation=True)
        self.capture('connected_preview')
        return text

    def connect(self):
        sn = self.config.get('device_sn', '')
        if not sn:
            raise AssertionError('请在流程配置填写指定 EGO 序列号，不能连接任意附近设备')
        ssid = self.config.get('ssid', '')
        assert ssid, '请配置测试 Wi-Fi 名称'
        self.home()
        self.ui.tap('设备')
        text = self.ui.wait(lambda x: '我的设备' in x)
        if '当前设备' in text:
            assert 'SN: '+sn in text, '当前连接的不是指定 EGO，停止'
            for _ in range(7):
                if self.ui.find('查看设备') and not self.config.get('require_wifi_provisioning'):
                    self.ui.tap('查看设备')
                    self.ui.wait(lambda x:'左目 ·' in x and '右目 ·' in x,timeout=self.config['wait_timeout'])
                    self.verify_phone_wifi(ssid)
                    if self.item is not None and getattr(self.item,'originalname',None)=='test_flow_target_connection':
                        self.note('设备已连接，本轮未重新执行配网；只验证现有预览',observation=True)
                    self.capture('connected_preview')
                    return
                if self.ui.find('继续配网') or self.ui.find('重新配网'):
                    break
                self.ui.swipe()
            if self.ui.find('继续配网'):
                self.ui.tap('继续配网')
            elif self.ui.find('重新配网'):
                self.ui.tap('重新配网')
            elif self.config.get('require_wifi_provisioning'):
                self.pause('当前设备已连接。请在手机为指定 EGO 打开重新配网或继续配网页，停留在设备配网页后继续。')
                selected=self.wait_for_provision_page()
                if 'SN: '+sn not in selected:
                    self.note('配网页未显示设备序列号，重新配网目标由人工确认',observation=True)
            else:
                raise AssertionError('没有找到当前设备的配网或查看入口')
        else:
            if self.ui.find('蓝牙设备'):
                self.ui.tap('蓝牙设备')
            existing=self.ui.text()
            if 'SN: '+sn not in existing:
                self.ui.tap('开始扫描' if self.ui.find('开始扫描') else '重新扫描')
            self.connect_discovered_device(sn)
        reached=self.wait_for_provision_page()
        if '视频预览' in reached and '左目 ·' in reached and '右目 ·' in reached:
            return self.finish_preview(reached,ssid)
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
        check = self.ui.find('免密码网络')
        assert check and check[0].get('checkable') == 'true', '未找到网络安全选项，停止配网'
        want_open = self.config.get('wifi_security','open') == 'open'
        is_open = check[0].get('checked') == 'true'
        if is_open != want_open:
            self.ui.tap('免密码网络')
            check = self.ui.find('免密码网络')
            assert check and (check[0].get('checked') == 'true') == want_open, '网络安全选项未切换成功'
        if not want_open:
            self.pause('请在手机填写所选加密 Wi-Fi 的密码，停留在配网页，不要发送密码到聊天或工作台；填写后点击继续。')
        # The operator may already have tapped Connect while entering a password.
        current=self.ui.text()
        if '视频预览' in current and '左目 ·' in current and '右目 ·' in current:
            return self.finish_preview(current,ssid)
        if not self.ui.find('连接网络'):
            reached=self.wait_for_provision_page()
            if '视频预览' in reached and '左目 ·' in reached and '右目 ·' in reached:
                return self.finish_preview(reached,ssid)
            if not self.ui.find('连接网络'):
                raise AssertionError('当前非可提交配网页，连接网络按钮不存在；停止重复点击并保存证据')
        self.ui.tap('连接网络')
        self.note('已请求指定 EGO 配网；若手机不是同一网络，请按提示切换')
        try:
            self.ui.wait(lambda x: '视频预览' in x and '设备配网' not in x, timeout=25)
        except AssertionError:
            self.pause('请确认手机与 EGO 已连接同一个测试 Wi-Fi；APP 进入双目预览后会自动继续。',
                       ready=self.stereo_preview_is_visible)
        text = self.ui.wait(lambda x: '左目 ·' in x and '右目 ·' in x, timeout=self.config['wait_timeout'])
        self.verify_phone_wifi(ssid)
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
        self.record_control_completed = False
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
            start = time.monotonic()
            self.ui.wait_control('结束采集', timeout=min(20,self.config['wait_timeout']))
            self.capture('record_started')
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
        self.record_control_completed = True
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
        if os.environ.get('APP_PATROL_ALLOW_RECORD') == '1' and not self.record_control_completed:
            import pytest
            pytest.skip('本轮短采集未完成，没有已确认的新录制；跳过依赖上传，不要求插卡或选择历史目录')
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
