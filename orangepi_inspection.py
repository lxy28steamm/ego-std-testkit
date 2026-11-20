"""Orange Pi SSH inspection. No passwords in CLI arguments, files, or reports."""
import argparse
import base64
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from uuid import uuid4

import paramiko
from app_patrol_engine import BASE, write_session


class Cancelled(Exception):
    pass


class UnknownHostKey(Exception):
    def __init__(self,fingerprint):
        self.fingerprint=fingerprint
        super().__init__('首次连接，请核对 SSH 指纹：'+fingerprint)


def fingerprint(key):
    return 'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, trusted=''):
        self.trusted=trusted

    def missing_host_key(self,client,hostname,key):
        value=fingerprint(key)
        if value!=self.trusted:
            raise UnknownHostKey(value)


def settings(host,username,port=22,services='ssh.service',temperature=80,disk=85,memory=90,cpu=95,trusted=''):
    host=host.strip()
    username=username.strip()
    if not host or any(x in host for x in '\r\n /\\') or len(host)>253:
        raise ValueError('请填写正确的设备 IP 或主机名')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}',username):
        raise ValueError('请填写有效的 SSH 用户名')
    units=[x.strip() for x in re.split(r'[,，\s]+',services) if x.strip()]
    if len(units)>8 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}',x) for x in units):
        raise ValueError('最多填写8个服务名，只支持字母、数字及 . _ @ : -')
    if not 1<=int(port)<=65535:
        raise ValueError('SSH 端口范围为1～65535')
    thresholds=dict(temperature=float(temperature),disk=float(disk),memory=float(memory),cpu=float(cpu))
    if not all(0<v<=100 for v in thresholds.values()):
        raise ValueError('告警阈值必须大于0且不超过100')
    return dict(host=host,username=username,port=int(port),services=units,thresholds=thresholds,trusted=trusted)


def base_cases(config):
    if config.get('acceptance'):
        from orangepi_acceptance import cases
        return cases(config['acceptance'])
    names=[('ssh','连接','SSH连接'),('identity','系统','板卡型号'),('system','系统','系统与内核'),
           ('uptime','系统','开机运行时长'),('cpu','资源','CPU即时使用率'),('memory','资源','可用内存'),
           ('disk','存储','根分区剩余空间'),('readonly','存储','根分区挂载状态'),
           ('temperature','资源','芯片温度'),('storage','存储','存储设备识别'),
           ('network','网络','网卡与IP地址'),('route','网络','默认路由'),('clock','系统','时间偏差与NTP同步'),
           ('failed','服务','失败的系统服务'),('kernel','日志','本次启动的内核错误摘要'),
           ('usb','接口','USB设备枚举'),('camera','专项待测','摄像头实际采集'),
           ('serial','专项待测','串口与GPIO功能'),('accelerator','专项待测','GPU/NPU实际计算')]
    names += [('service-'+unit,'服务','服务：'+unit) for unit in config['services']]
    return [dict(id='OPI-'+key,module=group,title=title,status='未执行',detail='') for key,group,title in names]


def evaluate(data,config,received=None):
    if config.get('acceptance'):
        from orangepi_acceptance import evaluate as accept
        return accept(data,config['acceptance'],config.get('acceptance_rules'))
    rows=base_cases(config)
    by_id={x['id']:x for x in rows}
    def result(key,status,detail):
        by_id['OPI-'+key].update(status=status,detail=detail)
    def percent(key,value,threshold,description):
        if not isinstance(value,(int,float)) or not 0<=value<=100:
            result(key,'未测','没有读取到有效数据')
        else:
            result(key,'待确认' if value>=threshold else '通过',f'{description} {value:.1f}%；本轮告警阈值 {threshold:g}%')
    result('ssh','通过','SSH认证和只读探测成功')
    model=data.get('model')
    result('identity','通过' if model and re.search(r'orange\s*pi|\bopi\b',model,re.I) else '待确认',model or '设备没有提供板卡型号，不能确认具体型号')
    info=data.get('system',{})
    result('system','通过' if info.get('PRETTY_NAME') and data.get('kernel') else '未测',
           ' · '.join(str(x) for x in (info.get('PRETTY_NAME'),data.get('kernel'),data.get('architecture')) if x) or '未读取到系统信息')
    try:
        uptime=float(data['uptime'].split()[0])
        assert uptime>=0
        result('uptime','通过',f'已运行 {uptime/3600:.2f} 小时；仅为开机时长，不代表稳定性测试通过')
    except (KeyError,ValueError,TypeError,AttributeError,AssertionError):
        result('uptime','未测','未读取到有效的开机时长')
    limits=config['thresholds']
    percent('cpu',data.get('cpu_busy'),limits['cpu'],'CPU约1秒采样的使用率')
    mem=data.get('memory',{})
    total,available=mem.get('MemTotal'),mem.get('MemAvailable')
    percent('memory',100*(total-available)/total if total and isinstance(available,(int,float)) else None,
            limits['memory'],'内存使用率（按MemAvailable）')
    fs=data.get('root_fs') or {}
    size,free=fs.get('bytes'),fs.get('free')
    percent('disk',100*(size-free)/size if size and isinstance(free,(int,float)) else None,limits['disk'],'根分区空间使用率')
    if 'readonly' in fs:
        result('readonly','待确认' if fs['readonly'] else '通过','根分区以只读方式挂载，需确认是否为设计配置' if fs['readonly'] else '根分区挂载标志为可读写；没有实际写文件')
    else:
        result('readonly','未测','未取得挂载标志')
    temperatures=data.get('temperatures') or []
    if temperatures:
        maximum=max(x['celsius'] for x in temperatures)
        result('temperature','待确认' if maximum>=limits['temperature'] else '通过',
               '；'.join(f"{x['name']} {x['celsius']:.1f}℃" for x in temperatures)+f"；本轮告警阈值 {limits['temperature']:g}℃")
    else:
        result('temperature','未测','未取得有效温度，可能与驱动或权限有关')
    storage=data.get('storage') or []
    result('storage','通过' if storage else '未测','；'.join(f"{x['name']} {x['bytes']/1024**3:.1f}GiB {x.get('model','')}" for x in storage) or '未枚举到存储设备')
    interfaces=data.get('interfaces')
    active=[]
    for nic in interfaces or []:
        if nic.get('ifname')=='lo':
            continue
        addresses=[x.get('local','') for x in nic.get('addr_info',[]) if x.get('scope')=='global']
        if addresses and ('UP' in nic.get('flags',[]) or nic.get('operstate')=='UP'):
            active.append(nic.get('ifname','')+': '+', '.join(addresses))
    result('network','通过' if active else '未测','；'.join(active) or '未取得已启用网卡的全局IP；不能据此判定网络故障')
    routes=data.get('routes')
    result('route','未测' if routes is None else '通过' if routes else '待确认',
           '；'.join(str(x.get('gateway',''))+' via '+str(x.get('dev','')) for x in routes or []) or '未取得默认路由；仅局域网用途时可能符合设计')
    utc=data.get('utc')
    ntp=data.get('ntp',{})
    if isinstance(utc,(int,float)):
        offset=abs((time.time() if received is None else received)-utc)
        synced=ntp.get('ok') and ntp.get('text','').strip()=='yes'
        result('clock','通过' if offset<=30 and synced else '待确认',
               f'相对本机接收时刻偏差约 {offset:.1f} 秒（含传输耗时）；NTP '+('已同步' if synced else '未同步或无法确认'))
    else:
        result('clock','未测','未取得设备时间')
    failed=data.get('failed_services',{})
    units=failed.get('units',[])
    result('failed','未测' if not failed.get('available') else '待确认' if units else '通过',
           '未能读取systemd服务状态' if not failed.get('available') else '失败服务：'+', '.join(units) if units else 'systemd当前没有失败的service；不代表业务功能已验证')
    errors=data.get('kernel_errors',{})
    result('kernel','未测' if not errors.get('available') else '待确认' if errors.get('sample_count') else '通过',
           f"本次启动最近最多50条内核错误摘要：{errors.get('sample_count',0)}行；仅保留数量及类别，未保存日志正文" if errors.get('available') else '没有内核日志读取权限或日志工具不可用')
    usb=data.get('usb')
    result('usb','通过' if isinstance(usb,list) else '未测',
           '读取USB枚举清单：'+('；'.join(x['id']+' '+x.get('name','') for x in usb) or '没有设备')+'；不代表各端口与外设功能已验证'
           if isinstance(usb,list) else '没有读取到USB清单')
    result('camera','未测',f"识别到 {len(data.get('video_nodes',[]))} 个视频节点；需要相机型号、连接方式和采集验收条件后另测")
    result('serial','未测',f"USB串口节点 {len(data.get('serial_nodes',[]))} 个，GPIO芯片节点 {len(data.get('gpio_nodes',[]))} 个；未打开串口或操作引脚")
    result('accelerator','未测',f"识别到 {len(data.get('accelerator_nodes',[]))} 个GPU/NPU节点；未运行计算或压力测试")
    for unit in config['services']:
        item=data.get('services',{}).get(unit,{})
        available=item.get('available') and item.get('LoadState')=='loaded'
        status='未测' if not available else '通过' if item.get('ActiveState')=='active' else '失败'
        result('service-'+unit,status,'无法读取或服务不存在' if not available else f"状态 {item.get('ActiveState')} / {item.get('SubState')}；Result={item.get('Result','未知')}。仅检查进程服务状态。")
    return rows


def fetch(client,config,cancel):
    source=(BASE/'orangepi_probe.py').read_text(encoding='utf-8')
    payload=('SERVICE_UNITS='+repr(config['services'])+'\nACCEPTANCE_DEVICE='+repr(config.get('acceptance',''))+'\nAPI_PORT='+repr(config.get('api_port',18000))+'\n'+source).encode('utf-8')
    channel=client.get_transport().open_session(timeout=10)
    channel.settimeout(10)
    try:
        channel.exec_command('python3 -')
        channel.sendall(payload)
        channel.shutdown_write()
        stdout=bytearray()
        stderr=bytearray()
        deadline=time.monotonic()+90
        while True:
            if cancel.is_set():
                raise Cancelled()
            if time.monotonic()>deadline:
                raise TimeoutError('设备探测超时')
            if channel.recv_ready():
                stdout.extend(channel.recv(32768))
            if channel.recv_stderr_ready():
                stderr.extend(channel.recv_stderr(8192))
            if len(stdout)>512000 or len(stderr)>64000:
                raise RuntimeError('设备返回数据超出限制')
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(.03)
        if channel.recv_exit_status()!=0:
            raise RuntimeError('设备只读探测未成功，请确认Python3可用；未保存远端错误正文')
        data=json.loads(stdout.decode('utf-8'))
        if not isinstance(data,dict) or data.get('schema')!=1:
            raise RuntimeError('设备返回格式不符合预期')
        return data
    finally:
        channel.close()


def inspect(config,password='',key_file='',cancel=None,on_update=lambda session:None):
    if config.get('framework_v2'):
        from orangepi_modular import inspect as modular
        return modular(config,password,key_file,cancel,on_update)
    cancel=cancel or threading.Event()
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]
    folder=BASE/'out/orangepi'/stamp
    folder.mkdir(parents=True)
    session=dict(schema=1,id=stamp,folder=str(folder),created=datetime.now().isoformat(timespec='seconds'),
                 mode='orangepi',report_title=(config.get('acceptance','').upper()+' 整机功能验收报告' if config.get('acceptance') else '香橙派巡检报告'),acceptance_device=config.get('acceptance',''),serial=config['host'],state='运行中',
                 cases=base_cases(config),report=str(folder/'summary.html'))
    client=paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(PinnedHostKey(config.get('trusted','')))
    trusted_error=None
    try:
        if cancel.is_set():
            raise Cancelled()
        write_session(session)
        on_update(session)
        if config.get('test_record'):
            if config.get('acceptance')!='umi': raise ValueError('实测目前仅支持 UMI')
            from umi_functional import Client
            api=Client(config['host'],config.get('api_port',18000))
            health=api.call('/api/v2/health')
            # Report only required identity; no login credentials are retained.
            business_health={k:health.get(k) for k in ('selection','devices','workers','storage','session','recorder','host','overall_state')}
            business_health['cloud']={k:(health.get('cloud') or {}).get(k) for k in ('device_sn','state','error_code','last_heartbeat_time_ns')}
            def optional_api(path):
                try:return api.call(path)
                except Exception:return None
            data={'schema':1,'business':{'health':business_health,'version':(api.call('/openapi.json').get('info') or {}).get('version',''),
                                      'sensor':api.call('/api/v2/umi/sensor-status'),
                                      'storage':optional_api('/api/v2/storage'),'bindings':optional_api('/api/v2/umi/bindings'),
                                      'calibration':{hand:optional_api('/api/v2/umi/calibration/'+hand+'/info') for hand in ('left','right')},
                                      'network':optional_api('/api/v2/network'),'wifi':optional_api('/api/v2/network/wifi')}}
        else:
            client.connect(config['host'],port=config['port'],username=config['username'],password=password or None,
                           key_filename=key_file or None,look_for_keys=not password,allow_agent=not password,
                           timeout=10,banner_timeout=10,auth_timeout=10)
            data=fetch(client,config,cancel)
        if cancel.is_set():
            raise Cancelled()
        received=time.time()
        session['device_info']={key:data.get(key,'') for key in ('model','kernel','architecture')}
        session['device_info']['system']=data.get('system',{}).get('PRETTY_NAME','')
        session['acceptance_device']=config.get('acceptance','')
        session['report_title']=config.get('acceptance','').upper()+' 整机功能验收报告' if config.get('acceptance') else '香橙派巡检报告'
        business=data.get('business',{})
        session['device_info']['sn']=((business.get('health') or {}).get('cloud') or {}).get('device_sn','')
        session['device_info']['version']=business.get('version','')
        session['diagnostics_only']=True
        session['thresholds']=config['thresholds']
        session['ssh_port']=config['port']
        session['acceptance_rules']=config.get('acceptance_rules',{})
        session['cases']=evaluate(data,config,received)
        if config.get('test_record'):
            from umi_functional import run
            session['state']='运行中';write_session(session);on_update(session)
            functional=run(config,folder,cancel)
            session['functional']=dict(session_id=functional.get('session_id'),task=functional.get('test_task'),
                                       evidence='functional.json',error=functional.get('error'))
            lookup={r['id']:r for r in session['cases']}
            for step in functional['steps']:
                if step['id'] in lookup: lookup[step['id']].update(status=step['status'],detail=step['detail'])
            if functional.get('session'):
                current=functional['session']
                lookup['ACC-session'].update(status='通过' if current.get('result')=='complete' else '待确认',detail='本轮会话 '+str(current.get('session_id'))+'；result='+str(current.get('result')))
                # Additional quality checks bind to this run's final session, never old history.
                from orangepi_acceptance_extra import evaluate as extra
                current_business=dict(data['business'])
                current_business['health']=dict(current_business['health'])
                current_business['health']['session']=current
                for item in extra(current_business,'umi',config.get('acceptance_rules')):
                    if item['key'] not in ('login','login_reject'):
                        lookup['ACC-'+item['key']].update(status=item['status'],detail=item['detail'])


        if config.get('test_login') and config.get('acceptance'):
            from umi_functional import Client
            from orangepi_login_checks import run as login_checks
            lookup={r['id']:r for r in session['cases']}
            for item in login_checks(Client(config['host'],config.get('api_port',18000)),config.get('login_account',''),config.get('login_password','')):
                lookup['ACC-'+item['key']].update(status=item['status'],detail=item['detail'])
        session['state']='已结束（含未测或待确认）' if any(x['status']!='通过' for x in session['cases']) else '已完成'
        if cancel.is_set(): session['state']='已停止'
        if any(x['status']=='失败' for x in session['cases']):
            session['state']='检查失败'
        # Keep only whitelisted probe fields, never authentication details.
        (folder/'metrics.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    except UnknownHostKey as exc:
        trusted_error=exc
        session['state']='等待核对SSH指纹'
        session['cases'][0].update(status='未测',detail=str(exc))
    except Cancelled:
        session['state']='已停止'
    except Exception as exc:
        session['state']='检查失败'
        detail=('SSH认证失败，请检查用户名和密码或密钥' if isinstance(exc,paramiko.AuthenticationException) else
                'SSH主机密钥与已知记录不一致，请核对设备' if isinstance(exc,paramiko.BadHostKeyException) else
                '设备连接或探测失败：'+type(exc).__name__+'；请检查地址、SSH服务和Python3环境')
        session['cases'][0].update(status='失败',detail=detail)
    finally:
        password=''
        config.pop('login_password',None)
        config.pop('login_account',None)
        client.close()
        for row in session['cases']:
            if row['status']=='未执行':
                row.update(status='未测',detail='连接或探测未完成，本项没有执行')
        write_session(session)
        on_update(session)
    if trusted_error:
        trusted_error.session=session
        raise trusted_error
    return session


def main(argv=None):
    p=argparse.ArgumentParser(description='香橙派巡检；默认只读，使用 --record 自动执行短采集回归')
    p.add_argument('--device',choices=('auto','ego','umi','dex','linked','system'),default='auto')
    p.add_argument('--scope',choices=('all','host','peripheral'),default='all')
    p.add_argument('--api-port',type=int,default=18000)
    p.add_argument('--host',default=os.environ.get('ORANGEPI_HOST'),help='目标 IP 或主机名，也可设置 ORANGEPI_HOST')
    p.add_argument('--record',action='store_true',help='自动执行短采集、预览、停止和本轮 MCAP 校验')
    p.add_argument('--record-seconds',type=int,metavar='10-60',help='短采集时长，默认 15 秒，需同时使用 --record')
    p.add_argument('--user',default='orangepi')
    p.add_argument('--port',type=int,default=22)
    p.add_argument('--services',default='ssh.service')
    p.add_argument('--key-file',default='')
    p.add_argument('--trust-fingerprint',default='')
    p.add_argument('--password',action='store_true',help='Prompt for password; never pass it as an argument')
    a=p.parse_args(argv)
    if not a.host:
        p.error('请用 --host 或 ORANGEPI_HOST 指定目标设备')
    if a.record and (a.device=='system' or a.scope=='host'):
        p.error('--record 需要外设巡检模式')
    if a.record_seconds is not None and (not a.record or not 10<=a.record_seconds<=60):
        p.error('--record-seconds 需要启用 --record，且时长为 10～60 秒')
    try:
        cfg=settings(a.host,a.user,a.port,a.services,trusted=a.trust_fingerprint)
    except ValueError as exc:
        p.error(str(exc))
    if not 1<=a.api_port<=65535:
        p.error('API端口范围为1～65535')
    if a.device!='system':
        cfg.update(acceptance=a.device,api_port=a.api_port,framework_v2=True,test_scope=a.scope,
                   test_record=a.record,capture_preview=a.record,record_seconds=a.record_seconds or 15)
    try:
        password=getpass.getpass('SSH password: ') if a.password and a.device=='system' else ''
        result=inspect(cfg,password,a.key_file)
        summary={k:result[k] for k in ('state','counts','report','error','device_profile','functional') if k in result}
        print(json.dumps(summary,ensure_ascii=False))
        return 1 if result['state'] in ('检查失败','检查未完成','已停止') else 0
    except UnknownHostKey as exc:
        print(str(exc))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
