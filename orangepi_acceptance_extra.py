"""Additional acceptance checks; missing product limits never imply a pass."""
import math,time

EXTRA_CASES=[('health_consistency','系统','总健康与 UMI Worker 状态一致性'),
 ('calibration','标定','UMI 左右手标定信息'),('storage_selection','存储','当前存储与挂载选项一致性'),
 ('storage_capacity','存储','剩余容量阈值'),('stream_set','采集','应有数据流完整性'),
 ('stream_rates','采集','各数据流频率与容差'),('writer','采集','写入拒绝及取消统计'),
 ('disk_latency','采集','写盘延迟阈值'),('heartbeat','网络','云端心跳时效与错误'),
 ('battery_limits','供电','电池电压范围及充电一致性'),('battery_long','供电','长时间充电及续航验证'),
 ('login','账号','账号登录验证'),('login_reject','账号','错误密码拒绝验证')]

DEFAULT_RULES={'storage_min_gib':2,'heartbeat_max_sec':300,'disk_latency_max_ms':None,
 'battery_min_mv':None,'battery_max_mv':None,'charging_current_sign':None,'streams':{}}

def validate_rules(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULT_RULES):raise ValueError('验收标准包含未知配置项')
    r=dict(DEFAULT_RULES);r.update(value)
    for key in ('storage_min_gib','heartbeat_max_sec','disk_latency_max_ms','battery_min_mv','battery_max_mv'):
        v=r[key]
        if v is not None and (not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<=0):raise ValueError(key+' 必须是正数或 null')
    if r['battery_min_mv'] is not None and r['battery_max_mv'] is not None and r['battery_min_mv']>=r['battery_max_mv']:raise ValueError('电池电压上下限顺序不正确')
    if r['charging_current_sign'] not in (None,'positive','negative'):raise ValueError('电流方向只能为 positive/negative/null')
    if not isinstance(r['streams'],dict):raise ValueError('streams 必须是按完整 topic 配置的对象')
    for topic,item in r['streams'].items():
        if not isinstance(topic,str) or not topic.startswith(('/devices/umi/','/devices/dex/','/devices/ego')) or not isinstance(item,dict):raise ValueError('流标准需指定完整设备 topic')
        hz=item.get('hz');tol=item.get('tolerance_ratio')
        if not isinstance(hz,(int,float)) or isinstance(hz,bool) or not math.isfinite(hz) or hz<=0:raise ValueError('流频率必须为正数')
        if not isinstance(tol,(int,float)) or isinstance(tol,bool) or not math.isfinite(tol) or not 0<=tol<1:raise ValueError('流频率容差需在0到1之间')
    return r

def evaluate(api,device,rules=None,now=None):
    rules=validate_rules(rules or {});rows=[]
    def put(key,status,detail):rows.append({'key':key,'status':status,'detail':detail})
    def num(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
    h=api.get('health') or {};w=(h.get('workers') or {}).get(device) or {};d=(h.get('devices') or {}).get(device) or {}
    overall=h.get('overall_state');ready=w.get('pipeline_state') or d.get('pipeline_state');connected=w.get('hardware_connected',d.get('hardware_connected'))
    conflict=overall=='healthy' and (ready=='fault' or w.get('error_code') or connected is True and ready not in ('ready',None))
    put('health_consistency','失败' if conflict else '通过' if overall and ready else '未测',f'overall={overall}；{device} pipeline={ready}；connected={connected}；worker_error={w.get("error_code") or "无"}；仅比较目标设备，不检查未选槽位')
    sensor=api.get('sensor') or {};hands=[x.get('side') for x in sensor.get('sides',[]) if x.get('required')]
    cal=api.get('calibration') or {};bindings=api.get('bindings') or {}
    cal_details=[];cal_bad=False;cal_missing=not hands
    for hand in hands:
        x=cal.get(hand)
        if not isinstance(x,dict):cal_missing=True;cal_details.append(str(hand)+': 未读取到标定信息');continue
        port=bindings.get(hand+'_port') if bindings.get('mode')=='dual' else bindings.get('single_port')
        consistent=x.get('hand')==hand and isinstance(x.get('size_bytes'),int) and x['size_bytes']>0 and bool(x.get('sha256')) and bool(x.get('crc32'))
        if port and x.get('port')!=port:consistent=False
        cal_bad|=not consistent;cal_details.append(f'{hand}: size={x.get("size_bytes")}；端口匹配={bool(port and x.get("port")==port)}；SHA256={x.get("sha256")}')
    put('calibration','失败' if cal_bad else '未测' if cal_missing else '待确认','；'.join(cal_details)+'；文件身份信息检查，尚未验证标定参数数值、几何精度及 MCAP 中对应关系')
    storage=api.get('storage') or {};cur=storage.get('current');opts=storage.get('options') or []
    selected=[o for o in opts if cur and cur in (o.get('storage_id'),o.get('path'))]
    if not storage:put('storage_selection','未测','未取得存储选项接口')
    elif len(selected)!=1:put('storage_selection','失败',f'当前存储 {cur} 未唯一匹配到选项')
    else:
        o=selected[0];ok=o.get('available') is True and o.get('mounted') is not False
        put('storage_selection','通过' if ok else '失败',f'current={cur}；available={o.get("available")}；mounted={o.get("mounted")}；不切换或弹出存储')
    free=selected[0].get('free_bytes') if len(selected)==1 else None
    limit=rules['storage_min_gib'];ok=num(free) and free>=0 and limit is not None
    put('storage_capacity','通过' if ok and free/1024**3>=limit else '失败' if ok else '未测',f'剩余={round(free/1024**3,2) if num(free) else "未知"}GiB；本轮提示阈值={limit}GiB')
    session=h.get('session') or {};rec=session.get('recorder') or {};belongs=device in (session.get('devices') or [])
    streams=rec.get('streams') or {} if belongs else {};expected=rules['streams'] if device=='umi' else {}
    missing=[k for k in expected if not isinstance(streams.get(k,{}).get('message_count'),(int,float)) or streams[k]['message_count']<=0]
    put('stream_set','未测' if not expected or not belongs else '失败' if missing else '通过',f'会话={session.get("session_id")}；应有流={len(expected)}；缺失={missing}；未提供产品流清单时不能判齐全')
    deviations=[];unknown=[]
    for topic,rule in expected.items():
        rate=streams.get(topic,{}).get('actual_rate_hz');nom=rule['hz'];tol=rule['tolerance_ratio']
        if not num(rate):unknown.append(topic)
        elif not nom*(1-tol)<=rate<=nom*(1+tol):deviations.append(f'{topic}: {rate}Hz，标准{nom}±{tol:.1%}')
    put('stream_rates','未测' if not expected or not belongs or unknown else '失败' if deviations else '通过',f'会话={session.get("session_id")}；偏离={deviations}；缺读数={unknown}；以明确配置的产品标准判定，不从本次实测反推标准')
    values=[rec.get(k) for k in ('writer_rejected_count','writer_canceled_count','drop_count')]
    put('writer','未测' if not belongs or not all(num(v) and v>=0 for v in values) else '待确认' if any(values) else '通过',f'会话={session.get("session_id")}；rejected/canceled/drop={values}；取消写入需结合停采边界判断')
    latency=rec.get('disk_latency_ms');limit=rules['disk_latency_max_ms']
    put('disk_latency','未测' if not belongs or not num(latency) or limit is None else '待确认' if latency>limit else '通过',f'会话={session.get("session_id")}；设备报告延迟={latency}ms；阈值={limit}ms；指标语义与存储型号未确认时不套用树莓派50ms标准')
    cloud=h.get('cloud') or {};stamp=cloud.get('last_heartbeat_time_ns');limit=rules['heartbeat_max_sec'];err=cloud.get('error_code')
    age=(time.time() if now is None else now)-stamp/1e9 if num(stamp) and stamp>0 else None
    put('heartbeat','失败' if err else '未测' if age is None or limit is None else '待确认' if age< -30 or age>limit else '通过',f'最近心跳约{round(age,1) if age is not None else "未知"}秒前；阈值={limit}s；error={err or "无"}；本机与设备时间偏差会影响判断')
    b=(h.get('host') or {}).get('battery') or {};mv=b.get('voltage_mv');ma=b.get('current_ma');lo=rules['battery_min_mv'];hi=rules['battery_max_mv'];sign=rules['charging_current_sign']
    known=lo is not None and hi is not None and sign is not None and num(mv) and num(ma) and isinstance(b.get('charging'),bool)
    bad=known and (not lo<=mv<=hi or ma!=0 and b['charging'] and ((ma>0)!=(sign=='positive')))
    put('battery_limits','未测' if not known else '待确认' if bad else '通过',f'电压={mv}mV；电流={ma}mA；范围={lo}~{hi}mV；充电正方向={sign}；无已确认的板卡标准时不作电气功能放行')
    put('battery_long','未测','未执行充电器插拔、充放电曲线、续航时长测试；需要现场条件和时长标准')
    put('login','未测','本轮未执行账号登录，已有登录状态不代表登录流程通过')
    put('login_reject','未测','本轮未提交错误密码；需专用测试账号和登录测试选项，避免影响当前业务登录态')
    return rows
