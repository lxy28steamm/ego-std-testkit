"""Opt-in UMI functional acceptance. Creates isolated data; never deletes recordings."""
import json
from pathlib import Path
import threading
import time
import urllib.request
import urllib.parse
from uuid import uuid4
from datetime import datetime

class ButtonSessionChanged(RuntimeError):
    pass

BUSY={'preparing','recording','stopping','finalizing'}

class Client:
    def __init__(self,host,port=18000):
        self.base=f'http://{host}:{int(port)}'
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(self,path,body=None,method=None,timeout=30):
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,
            method=method or ('POST' if body is not None else 'GET'),headers={'Content-Type':'application/json'})
        with self.opener.open(req,timeout=timeout) as response:
            return json.load(response)


def validate_mcap(path):
    from mcap.reader import make_reader
    from mcap.stream_reader import StreamReader
    path=Path(path)
    with path.open('rb') as f:
        f.seek(-8,2)
        if f.read()!=b'\x89MCAP0\r\n': raise ValueError('MCAP 尾部不完整')
    # Scan all records, including decompression and CRC validation where present.
    with path.open('rb') as f:
        for _ in StreamReader(f,validate_crcs=True).records: pass
    with path.open('rb') as f:
        reader=make_reader(f,validate_crcs=True)
        summary=reader.get_summary()
        if not summary or not summary.statistics: raise ValueError('缺少 MCAP 汇总统计')
        topics={}; first={}; last={}; regressions={}
        for _,channel,message in reader.iter_messages(log_time_order=False):
            topic=channel.topic
            topics[topic]=topics.get(topic,0)+1
            first.setdefault(topic,message.log_time)
            if topic in last and message.log_time<last[topic]: regressions[topic]=regressions.get(topic,0)+1
            last[topic]=message.log_time
        if not topics or any(regressions.values()): raise ValueError('消息为空或流内时间戳倒退')
        if sum(topics.values())!=summary.statistics.message_count: raise ValueError('实际消息数与汇总不一致')
        return {'message_count':sum(topics.values()),'topics':topics,'regressions':regressions,
                'durations':{k:(last[k]-first[k])/1e9 for k in topics},'size_bytes':path.stat().st_size,
                'channels':[{'topic':c.topic,'encoding':c.message_encoding} for c in summary.channels.values()],
                'schemas':[s.name for s in summary.schemas.values()],
                'attachments':[{'name':a.name,'media_type':a.media_type} for a in reader.iter_attachments()],
                'metadata_names':[m.name for m in reader.iter_metadata()]}


def run(config,folder,cancel=None,log=lambda text:None):
    cancel=cancel or threading.Event();folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    api=Client(config['host'],config.get('api_port',18000))
    profile=config.get('device_profile') or {'family':'umi','slot':'umi'}
    family=profile['family'];slot=profile['slot']
    if family not in ('ego','umi','dex'):raise ValueError('不支持的外接设备类型')
    evidence={'created':datetime.now().isoformat(),'device':family,'device_slot':slot,'steps':[]}; owned=None; task_changed=False
    def save(): (folder/'functional.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    def emit(key,status,detail):
        evidence['steps'].append(dict(id='ACC-'+key,status=status,detail=detail));save();log(detail)
    def check():
        if cancel.is_set(): raise RuntimeError('验收已取消')
    def stop_owned():
        current=api.call('/api/v2/session')
        if owned and current.get('session_id')!=owned: raise RuntimeError('Session ownership changed; refusing to stop or reuse it')
        if owned and current.get('session_id')==owned and current.get('state') in BUSY:
            stopped=api.call('/api/v2/session/stop',{'command_id':'accept-stop-'+uuid4().hex,'reason':'operator_request'},timeout=90)
            if stopped.get('session_id')!=owned: raise RuntimeError('Stop returned another session')
            return stopped
        return current
    previous={};name='acceptance-'+family+'-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6]
    try:
        check()
        import importlib.util
        if not importlib.util.find_spec('mcap'): raise RuntimeError('缺少 MCAP 校验依赖，请先安装 mcap')
        current=api.call('/api/v2/session')
        if current.get('state') in BUSY: raise RuntimeError('设备已有采集任务，不启动测试也不停止现有采集')
        health=api.call('/api/v2/health')
        storage=health.get('storage') or {}
        if storage.get('state') != 'healthy' or not isinstance(storage.get('free_bytes'), (int, float)) or storage['free_bytes'] <= 0:
            raise RuntimeError('存储状态异常或剩余空间不可用，停止实采；请检查挂载与供电')
        from orangepi_profiles import stream_contract,required_hands
        targets=profile.get('linked_slots') or [slot]
        if profile.get('linked_slots') and config.get('test_upload'): raise RuntimeError('联动上传类别尚未确认，请取消上传选项后执行联动采集')
        if set((health.get('selection') or {}).get('enabled_devices') or [])!=set(targets):
            raise RuntimeError('实采要求仅启用目标 '+slot+'；不自动切换配置或混录其他设备')
        for target_slot in targets:
            target=(health.get('devices') or {}).get(target_slot) or {}
            if target.get('hardware_connected') is not True or target.get('pipeline_state')!='ready': raise RuntimeError(target_slot+' 未就绪')
        evidence['target_devices']=targets
        bindings={};cameras={};vive={};hands=[]
        if family in ('umi','dex'):
            bindings=api.call('/api/v2/'+family+'/bindings')
            ports=api.call('/api/v2/'+family+'/serial-devices')
            hands=required_hands(bindings)
            if not hands and family=='umi':
                hands=[p.get('detected_hand') for p in ports if p.get('selected') and p.get('detected_hand') in ('left','right')]
            configured=[bindings.get(h+'_port') if bindings.get('mode')=='dual' else bindings.get('single_port') for h in hands]
            if not hands or not all(port and any(p.get('path')==port and not p.get('detection_error') for p in ports) for port in configured):
                raise RuntimeError(family.upper()+'串口绑定未就绪')
            if family=='umi':
                cameras=api.call('/api/v2/umi/cameras/bindings');vive=api.call('/api/v2/umi/vive/bindings')
            evidence['bindings']=bindings
        else:
            target=(health.get('devices') or {}).get(slot) or {}
            if not target.get('hardware_connected') or target.get('pipeline_state')!='ready':raise RuntimeError('EGO相机/IMU未就绪')
            evidence['camera_serial']=target.get('hardware_serial')
        emit('serial','通过',family.upper()+'连接前置检查通过；专项数据由本轮文件继续验证')
        previous=api.call('/api/v2/collection-task');evidence['previous_task']={k:previous.get(k) for k in ('name','source')}
        if previous.get('source')=='assigned': raise RuntimeError('当前为分配任务，不能自动切到本地验收任务，请先在设备端切到测试任务')
        created=api.call('/api/v2/collection-task',{'name':name},method='POST');task_changed=True
        actual_task=created.get('storage_name') or created.get('name') or name
        evidence['actual_task']=actual_task
        check();evidence['test_task']=name;save()
        try:
            started=api.call('/api/v2/session/start',{'command_id':'accept-start-'+uuid4().hex},timeout=60)
        except Exception:
            candidate=api.call('/api/v2/session')
            if candidate.get('session_id') and candidate.get('task_storage_name')==actual_task and set(candidate.get('devices') or [])==set(targets) and candidate.get('session_id')!=current.get('session_id'):
                owned=candidate.get('session_id')
            raise
        if started.get('session_id') and started.get('task_storage_name')==actual_task and started.get('session_id')!=current.get('session_id'):
            owned=started['session_id']
        if started.get('task_storage_name')!=actual_task or set(started.get('devices') or [])!=set(targets) or started.get('session_id')==current.get('session_id'):
            raise RuntimeError('Started session does not match test task and devices')
        owned=started.get('session_id');evidence['session_id']=owned;save()
        if not owned: raise RuntimeError('启动接口没有返回会话 ID，停止后续步骤并检查设备')
        deadline=time.monotonic()+40
        while started.get('state')!='recording':
            check()
            if time.monotonic()>deadline or started.get('state') not in ('preparing','recording'): raise RuntimeError('本轮采集未进入 recording')
            if cancel.wait(.5): check()
            started=api.call('/api/v2/session')
            if started.get('session_id')!=owned: raise RuntimeError('会话发生变化')
        seconds=int(config.get('record_seconds',15));deadline=time.monotonic()+seconds
        samples=[]
        if config.get('capture_preview'):
            try:
                with api.opener.open(api.base+'/api/v2/preview/snapshot.jpg',timeout=15) as response:
                    image=response.read(16*1024**2)
                if not image.startswith(b'\xff\xd8'): raise ValueError('不是 JPEG')
                (folder/'umi-preview.jpg').write_bytes(image)
                evidence['preview']='umi-preview.jpg'
                emit('preview','通过','本轮录制期间预览接口返回 JPEG；物理屏幕显示需现场确认')
            except Exception as exc: emit('preview','未测','预览未验证：'+type(exc).__name__)
        while time.monotonic()<deadline:
            check();state=api.call('/api/v2/session')
            if profile.get('linked_slots'):
                live=api.call('/api/v2/health')
                if (live.get('storage') or {}).get('state')!='healthy': raise RuntimeError('Linked capture storage fault')
                if set((live.get('selection') or {}).get('enabled_devices') or []) != set(targets): raise RuntimeError('Linked selection changed')
                evidence['linked_health']=live
                for target_slot in targets:
                    dev=(live.get('devices') or {}).get(target_slot) or {}
                    if dev.get('hardware_connected') is not True or dev.get('pipeline_state')!='ready': raise RuntimeError('Linked device not ready: '+target_slot)
            if state.get('session_id')!=owned:
                if config.get('test_buttons'): raise ButtonSessionChanged('按键测试改变了会话，请结合设备日志核实停止与启动动作；不判为设备故障')
                raise RuntimeError('会话已变化；不能将其他会话作为本轮实采证据')
            if state.get('state')!='recording':
                if config.get('test_buttons') and state.get('state') in ('stopping','finalizing','complete','degraded'):
                    evidence['button_stop_observed']=state
                    log('按键观察模式：录制已停止，继续等待文件收尾')
                    break
                raise RuntimeError('短录制意外中断')
            snapshot=api.call('/api/v2/umi/sensor-status') if family=='umi' else {'hands':{hand:api.call('/api/v2/dex/realtime?hand='+hand) for hand in hands}} if family=='dex' else {'device':(api.call('/api/v2/health').get('devices') or {}).get(slot,{})};samples.append(snapshot)
            evidence['sensor_samples']=samples;save()
            log('SENSOR '+json.dumps(snapshot,ensure_ascii=False))
            cancel.wait(min(.25 if config.get('test_buttons') else 1,max(0,deadline-time.monotonic())))
        evidence['sensor_samples']=samples
        transitions={}
        for hand in hands:
            states=[v.get('button_pressed') for snap in samples for v in snap.get('sides',[]) if v.get('side')==hand]
            transitions[hand]=any(a is False and b is True for a,b in zip(states,states[1:])) and any(a is True and b is False for a,b in zip(states,states[1:]))
        evidence['button_transitions']=transitions
        emit('button','通过' if hands and all(transitions.values()) else '未测','本轮按键按下/释放边沿：'+str(transitions)+'；未观察到完整边沿的侧需补测')
        final=stop_owned()
        deadline=time.monotonic()+90
        while final.get('state') in BUSY:
            if time.monotonic()>deadline: raise RuntimeError('文件收尾超时')
            time.sleep(.5);final=api.call('/api/v2/session')
            if final.get('session_id')!=owned: raise RuntimeError('收尾期间会话发生变化')
        evidence['session']=final;save()
        if final.get('result') not in ('complete','degraded') or not final.get('mcap_path'): raise RuntimeError('录制没有生成完整文件')
        rec=final.get('recorder') or {};streams=rec.get('streams') or {}
        if rec.get('drop_count') or rec.get('error_code'): emit('quality','失败','本轮录制存在丢帧或错误，请查看 functional.json')
        else: emit('quality','待确认' if final['result']=='degraded' else '通过','本轮录制结果='+final['result']+'；drop_count='+str(rec.get('drop_count')))
        if family=='umi':
            ready=any(s.get('state')=='ready' and all(any(v.get('side')==h and v.get('valid') for v in s.get('sides',[])) for h in hands) for s in samples)
        elif family=='dex':
            ready=bool(hands) and any(all((s.get('hands',{}).get(hand) or {}).get('connected') and (s.get('hands',{}).get(hand) or {}).get('data_fresh') and (s.get('hands',{}).get(hand) or {}).get('joints') for hand in hands) for s in samples)
        else:
            ready=any((s.get('device') or {}).get('pipeline_state')=='ready' and (s.get('device') or {}).get('hardware_connected') for s in samples)
        emit('sensor','通过' if ready else '待确认',family.upper()+'本轮传感器数据/就绪状态已检查；详见采样证据')
        own,issues=stream_contract(profile,streams,bindings,cameras,vive)
        if profile.get('linked_slots'):
            ego,ego_issues=stream_contract({'family':'ego','slot':'ego-plus'},streams)
            own.update(ego);issues.extend(ego_issues)
            for topic in linked_required_topics(hands,bindings):
                if (streams.get(topic) or {}).get('message_count',0)<=0: issues.append('缺少联动数据流 '+topic)
            evidence['linked_stream_issues']=issues
        missing=[k for k,v in own.items() if v.get('missing_count')]
        emit('stream_set','通过' if own and not issues and not missing else '失败',family.upper()+'录制数据契约：'+str(issues)+'；missing='+str(missing))
        # Compatibility for the original UMI report IDs.
        emit('streams','通过' if own and not issues and not missing else '失败',family.upper()+'实际数据流数='+str(len(own)))
        files=api.call('/api/v2/files');directory=final['session_directory'];remote=final['mcap_path']
        candidates=[f for f in files if f['path']==remote or f['path'].endswith('/'+Path(remote).name)]
        if len(candidates)!=1: raise RuntimeError('无法唯一匹配本轮 MCAP 文件')
        file=candidates[0]
        if file['size_bytes']>512*1024**2: raise RuntimeError('短采集文件超过512MiB，保留设备文件并停止自动下载')
        local=folder/Path(remote).name;query=urllib.parse.urlencode({'path':file['path']})
        with api.opener.open(api.base+'/api/v2/files/download?'+query,timeout=90) as src, local.open('wb') as dst:
            copied=0
            while True:
                check();chunk=src.read(1024*1024)
                if not chunk: break
                copied+=len(chunk)
                if copied>512*1024**2: raise RuntimeError('下载超出限制')
                dst.write(chunk)
        if local.stat().st_size!=file['size_bytes']: raise RuntimeError('下载文件大小不一致')
        evidence['mcap']=validate_mcap(local)
        from orangepi_profiles import file_contract
        data_status,data_detail=file_contract(profile,evidence['mcap'])
        if profile.get('linked_slots'):
            missing_topics=[t for t in linked_required_topics(hands,bindings) if (evidence['mcap'].get('topics') or {}).get(t,0)<=0]
            data_status='失败' if missing_topics else data_status
            data_detail+='；联动文件缺失流='+str(missing_topics)+'；容器检查不代表跨设备严格同步。'
        emit('data_layout',data_status,data_detail)
        emit('record','通过','本轮短采集及停止完成；MCAP 尾部、CRC（若存在）、实际消息数及流内时间戳检查通过；文件='+local.name)
        if config.get('test_upload'):
            check();auth=api.call('/api/v2/cloud/login/session')
            if not auth.get('authenticated') or not auth.get('account_id'):
                emit('upload','未测','当前没有云账号登录；保留本轮文件，未尝试上传')
            else:
                folders=api.call('/api/v2/task-folders')
                target=[f for f in folders if f.get('path')==Path(directory).name or f.get('path')==directory]
                if len(target)!=1: raise RuntimeError('无法唯一匹配隔离测试目录，禁止上传其他目录')
                if target[0].get('file_count')!=1 or len(target[0].get('source_paths',[]))!=1 or Path(target[0]['source_paths'][0]).name!=Path(remote).name:
                    raise RuntimeError('测试目录包含非本轮文件，停止上传')
                path=target[0]['path']
                if not target[0].get('collection_task_id'):
                    task_id=str(config.get('assigned_task_id') or '').strip()
                    if not task_id.isdigit() or int(task_id)<=0:
                        emit('upload','未测','本地验收目录需要绑定系统任务；请指定已授权的任务 ID 后上传。文件保留。')
                        return evidence
                    tasks=api.call('/api/v2/collection-task/assigned?createdSort=desc')
                    if not any(str(t.get('taskId'))==task_id and t.get('taskStatus')=='open' for t in tasks.get('items',[])):
                        raise RuntimeError('指定系统任务不可用或已关闭')
                    api.call('/api/v2/task-folders/bind-assigned',{'taskId':task_id,'path':path},method='PUT')
                    folders=api.call('/api/v2/task-folders')
                    target=[f for f in folders if str(f.get('collection_task_id'))==task_id and any(Path(x).name==Path(remote).name for x in f.get('source_paths',[]))]
                    if len(target)!=1 or target[0].get('file_count')!=1:
                        raise RuntimeError('绑定后目录包含其他文件，禁止批量上传')
                    path=target[0]['path'];evidence['binding']={'taskId':task_id,'upload_path':path}
                job=api.call('/api/v2/cloud/uploads/unified',{'paths':[path],'upload_category':{'ego':'ego_stereo','umi':'umi','dex':'dex'}[family],'account_id':auth['account_id'],'delete_after_upload':False},timeout=60)
                evidence['upload']=job;save();deadline=time.monotonic()+300
                while job.get('state') not in ('complete','failed'):
                    check()
                    if time.monotonic()>deadline: raise RuntimeError('上传等待超时；上传可能仍在设备后台运行')
                    cancel.wait(2);job=api.call('/api/v2/cloud/uploads/unified/'+urllib.parse.quote(job['job_id'],safe=''))
                evidence['upload']=job
                if job.get('state')!='complete': emit('upload','失败','设备上传任务失败：'+str(job.get('error_code')))
                else: emit('upload','待确认','设备报告上传完成；remote_task_ids='+str(job.get('remote_task_ids'))+'；本地文件保留。尚未独立读取云端文件，云端验收待确认。')
        else: emit('upload','未测','本轮未选择上传')
    except Exception as exc:
        evidence['error']=type(exc).__name__+': '+str(exc);save();log(evidence['error'])
        failed_key='upload' if any(s['id']=='ACC-record' and s['status']=='通过' for s in evidence['steps']) else 'record'
        emit(failed_key,'未测' if cancel.is_set() or isinstance(exc,ButtonSessionChanged) else '失败','流程未完成：'+type(exc).__name__+'；详见 functional.json')
    finally:
        if owned:
            try:
                if config.get('test_buttons'):
                    latest=api.call('/api/v2/session')
                    if latest.get('session_id')==owned and latest.get('task_storage_name')==evidence.get('actual_task') and latest.get('state') in BUSY:
                        api.call('/api/v2/session/stop',{'command_id':'accept-button-stop-'+uuid4().hex,'reason':'operator_request'},timeout=90)
                else: stop_owned()
            except Exception as exc:
                evidence['cleanup_error']=type(exc).__name__
                emit('record','失败','本轮会话收尾未确认，请检查设备；'+type(exc).__name__)
        # Never delete the test folder. Restore only a named local task, and only if nobody changed it.
        if task_changed:
            try:
                now=api.call('/api/v2/collection-task')
                latest=api.call('/api/v2/session')
                if latest.get('state') in BUSY: raise RuntimeError('Active session; task restoration skipped')
                if (now.get('storage_name') or now.get('name'))==evidence.get('actual_task',name) and previous.get('name'):
                    api.call('/api/v2/collection-task/select',{'name':previous['name']},method='PUT')
                elif not previous.get('name'): evidence['task_note']='原任务名为空，接口不接受空名；保留测试任务供检查，可在设备端手动选择其他任务。'
            except Exception as exc:
                evidence['restore_error']=type(exc).__name__
                emit('record','失败','原任务恢复未确认；'+type(exc).__name__)
        save()
    return evidence


def linked_required_topics(hands,bindings=None):
    cameras=['/devices/dex/cameras/'+hand+'/video' for hand in hands if (bindings or {}).get(hand+'_camera')]
    return cameras + ['/devices/ego-plus/cameras/'+side+'/video' for side in ('left','right')] + ['/devices/ego-plus/imu'] + ['/devices/dex/hands/'+hand+'/state' for hand in hands]
