"""Workstation bridge: new results only; previous patrol results are never replayed."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from uuid import uuid4

BASE = Path(__file__).resolve().parent
RUNS = BASE / 'out/app-patrol'
PREFIX = '@@APP_PATROL@@'
MODES = {
    'selfcheck': ('框架自检', 'tests/test_config.py'),
    'smoke': ('LivFlow 基础自动巡检', 'tests/test_livflow_adb.py'),
    'preview': ('LivFlow 预览只读检查', 'tests/test_livflow_open_wifi.py'),
    'flow_tasks': ('任务搜索、筛选与统计', 'tests/test_livflow_workflows.py'),
    'flow_settings': ('语言、版本与日志导出', 'tests/test_livflow_workflows.py'),
    'flow_connect': ('指定 EGO 连接及配网', 'tests/test_livflow_workflows.py'),
    'flow_parameters': ('IMU 参数修改、回读与恢复', 'tests/test_livflow_workflows.py'),
    'flow_record': ('短采集及后台恢复', 'tests/test_livflow_workflows.py'),
    'flow_upload': ('测试文件备份、写入、上传与防重复', 'tests/test_livflow_workflows.py'),
    'flow_all': ('扩展流程巡检', 'tests/test_livflow_workflows.py'),
    'flow_full': ('全量自动巡检', 'tests/test_livflow_workflows.py'),
    'files': ('本地 MCAP 文件及元数据校验', 'tests/test_livflow_files.py'),
}


def emit(kind, **data):
    print(PREFIX + json.dumps(dict(type=kind, **data), ensure_ascii=False), flush=True)


def new_session(mode, serial='', framework=''):
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8]
    folder = RUNS / stamp
    folder.mkdir(parents=True)
    return dict(schema=1, id=stamp, folder=str(folder), created=datetime.now().isoformat(timespec='seconds'),
                mode=mode, serial=serial, framework=framework, state='未开始', cases=[],
                counts={}, report=str(folder / 'summary.html'))


def write_session(session):
    folder = Path(session['folder'])
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    for row in session['cases']:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    session['counts'] = counts
    target = folder / 'session.json'
    temp = folder / 'session.json.tmp'
    temp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(target)
    from app_patrol_report import render_report
    mode = session.get('report_title') or ('完整巡检清单（人工协同）' if session['mode'] == 'manual' else MODES[session['mode']][0])
    (folder / 'summary.html').write_text(render_report(session, mode), encoding='utf-8')
    with (folder / 'cases.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['编号', '模块', '检查项', '结果', '记录'])
        for row in session['cases']:
            # Prevent spreadsheet formula execution when importing a manual note.
            writer.writerow([("'" + str(row.get(k, ''))) if str(row.get(k, '')).startswith(('=', '+', '-', '@'))
                             else row.get(k, '') for k in ('id', 'module', 'title', 'status', 'detail')])


def manual_session(framework):
    session = new_session('manual', framework=framework)
    catalogue = json.loads((BASE / 'app_patrol_catalog.json').read_text(encoding='utf-8'))
    session['cases'] = [dict(id=x['id'], module=x['module'], title=x['title'],
                             status='未执行', detail='', scope=x.get('scope', '')) for x in catalogue]
    session['state'] = '人工巡检进行中'
    write_session(session)
    return session


@contextmanager
def execution_lock():
    RUNS.mkdir(parents=True, exist_ok=True)
    f = (RUNS / 'execution.lock').open('a+b')
    if os.fstat(f.fileno()).st_size == 0:
        f.write(b'0')
        f.flush()
    f.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError('另一个工作台正在执行 APP 自动检查，请等待结束')
    try:
        yield
    finally:
        f.close()


def stop_child(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run(mode, framework, serial, cancel_file, flow_config=None, allow_parameters=False, allow_record=False, allow_upload=False):
    root = Path(framework).resolve()
    python = root / '.venv/Scripts/python.exe'
    test = root / MODES[mode][1]
    if not python.is_file() or not test.is_file():
        raise RuntimeError('测试框架路径或独立 Python 环境不完整，请选择 app-test-framework 文件夹')
    if mode not in ('selfcheck','files') and not serial:
        raise RuntimeError('必须选择明确的 Android 设备序列号')
    with execution_lock():
        session = new_session(mode, serial, str(root))
        session['operations'] = dict(parameters=allow_parameters, record=allow_record, upload=allow_upload)
        if flow_config:
            settings = json.loads(Path(flow_config).read_text(encoding='utf-8'))
            session['target'] = {key: settings.get(key, '') for key in ('task_id','device_sn','ssid')}
            if mode == 'flow_full' and allow_record and allow_upload and settings.get('recording_dir'):
                raise RuntimeError('全量包含新录制时，请先在流程配置清空“手机 TF 卡测试目录”；上传阶段再选择本轮新目录，避免上传旧记录。')
        session['state'] = '运行中'
        write_session(session)
        emit('session', session=session)
        folder = Path(session['folder'])
        args = [str(python), '-m', 'pytest', str(test), '-q', '-p', 'app_patrol_pytest_plugin',
                '--artifact-dir', str(folder), '--html', str(folder / 'report.html'),
                '--self-contained-html', '--junitxml', str(folder / 'junit.xml')]
        if mode == 'flow_full':
            args.insert(3, str(root / 'tests/test_config.py'))
        if mode not in ('selfcheck','files'):
            args.append('--device')
        if mode.startswith('flow_'):
            args.append('-s')
            if mode not in ('flow_all','flow_full'):
                args += ['-m',mode]
        env = os.environ.copy()
        env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONUNBUFFERED='1',
                   ANDROID_UDID=serial, PYTHONPATH=str(BASE))
        if flow_config:
            env['APP_PATROL_FLOW_CONFIG'] = str(Path(flow_config).resolve())
        env['APP_PATROL_CANCEL_FILE'] = str(Path(cancel_file).resolve()) if cancel_file else ''
        for name, value in [('PARAMETERS',allow_parameters),('RECORD',allow_record),('UPLOAD',allow_upload)]:
            env['APP_PATROL_ALLOW_'+name] = '1' if value else '0'
        # Passwords and account identifiers are intentionally not accepted by this bridge.
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        output = queue.Queue()

        def reader():
            try:
                for line in iter(proc.stdout.readline, b''):
                    output.put(line.decode('utf-8', errors='replace').rstrip())
            finally:
                proc.stdout.close()
                output.put(None)

        threading.Thread(target=reader, daemon=True).start()
        cancelled = False
        cancel_started = None
        try:
            while True:
                if cancel_file and Path(cancel_file).exists() and not cancelled:
                    cancelled = True
                    cancel_started = time.monotonic()
                    if not mode.startswith('flow_'):
                        stop_child(proc)
                    else:
                        emit('log',message='已请求停止；正在等待流程结束采集或恢复参数。')
                if cancelled and mode.startswith('flow_') and proc.poll() is None and time.monotonic()-cancel_started>90:
                    emit('log',message='流程未能及时结束，强制停止。请人工确认 EGO 已结束采集、参数已恢复。')
                    stop_child(proc)
                try:
                    line = output.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if PREFIX in line:
                    try:
                        event = json.loads(line.split(PREFIX, 1)[1])
                    except ValueError:
                        emit('log', message=line)
                        continue
                    if event['type'] == 'collected':
                        session['cases'] = event['cases']
                    elif event['type'] == 'case':
                        for row in session['cases']:
                            if row['id'] == event['case']['id']:
                                row.update(event['case'])
                                break
                    elif event['type'] == 'waiting':
                        session['state'] = '等待人工操作'
                        session['waiting'] = dict(gate=event['gate'],message=event['message'])
                    elif event['type'] == 'resumed':
                        session.pop('waiting',None)
                        session['state'] = '运行中'
                    elif event['type'] == 'step':
                        emit('log',message=event['message'])
                        continue
                    write_session(session)
                    emit('update', session=session)
                else:
                    emit('log', message=line)
            code = proc.wait(timeout=10)
        finally:
            stop_child(proc)
        for row in session['cases']:
            if row['status'] == '未执行':
                row.update(status='未测', detail='执行已停止，或检查未能开始')
        session['state'] = '已停止' if cancelled else '已完成' if code == 0 else '检查失败'
        session.pop('waiting',None)
        if code == 0 and not cancelled and any(x['status'] != '通过' for x in session['cases']):
            session['state'] = '已结束（含未测或待确认）'
        if not session['cases'] and not cancelled:
            session['state'] = '未能执行'
        session['exit_code'] = code
        write_session(session)
        emit('finished', session=session)
        return 130 if cancelled else code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--framework', required=True)
    parser.add_argument('--mode', choices=MODES, required=True)
    parser.add_argument('--serial', default='')
    parser.add_argument('--cancel-file')
    parser.add_argument('--flow-config')
    parser.add_argument('--allow-parameters',action='store_true')
    parser.add_argument('--allow-record',action='store_true')
    parser.add_argument('--allow-upload',action='store_true')
    args = parser.parse_args()
    try:
        return run(args.mode,args.framework,args.serial,args.cancel_file,args.flow_config,
                   args.allow_parameters,args.allow_record,args.allow_upload)
    except Exception as exc:
        emit('error', message=str(exc))
        return 2


if __name__ == '__main__':
    sys.exit(main())
