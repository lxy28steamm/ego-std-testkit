"""UMI 上机三步串联：串口绑定自愈 -> VIVE 基站标定 -> 测试采集。

设计前提（2026-10-27 从设备 D-o-0.0.28 源码核实，不是猜的）：
  1. 左右手 UMI 串口的 USB 序列号**完全相同**（LSUMI2P2705A00044），
     by-id 只会生成一条链接，指向最后枚举的那个，无法区分左右手。
     所以不能用 by-id 绑定，只能按设备自己识别的 detected_hand 配 by-path。
  2. by-path 里的 xhci-hcd.N 是控制器实例编号，重启/换口后会变（.3 -> .14），
     这就是"绑定莫名失效、左右手选到同一个口"的根因。
     因此绑定不做永久生效，改成每次上机前重算一次（自愈）。
  3. 基站标定必须有物理动作：人拿追踪器站在两座基站都能看到的位置缓慢移动。
     脚本只能负责"启动 -> 推进度 -> 判定结果 -> 落盘"，不能代替人动。
     backend/Device/umi/vive_manager.py 硬超时 200 秒，这里给 220 秒余量。
  4. 没有标定文件时 ViveSource.open() 直接抛"请先完成 VIVE 基站标定"，
     采集根本不会启动。所以标定是采集的硬前置。

用法：
    python umi_bringup.py --host 192.168.50.164                    # 只看不改（dry-run）
    python umi_bringup.py --host 192.168.50.164 --apply            # 真写绑定并标定
    python umi_bringup.py --host 192.168.50.164 --stage bind       # 只做绑定的 dry-run
    python umi_bringup.py --host 192.168.50.164 --apply --stage bind --yes
    python umi_bringup.py --host 192.168.50.164 --apply --stage all --record-seconds 20

默认 dry-run：只读接口 + 打印将要做什么。不加 --apply 不会写设备任何配置。
"""
import argparse
import json
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from umi_functional import Client

STAGES = ('bind', 'calib', 'record')
BUSY = ('preparing', 'recording', 'stopping', 'finalizing')


class Stop(RuntimeError):
    """当前阶段判定为阻塞性问题，后续阶段不再执行。"""


def plan_bindings(devices, current):
    """纯函数：按设备上报的左右手识别算出目标绑定，不碰任何 IO。

    返回 (target, problem, changed)：
      target  需要写入的 UmiBindings 字典；problem 非空时为 None
      problem 无法自动决定的原因（人手判断，脚本不猜）
      changed 与当前绑定是否不同
    """
    usable = [d for d in devices if not d.get('detection_error')]
    left = [d for d in usable if d.get('detected_hand') == 'left']
    right = [d for d in usable if d.get('detected_hand') == 'right']
    # 一左一右各恰好一个才敢自动写；多只或缺失都必须人来判断。
    if len(left) != 1 or len(right) != 1:
        return None, f'串口识别不是一左一右各一个（左 {len(left)}，右 {len(right)}）', False
    target = {
        'schema_version': 1, 'mode': 'dual', 'single_hand': 'auto', 'single_port': None,
        'left_port': left[0]['path'], 'right_port': right[0]['path'],
    }
    changed = not (current.get('mode') == 'dual'
                   and current.get('left_port') == target['left_port']
                   and current.get('right_port') == target['right_port'])
    return target, None, changed


class Runner:
    def __init__(self, host, port=18000, apply=False, assume_yes=False, log=print):
        self.api = Client(host, port)
        self.host = host
        self.port = int(port)
        self.apply = apply
        self.assume_yes = assume_yes
        self.log = log
        self.evidence = {
            'created': datetime.now().isoformat(timespec='seconds'),
            'host': host, 'apply': apply, 'stages': {},
        }

    def record(self, stage, **kv):
        self.evidence['stages'].setdefault(stage, {}).update(kv)

    def call(self, path, body=None, method=None, timeout=30):
        """设备拒绝时把 detail.code/message 翻成人话，不要裸的 HTTP 409。

        设备在别的操作进行中会用 409 + code 拒绝，例如 UMI_VIVE_CALIBRATING
        （标定中不允许读串口列表）。这类不是故障，是"现在不能做"。
        """
        try:
            return self.api.call(path, body, method, timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode('utf-8', 'replace')
            try:
                detail = json.loads(raw).get('detail')
            except ValueError:
                detail = raw[:300]
            code = detail.get('code', '') if isinstance(detail, dict) else ''
            msg = detail.get('message', raw[:300]) if isinstance(detail, dict) else str(detail)
            hint = ''
            if code == 'UMI_VIVE_CALIBRATING':
                hint = '（设备上已有标定在跑，等它结束或先取消）'
            raise Stop(f'{path} 被拒绝 {exc.code}{" [" + code + "]" if code else ""}：{msg}{hint}')

    def save(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'bringup.json'
        path.write_text(json.dumps(self.evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    def confirm(self, text):
        """写操作前的最后一道人工确认；--yes 可跳过，便于回归脚本调用。"""
        if not self.apply:
            return False
        if self.assume_yes:
            return True
        self.log('  ' + text)
        try:
            return input('  确认执行？输入 y 继续：').strip().lower() == 'y'
        except EOFError:
            self.log('  非交互环境，未确认，按 dry-run 处理。请加 --yes 表示知情同意。')
            return False

    # ---------------------------------------------------------------- 阶段一
    def stage_bind(self):
        """串口绑定自愈：按设备识别的左右手重算 by-path，不一致才写。"""
        self.log('[1/3] 串口绑定')
        devices = self.call('/api/v2/umi/serial-devices')
        current = self.call('/api/v2/umi/bindings')
        self.record('bind', detected=devices, before=current)

        for d in devices:
            self.log(f"  端口 {d.get('label') or d.get('path')}")
            self.log(f"    路径={d.get('path')}")
            self.log(f"    识别={d.get('detected_hand') or '未识别'}"
                     f"{'  选中' if d.get('selected') else ''}"
                     f"{'  错误:' + d['detection_error'] if d.get('detection_error') else ''}")

        target, problem, changed = plan_bindings(devices, current)
        if problem:
            self.record('bind', status='blocked', reason=problem)
            raise Stop(problem + '。请检查两根 USB 是否都插好，或到设备端手动指定。')

        self.record('bind', target=target)
        self.log(f"  应绑左手 = {target['left_port']}")
        self.log(f"  应绑右手 = {target['right_port']}")

        if not changed:
            self.log('  当前绑定已一致，无需改写。')
            self.record('bind', status='unchanged')
        else:
            self.log('  当前绑定与实测不一致：')
            self.log(f"    现绑左手 = {current.get('left_port')}")
            self.log(f"    现绑右手 = {current.get('right_port')}")
            if not self.apply:
                self.record('bind', status='dry-run', would_write=target)
                self.log('  [dry-run] 未写入。加 --apply 才会真正改绑定。')
                return 'dry-run'
            if not self.confirm('即将改写设备串口绑定（PUT /api/v2/umi/bindings）。'):
                self.record('bind', status='skipped')
                self.log('  已跳过写入。')
                return 'skipped'
            self.call('/api/v2/umi/bindings', target, method='PUT')
            after = self.call('/api/v2/umi/bindings')
            self.record('bind', after=after)
            if (after.get('left_port') != target['left_port']
                    or after.get('right_port') != target['right_port']):
                raise Stop(f'写入后回读不一致：left={after.get("left_port")} '
                           f'right={after.get("right_port")}')
            self.log('  绑定已写入并回读一致。')
            self.record('bind', status='written')

        # 绑定正确与否，设备自己的判断在 calibration/{hand}/info 里，直接问它。
        infos = {h: self.call(f'/api/v2/umi/calibration/{h}/info') for h in ('left', 'right')}
        self.record('bind', hand_info=infos)
        bad = {h: v.get('detail', {}).get('message', '')
               for h, v in infos.items() if isinstance(v.get('detail'), dict)}
        if bad:
            for hand, msg in bad.items():
                self.log(f'  {hand} 标定信息报错：{msg}')
            raise Stop('串口绑定后设备仍报标定信息错误：' + '；'.join(bad.values()))
        self.log('  左右手标定信息读取正常。')
        return 'ok'

    # ---------------------------------------------------------------- 阶段二
    def stage_calib(self, timeout=220, force=False):
        """VIVE 基站标定：校验前置 -> 启动 -> 轮询进度 -> 判定并落盘。

        真正干活的是设备端的原生标定进程，这里不制造任何"假标定"。
        """
        self.log('[2/3] VIVE 基站标定')
        bindings = self.call('/api/v2/umi/vive/bindings')
        devices = self.call('/api/v2/umi/vive/devices')
        status = self.call('/api/v2/umi/vive/calibration')
        self.record('calib', bindings=bindings, devices=devices, before=status)

        left_s, right_s = bindings.get('left_serial', ''), bindings.get('right_serial', '')
        self.log(f"  追踪器绑定：left={left_s or '空'}  right={right_s or '空'}  启用={bindings.get('enabled')}")
        for d in devices:
            self.log(f"  在线追踪器 {d.get('serial')}  USB口={d.get('usb_path')}  "
                     f"识别={d.get('detected_hand') or '未识别'}")

        if not bindings.get('enabled'):
            raise Stop('VIVE 追踪器绑定处于关闭状态，标定无意义。')
        if not (left_s or right_s):
            raise Stop('先保存 VIVE 追踪器绑定（PUT /api/v2/umi/vive/bindings），再标定。')
        if left_s and left_s == right_s:
            raise Stop('左右手绑定了同一只追踪器。')
        online = {d.get('serial') for d in devices}
        missing = [s for s in (left_s, right_s) if s and s not in online]
        if missing:
            raise Stop(f'绑定的追踪器不在线：{missing}。检查 USB 和供电。')

        if status.get('state') == 'running':
            raise Stop('设备上已有标定在跑，不重复启动。')
        if status.get('valid_stations') == 2 and status.get('calibration_available') and not force:
            self.log(f"  已有有效标定（valid_stations=2，文件在位），跳过。"
                     ' 要重标请加 --force-calib。')
            self.record('calib', status='already-valid')
            return 'ok'

        self.log(f"  当前状态 state={status.get('state')} "
                 f"valid_stations={status.get('valid_stations')} "
                 f"标定文件在位={status.get('calibration_available')}")

        if not self.apply:
            self.record('calib', status='dry-run')
            self.log('  [dry-run] 未启动标定。加 --apply 才会真正启动。')
            return 'dry-run'
        if not self.confirm('即将启动基站标定，期间 UMI 采集会被暂停占用硬件。\n'
                            '  启动后请立刻拿起追踪器，站在两座基站都能看到的位置缓慢移动。'):
            self.record('calib', status='skipped')
            self.log('  已跳过标定。')
            return 'skipped'

        started = self.call('/api/v2/umi/vive/calibration/start', {}, method='POST', timeout=20)
        self.record('calib', started=started)
        self.log(f"  已启动：state={started.get('state')}  {started.get('message', '')}")
        self.log('  >>> 现在请拿追踪器在两座基站视野内缓慢移动 <<<')

        deadline = time.monotonic() + timeout
        last = None
        while True:
            st = self.call('/api/v2/umi/vive/calibration', timeout=15)
            line = (st.get('state'), st.get('valid_stations'))
            if line != last:  # 只在变化时打印，避免刷屏
                self.log(f"  [{time.strftime('%H:%M:%S')}] state={st.get('state')} "
                         f"已标定基站={st.get('valid_stations')}/2  {st.get('message', '')}")
                last = line
            if st.get('state') in ('complete', 'failed', 'cancelled'):
                break
            if time.monotonic() > deadline:
                # 设备端 200 秒会自己停，这里超时只报告，不重复发取消指令。
                self.record('calib', timeout_status=st)
                raise Stop(f'等待标定超过 {timeout} 秒仍未结束（设备端硬超时 200 秒）。'
                           f'最后状态 state={st.get("state")} valid_stations={st.get("valid_stations")}')
            time.sleep(1)

        final = self.call('/api/v2/umi/vive/calibration', timeout=15)
        self.record('calib', final=final, status=final.get('state'))
        if final.get('log_file'):
            self.log(f"  标定日志（设备端）：{final['log_file']}")
        if final.get('state') != 'complete':
            raise Stop(f"标定未完成：state={final.get('state')}，{final.get('message', '')}")
        if not final.get('calibration_available'):
            raise Stop('设备报 complete 但标定文件不在位，不可用于采集。')
        self.log(f"  标定成功，valid_stations={final.get('valid_stations')}，文件已在位。")
        return 'ok'

    # ---------------------------------------------------------------- 阶段三
    def stage_record(self, folder, seconds=15, upload=False):
        """复用既有 umi_functional 的采集流程，不另写一套。"""
        self.log('[3/3] 测试采集')
        health = self.call('/api/v2/health')
        session = self.call('/api/v2/session')
        if session.get('state') in BUSY:
            raise Stop(f'设备正在采集（state={session.get("state")}），不抢占。')
        if (health.get('selection') or {}).get('enabled_devices') != ['umi']:
            raise Stop('设备当前没有选中 UMI，验收不自动切机型。')

        # 采集只看"标定文件在不在"，不看标定质量。最近一次标定失败但旧文件还在时
        # 照样能录，录出来是旧标定的位姿 —— 这种要提示，不能默默放过。
        vive = self.call('/api/v2/umi/vive/calibration')
        self.record('record', vive_status=vive)
        if vive.get('state') == 'failed':
            self.log(f"  [注意] 最近一次基站标定是 failed（{vive.get('message', '')}），"
                     f"标定文件在位={vive.get('calibration_available')}。"
                     '本轮录到的位姿基于旧标定，结论需谨慎。')
        elif not vive.get('calibration_available'):
            raise Stop('没有基站标定文件，VIVE 采集起不来（设备端会直接报"请先完成 VIVE 基站标定"）。')

        if not self.apply:
            self.record('record', status='dry-run', seconds=seconds)
            self.log(f'  [dry-run] 未启动采集。加 --apply 才会录 {seconds} 秒。')
            return 'dry-run'

        import umi_functional
        config = {'host': self.host, 'api_port': self.port,
                  'record_seconds': seconds, 'test_upload': upload, 'capture_preview': True}
        evidence = umi_functional.run(config, folder, log=lambda t: self.log('    ' + t))
        self.record('record', result=evidence.get('steps'), error=evidence.get('error'))
        failed = [s for s in evidence.get('steps', []) if s.get('status') == '失败']
        if evidence.get('error') or failed:
            raise Stop('采集阶段未通过：' + (evidence.get('error')
                                            or '; '.join(s['id'] for s in failed)))
        self.log('  采集完成，MCAP 校验通过。')
        return 'ok'


def selftest():
    """离线自测：只验证绑定判定逻辑，不连设备、不发任何请求。"""
    by_path = '/dev/serial/by-path/platform-xhci-hcd.14.auto-usb-0:1.4:1.0'
    by_id = '/dev/serial/by-id/usb-Espressif_ESP32-P4_CDC_Serial_LSUMI2P2705A00044-if00'
    old = '/dev/serial/by-path/platform-xhci-hcd.3.auto-usb-0:1.4:1.0'
    good = [{'path': by_path, 'detected_hand': 'left', 'detection_error': ''},
            {'path': by_id, 'detected_hand': 'right', 'detection_error': ''}]
    cur_same = {'mode': 'dual', 'left_port': by_path, 'right_port': by_id}
    cur_old = {'mode': 'dual', 'left_port': old, 'right_port': by_id}
    cases = [
        ('一左一右但当前绑的是旧控制器 -> 要改写', good, cur_old, None, True),
        ('一左一右且已一致 -> 不改写', good, cur_same, None, False),
        ('单口模式 -> 要改成 dual', good, {'mode': 'single', 'left_port': None,
                                          'right_port': None, 'single_hand': 'left',
                                          'single_port': '/dev/ttyACM0'}, None, True),
        ('两个都识别成右手 -> 交给人', [dict(good[0], detected_hand='right'), good[1]], cur_same,
         '左 0，右 2', None),
        ('只插了一只手 -> 交给人', [good[0]], cur_same, '左 1，右 0', None),
        ('坏口被排除后仍一左一右 -> 正常', good + [{'path': '/dev/ttyACM9',
                                                'detected_hand': None,
                                                'detection_error': '打开失败'}], cur_same, None, False),
    ]
    failed = 0
    for name, devices, current, want_problem, want_changed in cases:
        target, problem, changed = plan_bindings(devices, current)
        if want_problem is not None:
            ok = target is None and problem is not None and want_problem in problem
        else:
            ok = target is not None and problem is None and changed is want_changed
        failed += not ok
        print(('PASS' if ok else 'FAIL'), '|', name, '|',
              problem or f'changed={changed}')
    print(f'\n{len(cases) - failed}/{len(cases)} 通过')
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='UMI 上机三步串联（默认 dry-run）')
    ap.add_argument('--host', default='', help='设备 IP')
    ap.add_argument('--port', type=int, default=18000)
    ap.add_argument('--stage', default='all',
                    help='all 或组合：bind / calib / record，逗号分隔')
    ap.add_argument('--apply', action='store_true', help='真正写设备；不加只读')
    ap.add_argument('--yes', action='store_true', help='跳过每步人工确认（脚本化用）')
    ap.add_argument('--force-calib', action='store_true', help='已有有效标定也重标')
    ap.add_argument('--calib-timeout', type=int, default=220)
    ap.add_argument('--record-seconds', type=int, default=15)
    ap.add_argument('--upload', action='store_true', help='采集后尝试上传')
    ap.add_argument('--out', default='', help='证据目录，默认 out/bringup-<时间戳>')
    ap.add_argument('--selftest', action='store_true', help='跑离线自测，不连设备')
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.host:
        ap.error('需要 --host（或 --selftest 只跑离线自测）')

    stages = STAGES if args.stage == 'all' else tuple(
        s.strip() for s in args.stage.split(',') if s.strip())
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        ap.error(f'未知阶段：{unknown}，可用：{STAGES}')

    folder = Path(args.out) if args.out else Path('out') / (
        'bringup-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:4])
    runner = Runner(args.host, args.port, apply=args.apply, assume_yes=args.yes)
    runner.log(f"目标 {args.host}:{args.port}  模式={'APPLY（会写设备）' if args.apply else 'dry-run（只读）'}")
    runner.log(f"阶段 {' -> '.join(stages)}")
    runner.log('')

    results, stopped = {}, ''
    for name in stages:
        try:
            if name == 'bind':
                results[name] = runner.stage_bind()
            elif name == 'calib':
                results[name] = runner.stage_calib(args.calib_timeout, args.force_calib)
            else:
                results[name] = runner.stage_record(folder, args.record_seconds, args.upload)
        except Stop as exc:
            results[name] = 'blocked'
            stopped = f'{name}: {exc}'
            runner.log(f'  [中止] {exc}')
            break
        except Exception as exc:
            results[name] = 'error'
            stopped = f'{name}: {type(exc).__name__}: {exc}'
            runner.log(f'  [异常] {type(exc).__name__}: {exc}')
            break
        runner.log('')

    runner.record('summary', results=results, stopped=stopped,
                  skipped=list(STAGES[len(stages):]) if not stopped else [])
    path = runner.save(folder)
    runner.log(f"结果：{results}")
    runner.log(f"证据：{path}")
    return 0 if not stopped else 1


if __name__ == '__main__':
    sys.exit(main())
