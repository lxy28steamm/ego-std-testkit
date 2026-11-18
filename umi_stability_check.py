"""UMI 采集稳定性检查：API 实时状态 + SSH 日志历史扫描，输出可核对结论。只读，不启动/停止采集。

背景（2026-10-26 实测）：香橙派 UMI 采集当天 9 起 worker_exception 失败——
8 起为四组视频队列（left/right x fisheye/stereo）积压爆满（video queue is full，
分布在开录后 38-61 秒窗口 7 起 + 一次长录制 54 分钟后 1 起），1 起为 18:14 的
JPEG 帧损坏（invalid UMI JPEG header，根因 USB 2.0 带宽饱和）。另有 10:55
USB SSD 掉线一次（dmesg 实锤重新枚举）、VIVE 进程退出 52 条。
失败后系统一律自动停止采集并删除 .mcap.partial，数据不保留。
本脚本把当天排查方法固化为可复跑检查。

用法：
    python umi_stability_check.py --host 192.168.50.164
    # 带日志扫描（密码走环境变量，不进命令行参数）：
    set OP_SSH_PW=********
    python umi_stability_check.py --host 192.168.50.164 --ssh-user orangepi --ssh-pw-env OP_SSH_PW

无 SSH 时只做 API 实时检查；有 SSH 时追加当天日志历史扫描。
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import urllib.request
import urllib.error

OUT_DIR = Path(__file__).resolve().parent / 'out'
LOG_PATH = '/home/orangepi/raspberry-ego-v2/logs/livstudio-edge-{date}.log'

# 判定阈值（可按设备实际情况调整；含义见 docs/2026-10-26-UMI视频队列溢出排查.md）
QUEUE_WARN_RATIO = 0.5      # 队列水位/容量超过该比例提醒（8192 容量的经验值）
DISK_LATENCY_WARN_MS = 300  # recorder.disk_latency_ms 超过该值提醒（实测健康录制也有 ~700ms，仅作记录）
RESTART_FAIL = 1            # worker restart_count >= 该值视为本轮启动后发生过异常重启


def api_get(host, port, path, timeout=15):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f'http://{host}:{int(port)}{path}')
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def ssh_run(host, user, password, command, timeout=90):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=user, password=password, timeout=15)
        _, stdout, _ = client.exec_command(command, timeout=timeout)
        return stdout.read().decode('utf-8', 'replace')
    finally:
        client.close()


def scan_log_text(text):
    """从 livstudio-edge 日志文本提取当天 worker 异常事件（含后果）。

    已知失败/异常模式（2026-10-26 实测统计）：
      - video queue is full     四组视频队列积压爆满，删数据
      - invalid UMI JPEG header JPEG 帧数据损坏，删数据
      - VIVE 采集失败            VIVE 进程退出（USB/标定），当天 52 条记录，最高频
      - video_keyframe_timeout   等 H.264 关键帧超时
      - 左右 UMI 串口不能绑定到同一个设备  串口绑定冲突
      - 数据存储路径不可用/外接盘可能已断开  USB SSD 掉线（10:55 实锤 dmesg 重新枚举）

    删数据事件的权威锚点是「⚠️ [采集] 异常采集已停止并删除 session=...」行
    （自带 session/code/reason），不要用同秒匹配关联——异常与删除常差 1-2 秒。
    """
    lines = text.splitlines()

    def classify(reason):
        if 'queue is full' in reason:
            return 'queue_full'
        if 'JPEG header' in reason:
            return 'jpeg_header'
        if 'VIVE' in reason:
            return 'vive_fail'
        if 'keyframe' in reason:
            return 'keyframe_timeout'
        if '串口' in reason:
            return 'serial_conflict'
        if '存储路径' in reason or '外接盘' in reason:
            return 'ssd_offline'
        return 'other'

    # 1) 删数据事件：以「异常采集已停止并删除」行为锚点
    #    先收集所有 worker 异常行，用于给删除事件归因（删除行自带的 reason 是通用的
    #    "umi Worker 已重启"，不含模式信息，必须向前找最近的 worker_exception）
    worker_exc = []
    for line in lines:
        if '[设备]' not in line or 'code=' not in line:
            continue
        if 'worker_exception' not in line and 'umi_source_error' not in line:
            continue
        t = line.split(' ', 2)[1] if len(line.split(' ', 2)) > 1 else ''
        r = line.split('reason=', 1)[-1].strip('"') if 'reason=' in line else ''
        worker_exc.append((t, r))

    def attribute(del_time):
        """删除事件向前找 120 秒内最近的 worker 异常行来定模式。"""
        for t, r in reversed(worker_exc):
            if t <= del_time:
                try:
                    from datetime import datetime as _dt
                    delta = (_dt.strptime(del_time, '%H:%M:%S')
                             - _dt.strptime(t, '%H:%M:%S')).total_seconds()
                except ValueError:
                    return 'other'
                if delta <= 120:
                    return classify(r)
                return 'other'
        return 'other'

    deleted_events = []
    for line in lines:
        if '异常采集已停止并删除' not in line:
            continue
        time_str = line.split(' ', 2)[1] if len(line.split(' ', 2)) > 1 else ''
        reason = line.split('reason=', 1)[-1].strip('"') if 'reason=' in line else ''
        session = line.split('session=', 1)[-1].split(' ', 1)[0].strip('"') if 'session=' in line else ''
        code = line.split('code=', 1)[-1].split(' ', 1)[0].strip('"') if 'code=' in line else ''
        # 同一 session 的删除行会重复打 2 次，去重
        if deleted_events and deleted_events[-1]['session'] == session:
            continue
        # partial 路径从相邻的「已删除异常采集数据」行取
        path = ''
        for l2 in lines:
            if f' {time_str}' in l2[:20] and '已删除异常采集数据' in l2 and 'path=' in l2:
                path = l2.split('path=', 1)[-1].strip('"')
                break
        deleted_events.append({
            'time': time_str, 'session': session, 'code': code,
            'reason': reason, 'kind': attribute(time_str),
            'deleted_path': path,
        })

    # 2) worker 异常模式统计（不去重同一时刻的连续重复行，按秒+reason 去重）
    events = []
    for line in lines:
        if '[设备]' not in line or 'code=' not in line:
            continue
        if 'worker_exception' not in line and 'umi_source_error' not in line:
            continue
        reason = line.split('reason=', 1)[-1].strip('"') if 'reason=' in line else ''
        time_str = line.split(' ', 2)[1] if len(line.split(' ', 2)) > 1 else ''
        if events and events[-1]['time'] == time_str and events[-1]['reason'] == reason:
            continue
        events.append({'time': time_str, 'reason': reason, 'kind': classify(reason)})

    # 3) SSD 掉线类事件（[健康] 行，没有 [设备] code= 前缀）
    for line in lines:
        if ('数据存储路径不可用' in line or '外接盘' in line) and '已停止' not in line:
            time_str = line.split(' ', 2)[1] if len(line.split(' ', 2)) > 1 else ''
            if any(e['time'] == time_str for e in events):
                continue
            events.append({'time': time_str, 'reason': '数据存储路径不可用（外接盘可能已断开）',
                           'kind': 'ssd_offline'})

    return events, deleted_events


def main():
    parser = argparse.ArgumentParser(description='UMI 采集稳定性检查（只读）')
    parser.add_argument('--host', default='')
    parser.add_argument('--api-port', type=int, default=18000)
    parser.add_argument('--ssh-user', default='orangepi')
    parser.add_argument('--ssh-pw-env', default='OP_SSH_PW',
                        help='保存 SSH 密码的环境变量名；不提供则跳过日志扫描')
    parser.add_argument('--log-path', default='',
                        help='覆盖默认日志路径；默认按日期推导')
    parser.add_argument('--date', default=datetime.date.today().strftime('%Y-%m-%d'))
    args = parser.parse_args()

    host = args.host.strip()
    if not host:
        ip_file = Path(__file__).resolve().parent / 'device_ip.txt'
        if ip_file.exists():
            host = ip_file.read_text(encoding='utf-8').strip().splitlines()[-1].strip()
    if not host:
        raise SystemExit('未指定 --host，且 device_ip.txt 不可用')

    report = {
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'host': host,
        'checks': [],
        'verdict': '未执行',
    }

    def put(cid, status, detail, evidence=None):
        report['checks'].append({'id': cid, 'status': status, 'detail': detail,
                                 'evidence': evidence or {}})

    # 1) 实时状态：健康接口
    try:
        health = api_get(host, args.api_port, '/api/v2/health')
        selection = health.get('selection') or {}
        devices = selection.get('devices') or {}
        umi = devices.get('umi') or {}
        workers = health.get('workers') or {}
        worker = workers.get('umi') or {}
        report['health'] = {
            'umi_online': umi.get('logical_online'),
            'umi_hardware_connected': umi.get('hardware_connected'),
            'umi_pipeline': umi.get('pipeline_state'),
            'worker_state': worker.get('state'),
            'worker_restart_count': worker.get('restart_count'),
            'worker_cpu_percent': worker.get('cpu_percent'),
            'worker_error': worker.get('error_message'),
            'recorder_active': health.get('recorder_active'),
        }
        online = bool(umi.get('logical_online') and umi.get('hardware_connected')
                      and worker.get('state') == 'running')
        put('UMI-ONLINE', '通过' if online else '失败',
            f"UMI 在线={umi.get('logical_online')}；硬件连接={umi.get('hardware_connected')}；"
            f"Worker 状态={worker.get('state')}；管线={umi.get('pipeline_state')}",
            {'restart_count': worker.get('restart_count'), 'error': worker.get('error_message')})
        restarts = worker.get('restart_count') or 0
        put('UMI-WORKER-RESTART', '通过' if restarts < RESTART_FAIL else '失败',
            f"本次设备启动后 worker 重启次数={restarts}（阈值 {RESTART_FAIL}）；"
            f">=阈值说明本轮发生过队列溢出类异常重启，需结合日志时间线定位",
            {'restart_count': restarts})
    except Exception as exc:
        put('UMI-ONLINE', '失败', f'读取健康接口失败：{type(exc).__name__}: {exc}')
        report['verdict'] = '失败'
        report['fail_reason'] = 'API 不可达'
        finish(report)
        return

    # 2) 实时状态：当前会话与队列
    try:
        session = api_get(host, args.api_port, '/api/v2/session')
        recorder = session.get('recorder') or {}
        streams = recorder.get('streams') or {}
        state = session.get('state')
        depth = recorder.get('queue_depth')
        capacity = recorder.get('queue_capacity')
        hwm = recorder.get('queue_high_watermark')
        latency = recorder.get('disk_latency_ms')
        report['session'] = {
            'state': state,
            'session_id': session.get('session_id'),
            'task_name': session.get('task_name'),
            'queue_depth': depth, 'queue_capacity': capacity,
            'queue_high_watermark': hwm, 'disk_latency_ms': latency,
            'drop_count': recorder.get('drop_count'),
            'writer_rejected_count': recorder.get('writer_rejected_count'),
            'free_bytes': recorder.get('free_bytes'),
            'topic_count': recorder.get('topic_count'),
            'missing_total': sum((s or {}).get('missing_count', 0) for s in streams.values()),
            'streams': {k: {'rate_hz': (v or {}).get('actual_rate_hz'),
                            'missing': (v or {}).get('missing_count')}
                        for k, v in streams.items()},
        }
        if state == 'recording' and isinstance(depth, int) and capacity:
            ratio = depth / capacity
            status = '通过' if ratio < QUEUE_WARN_RATIO else '待确认'
            put('UMI-QUEUE', status,
                f"当前队列水位 {depth}/{capacity}（{ratio:.0%}，阈值 {QUEUE_WARN_RATIO:.0%}）；"
                f"历史最高 {hwm}；丢帧 {recorder.get('drop_count')}；写拒绝 {recorder.get('writer_rejected_count')}",
                {'ratio': round(ratio, 4), 'high_watermark': hwm})
        else:
            detail = f'当前会话状态={state}，无实时队列数据；队列检查只在录制中有效'
            if state in ('invalid', 'fault'):
                detail += '；状态为 invalid/fault 说明本轮录制已被系统判定异常并处理，' \
                          '请查看下方 UMI-LOG-HISTORY 中的失败事件与原因'
            put('UMI-QUEUE', '待确认' if state in ('invalid', 'fault') else '未测', detail)
        missing = report['session']['missing_total']
        put('UMI-STREAM-MISSING', '通过' if missing == 0 else '待确认',
            f"当前会话各流 missing_count 合计={missing}（0 为正常；>0 说明有帧序号空洞）")
        if isinstance(latency, (int, float)):
            put('UMI-DISK-LATENCY', '记录',
                f"recorder.disk_latency_ms={latency}ms（>{DISK_LATENCY_WARN_MS}ms 仅记录不判定；"
                f"该字段为写盘延迟估计，与 USB SSD 性能相关，队列溢出时该值通常同步升高）")
    except Exception as exc:
        put('UMI-QUEUE', '失败', f'读取会话接口失败：{type(exc).__name__}: {exc}')

    # 3) 传感器与串口绑定
    try:
        sensor = api_get(host, args.api_port, '/api/v2/umi/sensor-status')
        bindings = api_get(host, args.api_port, '/api/v2/umi/bindings')
        sides = sensor.get('sides') or []
        invalid = [s.get('side') for s in sides if s.get('required') and not s.get('valid')]
        report['sensor'] = {'state': sensor.get('state'), 'sides': sides, 'bindings': bindings}
        ok = sensor.get('state') == 'ready' and not invalid
        put('UMI-SENSOR', '通过' if ok else '待确认',
            f"传感器状态={sensor.get('state')}；必需侧未就绪={invalid or '无'}；"
            f"绑定模式={bindings.get('mode')}；左口={bindings.get('left_port')}；右口={bindings.get('right_port')}")
    except Exception as exc:
        put('UMI-SENSOR', '未测', f'读取传感器状态失败：{type(exc).__name__}: {exc}')

    # 4) 日志历史扫描（需要 SSH 密码）
    password = os.environ.get(args.ssh_pw_env, '') if args.ssh_pw_env else ''
    if password:
        try:
            log_path = args.log_path or LOG_PATH.format(date=args.date)
            text = ssh_run(host, args.ssh_user, password, f'cat {log_path} 2>/dev/null')
            events, deleted = scan_log_text(text)
            kinds = {}
            for e in events:
                kinds[e['kind']] = kinds.get(e['kind'], 0) + 1
            report['log_history'] = {'path': log_path, 'events': events,
                                     'deleted_events': deleted,
                                     'kinds': kinds,
                                     'queue_full_lines': text.count('queue is full')}
            put('UMI-LOG-HISTORY',
                '通过' if not deleted else '失败',
                f"{log_path} 中扫描到 worker 异常 {len(events)} 起（按模式：{kinds}），"
                f"其中自动停止并删除数据 {len(deleted)} 起："
                + '；'.join(f"{e['time']} {e['kind']} 删 {e.get('deleted_path', '')}" for e in deleted),
                {'events': events, 'deleted_events': deleted, 'kinds': kinds})
        except Exception as exc:
            put('UMI-LOG-HISTORY', '未测', f'SSH 日志扫描失败：{type(exc).__name__}: {exc}')
    else:
        put('UMI-LOG-HISTORY', '未测',
            f'未提供 SSH 密码环境变量 {args.ssh_pw_env}，跳过当天日志历史扫描；'
            f'该项用于发现当天历次队列溢出与删数据事件')

    # 5) 总判定
    statuses = [c['status'] for c in report['checks']]
    if '失败' in statuses:
        report['verdict'] = '失败'
    elif '待确认' in statuses:
        report['verdict'] = '待确认'
    else:
        report['verdict'] = '通过'
    finish(report)


def finish(report):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    path = OUT_DIR / f'umi-stability-{stamp}.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f" verdict: {report['verdict']}")
    for check in report['checks']:
        print(f"  [{check['status']}] {check['id']}: {check['detail']}")
    print(f" report: {path}")


if __name__ == '__main__':
    main()
