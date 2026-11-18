"""External-device contracts. Selection never changes the device configuration."""
FAMILIES={'ego-lite-01':'ego','ego-std-235':'ego','ego-plus':'ego','umi':'umi','dex':'dex'}
GUIDES={
 'ego':'EGO：USB 相机/IMU → 核对实际型号和生效槽位 → 读取相机/IMU标定。数据检查双目视频、IMU和时间戳，不使用手套串口规则。',
 'umi':'UMI：左右手串口及手侧识别 → 每侧相机、VIVE（按实际配置）→ 每手标定信息。数据检查控制/夹爪、配置的视频和追踪流。',
 'dex':'DEX：左右手串口 → 近手USB相机（按实际配置）→ 每手URDF标定。数据检查关节/触觉/IMU遥测及已配置相机，不使用UMI标定接口。'}


def resolve(health,requested='auto'):
    selection=health.get('selection') or {}
    enabled=selection.get('enabled_devices')
    if enabled is None:enabled=[k for k,v in (health.get('devices') or {}).items() if v.get('active')]
    enabled=[x for x in enabled if x in FAMILIES]
    if requested == 'linked':
        if set(enabled) != {'ego-plus', 'dex'}: raise ValueError('联动测试要求仅启用 EGO Plus 和 DEX')
        return {'family':'dex','slot':'dex','linked_slots':['ego-plus','dex'],'guide':'EGO Plus + DEX 联动采集；双方数据独立核验'}
    candidates=[x for x in enabled if requested=='auto' or requested==x or FAMILIES[x]==requested]
    if len(candidates)!=1:raise ValueError('不能唯一确定外接设备，请核对设备端已启用配置并明确选择专项；当前='+str(enabled))
    slot=candidates[0];return {'family':FAMILIES[slot],'slot':slot,'guide':GUIDES[FAMILIES[slot]]}


def paths(profile):
    f=profile['family']
    if f=='ego':return {'camera_devices':'/api/v2/camera/stereo-uvc/devices','imu_settings':'/api/v2/camera/stereo-imu/settings','camera_settings':'/api/v2/camera/settings'}
    common={'bindings':f'/api/v2/{f}/bindings','serial_devices':f'/api/v2/{f}/serial-devices'}
    if f=='umi':common.update(sensor='/api/v2/umi/sensor-status',camera_bindings='/api/v2/umi/cameras/bindings',vive='/api/v2/umi/vive/bindings',cal_left='/api/v2/umi/calibration/left/info',cal_right='/api/v2/umi/calibration/right/info')
    else:common.update(realtime='/api/v2/dex/realtime',camera_devices='/api/v2/dex/camera-devices',urdf='/api/v2/dex/urdf-calibrations')
    return common


def required_hands(bindings):
    if bindings.get('mode')=='dual':return ['left','right']
    hand=bindings.get('single_hand')
    return [hand] if hand in ('left','right') else []


def stream_contract(profile,streams,bindings=None,cameras=None,vive=None):
    family=profile['family'];bindings=bindings or {};cameras=cameras or {};vive=vive or {}
    own={k:v for k,v in streams.items() if ('/devices/'+family) in k and (family!='ego' or not any(x in k for x in ['/umi/','/dex/']))}
    issues=[]
    def has(word):return any(word in k and v.get('message_count',0)>0 for k,v in own.items())
    if family=='ego':
        if not has('/video'):issues.append('缺少EGO视频数据')
        if not has('/imu'):issues.append('缺少EGO IMU数据')
    elif family=='umi':
        if not has('/control/'):issues.append('缺少UMI控制流')
        for hand in required_hands(bindings):
            camera=cameras.get(hand) or {}
            if camera.get('fisheye') and not has('/hands/'+hand+'/cameras/fisheye/video'):issues.append(hand+'缺鱼眼视频')
            if camera.get('stereo'):
                for side in ('left','right'):
                    if not has('/hands/'+hand+'/cameras/'+side+'/video'):issues.append(hand+'缺双目'+side)
            if vive.get('enabled') and vive.get(hand+'_serial') and not has('/vive/'+hand+'/raw'):issues.append(hand+'缺VIVE数据')
    else:
        if not own:issues.append('缺少DEX数据流')
        # No guessed telemetry topic name; exact sensor fields are validated separately.
        for hand in required_hands(bindings):
            if bindings.get(hand+'_camera') and not has('/cameras/'+hand+'/video'):issues.append(hand+'缺近手相机视频')
    for topic,stats in own.items():
        missing=stats.get('missing_count')
        if isinstance(missing,(int,float)) and missing>0:issues.append('流缺失 '+topic+': '+str(missing))
    return own,issues


def file_contract(profile,mcap):
    family=profile['family'];topics=list(mcap.get('topics') or {})
    video=[x for x in topics if any(k in x.lower() for k in ('video','camera','image')) and 'calibration' not in x.lower()]
    imu=[x for x in topics if 'imu' in x.lower() and 'calibration' not in x.lower()]
    schemas=mcap.get('schemas') or []
    if family=='ego':
        status='通过' if video and imu else '失败'
        detail=f'EGO文件：视频流={len(video)}；IMU流={len(imu)}；标定附件={mcap.get("attachments")}；标定元数据名={mcap.get("metadata_names")}。视频/IMU必须同时存在，标定精度不由此放行。'
    elif family=='umi':
        hands=[x for x in topics if any(w in x.lower() for w in ('gripper','hands','control')) and 'calibration' not in x.lower()]
        status='通过' if hands else '待确认'
        detail=f'UMI文件：控制/手侧流={hands}；视频流={len(video)}；schema={schemas}。实际左右手/相机/VIVE数量另由录制契约核对。'
    else:
        telemetry=[x for x in topics if any(w in x.lower() for w in ('joint','tactile','dex','finger')) and 'calibration' not in x.lower()]
        status='通过' if telemetry else '待确认'
        detail=f'DEX文件：关节/触觉候选流={telemetry}；视频流={len(video)}；schema={schemas}。未知编码需按协议补映射，不能按EGO视频+IMU规则判定。'
    return status,detail
