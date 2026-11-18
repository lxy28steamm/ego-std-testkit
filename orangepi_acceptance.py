"""Business acceptance from read-only UMI/DEX evidence; never infers a functional pass from nodes."""

def cases(device):
    names=[('api','系统','业务接口'),('identity','系统','设备 SN 与软件版本'),
           ('serial','设备','串口绑定与左右手识别'),('preview','设备','业务画面预览'),('selection','设备','设备配置与连接'),('sensor','设备','传感器就绪'),
           ('storage','存储','采集存储状态'),
           ('battery','供电','电池识别'),('battery_level','供电','电池电量'),
           ('battery_power','供电','电压与电流读取'),('charging','供电','充电状态'),
           ('network','网络','当前网络连接'),('ethernet','网络','有线网络连接'),
           ('wifi','网络','Wi-Fi 当前连接'),('wifi_scan','网络','Wi-Fi 扫描'),('cloud','网络','云端连接状态'),           ('session','采集','最近一次录制结果'),
           ('streams','采集','本轮目标设备数据流记录'),('quality','采集','录制丢帧与异常'),
           ('stability','采集','采集稳定性：队列水位与 Worker 重启'),
           ('record','专项待测','本轮采集启停与 MCAP 完整性'),
           ('upload','专项待测','上传及云端文件核验'),('display','专项待测','显示与交互'),
           ('button','专项待测','实体按键与状态反馈')]
    from orangepi_acceptance_extra import EXTRA_CASES
    names += EXTRA_CASES
    return [dict(id='ACC-'+k,module=g,title=t,status='未执行',detail='') for k,g,t in names]


def evaluate(data,device,rules=None):
    rows=cases(device); by={r['id']:r for r in rows}
    def put(key,status,detail): by['ACC-'+key].update(status=status,detail=detail)
    api=data.get('business',{})
    health=api.get('health') or {}
    if not health:
        for row in rows: row.update(status='未测',detail='未取得业务健康接口，不能判断实际功能；请检查后端和 API 端口')
        return rows
    put('api','通过','本轮通过设备本机业务健康接口读取状态；不等同于业务全功能通过')
    sn=(health.get('cloud') or {}).get('device_sn')
    version=api.get('version')
    put('identity','通过' if sn and version else '未测',f"设备 SN：{sn or '未获取'}；软件版本：{version or '未获取'}；对象：{device.upper()}")
    devices=health.get('devices') or {}; workers=health.get('workers') or {}
    slot=devices.get(device) or {}; worker=workers.get(device) or {}
    enabled=(health.get('selection') or {}).get('enabled_devices') or []
    active=slot.get('active') or device in enabled
    connected=slot.get('hardware_connected',worker.get('hardware_connected'))
    put('selection','通过' if active and connected else '待确认',f"目标 {device.upper()}；启用={bool(active)}；硬件连接={connected}。不自动切换设备配置。")
    put('serial','未测','串口绑定需在本轮功能测试核实')
    put('preview','未测','本轮没有取得预览画面')
    if device=='umi':
        sensor=api.get('sensor') or {}; sides=sensor.get('sides') or []
        required=[s for s in sides if s.get('required')]
        invalid=[str(s.get('side','未知')) for s in required if not s.get('valid')]
        state=sensor.get('state')
        status='未测' if state=='waiting' else '待确认' if invalid else '通过' if required and state=='ready' else '未测'
        put('sensor',status,f"UMI 状态={state or '未获取'}；必需侧={len(required)}；未就绪侧={','.join(invalid) or '无'}；waiting 不当作传感器故障。")
    else:
        put('sensor','未测','DEX 专项数据结构尚未验证；不套用 UMI 的传感器判断')
    storage=health.get('storage') or {}
    put('storage','通过' if storage.get('state')=='healthy' else '待确认',
        f"状态={storage.get('state','未获取')}；可用字节={storage.get('free_bytes','未获取')}。未实际写入校验文件。")
    from orangepi_power_network import evaluate as evaluate_power_network
    for row in evaluate_power_network(api):
        put(row['key'],row['status'],row['detail'])
    session=health.get('session') or {}; rec=session.get('recorder') or {}
    sid=session.get('session_id'); targets=session.get('devices') or []
    matches=device in targets
    result=session.get('result'); final=rec.get('final_path') or session.get('mcap_path')
    if not sid or not matches:
        for key in ('session','streams','quality'): put(key,'未测','没有属于当前目标设备的录制记录；不复用其他设备的结果')
    else:
        detail=f"历史会话 {sid}；设备={targets}；result={result}；final_path={final or '无'}"
        put('session','待确认' if result=='degraded' else '通过' if result=='complete' and final else '失败' if result in ('invalid','fault','failed') else '未测',detail)
        streams=rec.get('streams') or {}
        own={k:v for k,v in streams.items() if device in k.lower().split('/')}
        valid=own and all(isinstance(v.get('message_count'),(int,float)) and v['message_count']>0 for v in own.values())
        put('streams','待确认' if valid else '未测','历史记录：'+ '; '.join(f"{k}: count={v.get('message_count')}, rate={v.get('actual_rate_hz')}" for k,v in own.items())+'；未配置应有流清单及帧率阈值，不能据此认定流齐全。')
        error=rec.get('error_code') or session.get('error_code')
        drops=rec.get('drop_count')
        missing=[v.get('missing_count') for v in own.values()]
        bad=bool(error) or (isinstance(drops,(int,float)) and drops>0) or any(isinstance(x,(int,float)) and x>0 for x in missing)
        put('quality','失败' if bad else '通过' if valid and drops==0 and all(x==0 for x in missing) else '未测',
            f"历史会话 {sid}；error={error or '无'}；drop_count={drops}；各流 missing_count={missing}。仅为设备统计，未独立解析 MCAP。")
    # 采集稳定性：队列水位 + worker 重启次数（2026-10-26 实测当天 8 起 worker_exception
    # 导致自动停止并删除 partial；详见 docs/2026-10-26-UMI采集稳定性排查.md）
    worker_restarts = worker.get('restart_count')
    depth = rec.get('queue_depth'); capacity = rec.get('queue_capacity')
    hwm = rec.get('queue_high_watermark'); latency = rec.get('disk_latency_ms')
    state_text = session.get('state')
    if state_text in ('invalid', 'fault'):
        put('stability', '失败',
            f"最近会话状态={state_text}（recorder fault/invalid），本轮录制已被系统判定异常；"
            f"worker 重启次数={worker_restarts}；请用 umi_stability_check.py 扫描当天日志定位原因")
    elif isinstance(worker_restarts, int) and worker_restarts >= 1:
        put('stability', '失败',
            f"本次设备启动后 worker 重启 {worker_restarts} 次（阈值 1）；重启原因需结合日志核实；这是启动后累计值，不能直接归因于本轮采集。"
            f"当前健康不代表本轮稳定；请用 umi_stability_check.py 扫描当天日志")
    elif state_text == 'recording' and isinstance(depth, int) and capacity:
        ratio = depth / capacity
        put('stability', '通过' if ratio < 0.5 else '待确认',
            f"录制中队列水位 {depth}/{capacity}（{ratio:.0%}）；历史最高 {hwm}；"
            f"写盘延迟估计 {latency}ms（USB SSD 实测 ~700ms，仅记录）")
    else:
        put('stability', '未测', f'当前会话状态={state_text or "未获取"}，无实时队列数据；非录制态不判定队列')
    for key,detail in [('record','本轮没有执行启停或解析文件；需独立验证 MCAP 尾部、流清单、时长及时间戳'),
                       ('upload','本轮没有上传测试文件，也未核实云端文件；在线状态不能替代上传验收'),
                       ('display','本轮没有验证显示画面和交互结果'),('button','本轮没有触发实体按键并核对状态变化')]:
        put(key,'未测',detail)
    from orangepi_acceptance_extra import evaluate as extra
    for row in extra(api,device,rules):
        put(row['key'],row['status'],row['detail'])
    return rows
