"""Conservative page classification; never treats a missing control as success."""
import hashlib

NAMES={'preview':'视频预览','recording':'正在采集','device_connected':'指定设备已连接页面','provision':'设备配网页',
       'device_scan':'设备扫描页','login':'登录页','file_picker':'系统文件选择器','permission':'系统权限弹窗',
       'task_ready':'任务待采集','task_list':'任务中心','home':'APP主页','connection_error':'设备连接错误','unknown':'未识别页面'}

def classify(tree):
    nodes=list(tree.iter('node'));packages={n.get('package','') for n in nodes}
    labels=[n.get('content-desc') or n.get('text','') for n in nodes if n.get('class')!='android.widget.EditText' and n.get('password')!='true']
    text='\n'.join(labels);exact=set(labels)
    if any('documentsui' in x for x in packages):return 'file_picker'
    if any('permissioncontroller' in x or 'packageinstaller' in x for x in packages):return 'permission'
    if '欢迎登录' in text or '登录' in exact and any(n.get('password')=='true' for n in nodes):return 'login'
    if '连接失败' in text or '蓝牙服务发现未完成' in text:return 'connection_error'
    if {'结束采集','停止采集'} & exact:return 'recording'
    if '视频预览' in text and '左目 ·' in text and '右目 ·' in text:return 'preview'
    if '当前设备' in text and 'SN:' in text and ('查看设备' in exact or '已连接' in exact):return 'device_connected'
    if '设备配网' in text and ('免密码网络' in text or '连接网络' in text or 'Wi-Fi' in text):return 'provision'
    if '开始采集' in exact or '开始任务' in exact:return 'task_ready'
    if '开始扫描' in exact or '重新扫描' in exact:return 'device_scan'
    if '任务中心' in text:return 'task_list'
    if {'任务','我的'}<=exact:return 'home'
    return 'unknown'

def fingerprint(tree):
    # Excludes editable values, passwords, device SNs and task identifiers.
    shape=[(n.get('class',''),n.get('resource-id',''),n.get('bounds',''),n.get('enabled',''),n.get('clickable','')) for n in tree.iter('node')]
    return hashlib.sha256(repr(shape).encode()).hexdigest()[:16]
