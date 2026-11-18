"""Host checks and EGO/UMI/DEX adapters, with evidence isolated per run."""
import json,threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from app_patrol_engine import BASE,write_session
from orangepi_profiles import resolve,paths,required_hands,stream_contract

HOST_KEYS={'api','identity','storage','battery','battery_level','battery_power','charging','network','ethernet','wifi','wifi_scan','cloud','storage_selection','storage_capacity','heartbeat','battery_limits','battery_long','login','login_reject','display'}
DEVICE_NAMES=[('quality','本轮采集质量'),('selection','设备与实际槽位'),('serial','连接与手侧/相机识别'),('sensor','传感器就绪'),('calibration','本设备标定检查'),('preview','本设备画面预览'),('session','目标设备录制结果'),('stream_set','本设备数据流契约'),('stream_rates','本设备频率标准'),('writer','写入拒绝/取消/丢帧'),('disk_latency','写盘延迟'),('health_consistency','健康状态一致性'),('stability','本设备 Worker 重启'),('record','本轮采集及MCAP结构完整性'),('data_layout','本设备文件数据结构与标定载体'),('upload','本轮文件上传'),('button','实体按键与反馈')]


def catalogue(profile,scope='all'):
    from orangepi_acceptance import cases
    result=[]
    if scope in ('all','host'):
        result.append(dict(id='ACC-ego_plus_wifi', title='EGO Plus Wi-Fi 状态', module='主机常规 / 接口版本', scope='host', status='未执行', detail=''))
        result.append(dict(id='ACC-api_capabilities', title='新增接口能力', module='主机常规 / 接口版本', scope='host', status='未执行', detail=''))
        for r in cases('umi'):
            if r['id'].removeprefix('ACC-') in HOST_KEYS:
                r.update(scope='host',module='主机常规 / '+r['module']);result.append(r)
    if scope in ('all','peripheral'):
        for key,title in DEVICE_NAMES:
            if key=='serial':title={'ego':'USB 相机与 IMU 识别','umi':'左右手串口及手侧识别','dex':'左右手串口与近手相机绑定'}[profile['family']]
            if key=='calibration':title={'ego':'相机 / IMU 标定状态与载体','umi':'每手标定与相机 / VIVE 配套','dex':'每手 URDF 标定及关节映射'}[profile['family']]
            result.append(dict(id='ACC-'+key,title=title,module=profile['family'].upper()+'专项',scope='peripheral',status='未执行',detail=''))
    return result


def collect(api,profile,scope,cancel):
    errors={}
    def get(path):
        if cancel.is_set():raise RuntimeError('已取消')
        try:return api.call(path,timeout=5)
        except Exception as exc:errors[path]=type(exc).__name__;return None
    h=get('/api/v2/health') or {};spec=get('/openapi.json') or {}
    b={'api_capabilities': {path: sorted(method for method in (spec.get('paths') or {}).get(path, {}) if method in ('get', 'put')) for path in ('/api/v2/ego-plus/wifi', '/api/v2/umi/cameras/preview.jpg')} if spec else None, 'health':{k:h.get(k) for k in ('overall_state','selection','devices','workers','storage','host','session')},'version':(spec.get('info') or {}).get('version')}
    b['health']['cloud']={k:(h.get('cloud') or {}).get(k) for k in ('device_sn','state','error_code','last_heartbeat_time_ns')}
    if scope in ('all','host'):
        for k,path in {'storage':'/api/v2/storage','network':'/api/v2/network','wifi':'/api/v2/network/wifi'}.items():b[k]=get(path)
    if scope in ('all','host') and 'get' in (b.get('api_capabilities') or {}).get('/api/v2/ego-plus/wifi', []):
        wifi=get('/api/v2/ego-plus/wifi')
        b['ego_plus_wifi']={k:wifi.get(k) for k in ('readable','writable','password_set','connected','config_mode')} if isinstance(wifi,dict) else None
    if scope in ('all','peripheral'):
        for k,path in paths(profile).items():b[k]=get(path)
        b['calibration']={k:b.get('cal_'+k) for k in ('left','right')}
        if profile['family']=='dex':
            b['realtime_sides']={hand:get('/api/v2/dex/realtime?hand='+hand) for hand in required_hands(b.get('bindings') or {})}
    return b,errors


def evaluate(b,profile,scope='all',rules=None):
    from orangepi_acceptance import evaluate as legacy
    family,slot=profile['family'],profile['slot'];rows=catalogue(profile,scope);lookup={r['id']:r for r in rows}
    def put(k,status,detail):
        if 'ACC-'+k in lookup:lookup['ACC-'+k].update(status=status,detail=detail)
    # Shared evaluators only supply common checks. UMI-specific endpoints are never fetched for EGO/DEX.
    for r in legacy({'business':b},slot,rules):
        key=r['id'].removeprefix('ACC-')
        if key in HOST_KEYS:put(key,r['status'],r['detail'])
    capabilities=b.get('api_capabilities')
    if scope in ('all','host'):
        wifi=b.get('ego_plus_wifi')
        put('ego_plus_wifi', '待确认' if wifi else '未测', '只读状态='+str(wifi)+'；连接状态与配置生效需结合实际网络核验，未修改配置。')
        put('api_capabilities', '待确认' if capabilities is not None else '未测', f"后端版本={b.get('version') or '未知'}；OpenAPI 声明={capabilities if capabilities is not None else '未获取'}；仅检查接口声明，Wi-Fi 配置及 UMI 预览实际功能未测试。")
    if scope=='host':return rows
    h=b.get('health') or {};dev=(h.get('devices') or {}).get(slot) or {};worker=(h.get('workers') or {}).get(slot) or {}
    connected=dev.get('hardware_connected',worker.get('hardware_connected'));ready=dev.get('pipeline_state') or worker.get('pipeline_state')
    put('selection','通过' if connected is True and ready=='ready' else '待确认',f'目标={family.upper()}；槽位={slot}；连接={connected}；pipeline={ready}。'+profile['guide'])
    bind=b.get('bindings') or {};hands=required_hands(bind)
    if family=='ego':
        cams=b.get('camera_devices')
        put('serial','通过' if connected is True and (bool(dev.get('hardware_serial')) or isinstance(cams,list) and bool(cams)) else '未测',f'USB相机枚举={len(cams) if isinstance(cams,list) else "未获取"}；相机芯片SN={dev.get("hardware_serial") or "未获取"}；EGO不使用左右手串口绑定。枚举接口为空时依据目标硬件连接及相机SN确认识别，不将其当作串口故障。')
        imu=b.get('imu_settings') or {}
        put('sensor','通过' if ready=='ready' and connected is True and imu.get('imu_rate') else '待确认',f'EGO pipeline={ready}；IMU配置={imu}；配置可读不代表已采到有效IMU数据')
        cal=dev.get('calibration_state') or worker.get('calibration_state')
        put('calibration','待确认' if cal=='valid' else '未测' if not cal or cal=='not_checked' else '失败',f'EGO相机/IMU calibration_state={cal}；仅设备报告，需MCAP标定附件/元数据及物理精度复核；不套用URDF或手侧标定')
    elif family=='dex':
        serial=b.get('serial_devices');ports={x.get('path') for x in serial or []}
        required=[bind.get(k+'_port') if bind.get('mode')=='dual' else bind.get('single_port') for k in hands]
        matched=bool(required) and all(x and x in ports for x in required)
        put('serial','通过' if matched else '未测',f'DEX mode={bind.get("mode")}；手侧={hands}；已绑定串口均被枚举={matched}；相机绑定按每手独立检查')
        per_hand=b.get('realtime_sides') or {}
        fresh_hands=[hand for hand in hands if (per_hand.get(hand) or {}).get('connected') and (per_hand.get(hand) or {}).get('data_fresh') and (per_hand.get(hand) or {}).get('joints')]
        put('sensor','通过' if hands and len(fresh_hands)==len(hands) else '未测',f'DEX配置手侧={hands}；收到新鲜关节遥测的手侧={fresh_hands}；每手独立检查，触觉/IMU字段完整性仍需协议标准')
        urdf=b.get('urdf')
        put('calibration','待确认' if urdf else '未测','DEX URDF标定信息'+('已读取；需按左右手核对关节映射和零位，不能以UMI blob校验代替' if urdf else '未获取'))
    else:
        # Reuse the established UMI evaluator only for UMI.
        for r in legacy({'business':b},'umi',rules):
            key=r['id'].removeprefix('ACC-')
            if key in ('sensor','calibration'):put(key,r['status'],r['detail'])
        serial=b.get('serial_devices');ports={x.get('path') for x in serial or []}
        wanted=[bind.get(x+'_port') if bind.get('mode')=='dual' else bind.get('single_port') for x in hands]
        matched=bool(wanted) and all(x and x in ports for x in wanted)
        put('serial','通过' if matched else '未测',f'UMI手侧={hands}；串口绑定已枚举={matched}；每侧相机和VIVE按绑定配置纳入数据契约')
    put('preview','未测',family.upper()+'预览需要实际收帧验证，不以接口在线或其他型号预览代替')
    session=h.get('session') or {};rec=session.get('recorder') or {};belongs=slot in (session.get('devices') or [])
    if belongs:
        status=session.get('result');put('session','通过' if status=='complete' and session.get('mcap_path') else '失败' if status=='invalid' else '待确认',f'历史会话={session.get("session_id")}；设备={slot}；result={status}')
        own,issues=stream_contract(profile,rec.get('streams') or {},bind,b.get('camera_bindings'),b.get('vive'))
        put('stream_set','失败' if issues else '通过' if own else '未测',f'历史会话={session.get("session_id")}；{family.upper()}数据流数={len(own)}；缺项={issues}；不接受其他设备的历史记录')
        values=[rec.get(x) for x in ('writer_rejected_count','writer_canceled_count','drop_count')]
        put('writer','未测' if any(x is None for x in values) else '待确认' if any(values) else '通过',f'本设备历史会话 rejected/canceled/drop={values}')
    else:
        for key in ('session','stream_set','writer'):put(key,'未测','没有属于当前 '+slot+' 的录制记录，不复用其他设备数据')
    limits=rules or {};expected=limits.get('streams') or {};own=(rec.get('streams') or {}) if belongs else {}
    configured=bool(expected) and all(('/devices/'+family) in x for x in expected)
    bad=[];missing=[]
    if configured:
        for topic,rule in expected.items():
            actual=own.get(topic,{}).get('actual_rate_hz')
            if not isinstance(actual,(int,float)):missing.append(topic)
            elif not rule['hz']*(1-rule['tolerance_ratio'])<=actual<=rule['hz']*(1+rule['tolerance_ratio']):bad.append(topic)
    put('stream_rates','未测' if not configured or not belongs or missing else '失败' if bad else '通过',f'{family.upper()}产品频率标准已配置={configured}；超差={bad}；缺读数={missing}')
    latency=rec.get('disk_latency_ms') if belongs else None;limit=limits.get('disk_latency_max_ms')
    put('disk_latency','未测' if latency is None or limit is None else '待确认' if latency>limit else '通过',f'延迟={latency}ms；本设备阈值={limit}ms')
    error=worker.get('error_code');overall=h.get('overall_state')
    put('health_consistency','失败' if overall=='healthy' and error else '通过' if overall and ready else '未测',f'overall={overall}；{slot} pipeline={ready}；error={error or "无"}')
    restarts=worker.get('restart_count',dev.get('restart_count'))
    put('stability','未测' if restarts is None else '待确认' if restarts else '通过',f'{slot}当前进程累计重启={restarts}；不将累计值归因于本轮，不推断重启原因')
    for key in ('record','data_layout','upload','button'):put(key,'未测','本轮尚未执行 '+family.upper()+' 专项操作')
    if profile.get('linked_slots'):
        states={target:(h.get('devices') or {}).get(target,{}) for target in profile['linked_slots']}
        ready_all=all(v.get('hardware_connected') is True and v.get('pipeline_state')=='ready' for v in states.values())
        put('selection','通过' if ready_all else '失败','联动设备就绪='+str({k:(v.get('hardware_connected'),v.get('pipeline_state')) for k,v in states.items()}))
        put('calibration','待确认','联动标定状态='+str({k:v.get('calibration_state') for k,v in states.items()})+'；仍需分别核对标定载体与精度')
        counts={k:((h.get('workers') or {}).get(k) or {}).get('restart_count') for k in states}
        put('stability','未测' if any(v is None for v in counts.values()) else '待确认' if any(counts.values()) else '通过','双方累计重启='+str(counts))
    return rows


def inspect(config,password='',key_file='',cancel=None,on_update=lambda s:None):
    from umi_functional import Client
    cancel=cancel or threading.Event();api=Client(config['host'],config.get('api_port',18000))
    scope=config.get('test_scope','all');requested=config.get('acceptance','auto')
    folder=BASE/'out/orangepi'/('modular-'+datetime.now().strftime('%Y%m%d_%H%M%S')+'-'+uuid4().hex[:6]);folder.mkdir(parents=True)
    session=dict(schema=1,id=folder.name,folder=str(folder),report=str(folder/'summary.html'),mode='orangepi',report_title='香橙派分模块验收',serial=config['host'],created=datetime.now().isoformat(timespec='seconds'),state='运行中',cases=[],framework_v2=True,test_scope=scope)
    try:
        if scope not in ('all','host','peripheral'):raise ValueError('测试范围无效')
        if scope=='host' and any(config.get(k) for k in ('test_record','test_upload','test_buttons')):raise ValueError('主机常规模式不执行外接设备实采')
        if cancel.is_set():raise RuntimeError('已取消')
        health=api.call('/api/v2/health',timeout=8)
        profile=resolve(health,requested) if scope!='host' else {'family':'host','slot':'host','guide':'仅主机常规检查，不读取外接设备专项接口'}
        session.update(device_profile=profile,acceptance_device=profile['family'])
        b,errors=collect(api,profile,scope,cancel)
        # Recheck selection after discovery: reconnecting during a run must not mix device evidence.
        if scope!='host' and resolve(b['health'],requested)!=profile:raise ValueError('检查期间设备配置发生变化，请重跑')
        session['device_info']={'sn':(b['health'].get('cloud') or {}).get('device_sn') or '', 'version':b.get('version') or ''}
        session['cases']=evaluate(b,profile,scope,config.get('acceptance_rules'))
        session['read_errors']=errors
        (folder/'metrics.json').write_text(json.dumps({'business':b,'profile':profile,'read_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        write_session(session);on_update(session)
        if config.get('test_record'):
            from umi_functional import run
            cfg=dict(config,device_profile=profile)
            functional=run(cfg,folder,cancel)
            lookup={r['id']:r for r in session['cases']}
            for r in functional['steps']:
                if r['id'] in lookup:lookup[r['id']].update(status=r['status'],detail=r['detail'])
            session['functional']={'session_id':functional.get('session_id'),'evidence':'functional.json','error':functional.get('error')}
            if functional.get('session'):
                b['health']['session']=functional['session']
                refreshed=evaluate(b,profile,scope,config.get('acceptance_rules'))
                for r in refreshed:
                    if r['id'] in ('ACC-session','ACC-writer','ACC-disk_latency','ACC-stream_rates'):lookup[r['id']].update(status=r['status'],detail=r['detail'])
        if config.get('test_login') and scope in ('all','host'):
            from orangepi_login_checks import run as login
            lookup={r['id']:r for r in session['cases']}
            for r in login(api,config.get('login_account',''),config.get('login_password','')):lookup['ACC-'+r['key']].update(status=r['status'],detail=r['detail'])
        session['state']='已结束（含未测或待确认）' if any(r['status']!='通过' for r in session['cases']) else '已完成'
        if any(r['status']=='失败' for r in session['cases']):session['state']='检查失败'
    except Exception as exc:
        session['state']='已停止' if cancel.is_set() else '检查未完成'
        session['error']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        if not session['cases']:session['cases']=[dict(id='ACC-api',scope='host',module='连接',title='目标设备与测试配置核实',status='未测',detail=session['error'])]
    finally:
        password='';config.pop('login_password',None);config.pop('login_account',None)
        write_session(session);on_update(session)
    return session
