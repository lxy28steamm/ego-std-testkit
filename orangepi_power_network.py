"""Read-only power/network checks. Board-specific electrical limits are not assumed."""
import math

def evaluate(api):
    result=[]
    def put(key,status,detail):result.append(dict(key=key,status=status,detail=detail))
    def number(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
    h=api.get('health') or {};b=(h.get('host') or {}).get('battery') or {}
    present=b.get('present')
    put('battery','通过' if present is True else '待确认' if present is False else '未测',
        '电池已识别；来源='+str(b.get('source','未提供')) if present is True else '未检测到电池，请核对供电配置' if present is False else '接口未提供电池识别状态')
    pct=b.get('percent');valid=present is True
    put('battery_level','未测' if not valid or not number(pct) or not 0<=pct<=100 else '待确认' if pct<10 else '通过',
        f'当前电量={pct if pct is not None else "未获取"}%；低于10%提醒补电；不代表续航或容量验收')
    mv,ma=b.get('voltage_mv'),b.get('current_ma')
    put('battery_power','通过' if valid and number(mv) and mv>0 and number(ma) else '未测',
        f'电压={mv if mv is not None else "未获取"}mV；电流={ma if ma is not None else "未获取"}mA。仅验证参数可读；未沿用树莓派电压范围或电流正负约定。')
    charging=b.get('charging');status=b.get('status')
    put('charging','通过' if valid and isinstance(charging,bool) and status else '未测',
        f'充电标志={charging}；状态={status or "未获取"}。仅核对状态可读，未验证插拔充电器及真实充放电能力。')
    n=api.get('network');n=n if isinstance(n,dict) else {}
    interfaces=n.get('interfaces') or []
    def addresses(items):return '；'.join(str(x.get('interface') or x.get('name') or '未知网卡')+': '+str(x.get('ip_address') or '无IP') for x in items)
    active=[x for x in interfaces if x.get('connected') and x.get('ip_address')]
    connected=n.get('connected')
    put('network','通过' if active or connected is True and n.get('ip_address') else '待确认' if n else '未测',
        addresses(active) or ('主连接='+str(n.get('interface'))+'；IP='+str(n.get('ip_address'))) if n else '未读取到网络接口')
    def kind(x):
        return str(x.get('connection_type') or x.get('type') or '').lower()
    wired=[x for x in interfaces if kind(x) in ('ethernet','wired','802-3-ethernet') or str(x.get('interface','')).startswith(('eth','en'))]
    wireless=[x for x in interfaces if kind(x) in ('wifi','wireless','802-11-wireless') or str(x.get('interface','')).startswith(('wl','wlan'))]
    up=[x for x in wired if x.get('connected') and x.get('ip_address')]
    put('ethernet','通过' if up else '未测',addresses(wired)+'；仅验证当前链路与IP，未测试插拔和重连' if wired else '未枚举到有线网卡；未据此判定硬件损坏')
    scans=api.get('wifi');scans=scans if isinstance(scans,list) else None
    wifi_up=[x for x in wireless if x.get('connected') and x.get('ip_address')]
    ssids=[x.get('ssid') for x in scans or [] if x.get('active')]
    scan_text=f'扫描到 {len(scans)} 个网络' if scans is not None else '扫描结果未获取'
    put('wifi','通过' if wifi_up else '待确认' if scans else '未测',
        f'{addresses(wireless)}；{scan_text}；当前SSID={ssids or n.get("ssid") or "未获取"}；未切换或删除网络，未验证重连')
    put('wifi_scan','通过' if scans else '待确认' if scans is not None else '未测',
        scan_text+'；扫描结果不代表各网络可成功连接')
    cloud=h.get('cloud') or {};state=cloud.get('state')
    put('cloud','通过' if state=='online' else '待确认' if state else '未测',
        f'设备云端状态={state or "未获取"}；错误码={cloud.get("error_code") or "无"}。在线不代表文件上传或云端质检通过。')
    return result
