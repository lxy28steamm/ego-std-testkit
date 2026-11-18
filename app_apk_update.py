"""APK metadata inspection and data-preserving ADB updates for an explicit tablet."""
from pathlib import Path
import hashlib,json,re,shutil,struct,subprocess,zipfile
from datetime import datetime

PACKAGE='com.livsyn.livsyn_collection_app'


def read_apk(path):
    path=Path(path).resolve()
    if not path.is_file() or path.suffix.lower()!='.apk':raise ValueError('请选择存在的单个 APK 文件')
    with zipfile.ZipFile(path) as z:
        info=z.getinfo('AndroidManifest.xml')
        if info.file_size>8*1024**2:raise ValueError('APK manifest 过大')
        data=z.read(info)
    if len(data)<8 or struct.unpack_from('<H',data)[0]!=3:raise ValueError('不是有效的 Android 二进制 manifest')
    strings=[];attrs={};offset=8
    def length8(pos):
        x=data[pos];return ((x&127)<<8|data[pos+1],pos+2) if x&128 else (x,pos+1)
    def length16(pos):
        x=struct.unpack_from('<H',data,pos)[0]
        return (((x&32767)<<16)|struct.unpack_from('<H',data,pos+2)[0],pos+4) if x&32768 else (x,pos+2)
    def string(index):
        if index==0xffffffff:return ''
        if index>=len(strings):raise ValueError('manifest 字符串索引无效')
        return strings[index]
    while offset+8<=len(data):
        kind,header,size=struct.unpack_from('<HHI',data,offset)
        if size<8 or header<8 or offset+size>len(data):raise ValueError('manifest chunk 损坏')
        if kind==1:
            count,styles,flags,start,_=struct.unpack_from('<5I',data,offset+8)
            if count>200000 or offset+header+count*4>offset+size:raise ValueError('manifest 字符串表损坏')
            for i in range(count):
                pos=offset+start+struct.unpack_from('<I',data,offset+header+i*4)[0]
                if flags&256:
                    _,pos=length8(pos);length,pos=length8(pos);end=pos+length
                    if end>offset+size:raise ValueError('manifest 字符串越界')
                    strings.append(data[pos:end].decode('utf-8'))
                else:
                    length,pos=length16(pos);end=pos+length*2
                    if end>offset+size:raise ValueError('manifest 字符串越界')
                    strings.append(data[pos:end].decode('utf-16-le'))
        elif kind==0x102:
            name=string(struct.unpack_from('<I',data,offset+20)[0])
            attr_start,attr_size,count=struct.unpack_from('<HHH',data,offset+24)
            if attr_size<20:raise ValueError('manifest 属性格式无效')
            for i in range(count):
                pos=offset+16+attr_start+i*attr_size
                if pos+20>offset+size:raise ValueError('manifest 属性越界')
                ns,key,raw=struct.unpack_from('<III',data,pos)
                value_type=data[pos+15];value=struct.unpack_from('<I',data,pos+16)[0]
                text=string(raw) if raw!=0xffffffff else string(value) if value_type==3 else value
                if name=='manifest' and string(key) in ('package','versionCode','versionName','versionCodeMajor'):attrs[string(key)]=text
        offset+=size
    package=attrs.get('package');version=str(attrs.get('versionName') or '');code=attrs.get('versionCode')
    if not isinstance(package,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+',package):raise ValueError('无法识别 APK 包名')
    if not version or not isinstance(code,int):raise ValueError('无法识别 APK 版本，请使用完整单包 APK')
    major=attrs.get('versionCodeMajor',0)
    if not isinstance(major,int):raise ValueError('无效 versionCodeMajor')
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return dict(path=str(path),package=package,version_name=version,version_code=code+(major<<32),size_bytes=path.stat().st_size,sha256=digest.hexdigest())


def adb_path():
    found=shutil.which('adb') or 'D:/Android/Sdk/platform-tools/adb.exe'
    if not Path(found).is_file():raise ValueError('找不到 ADB，请安装 Android SDK platform-tools')
    return str(found)


def command(args,timeout=30):
    p=subprocess.run(args,capture_output=True,timeout=timeout,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode:raise RuntimeError((p.stdout+'\n'+p.stderr).strip()[-1600:] or 'ADB 命令失败')
    return p.stdout.strip()


def installed(serial,package=PACKAGE):
    if not serial or any(x.isspace() for x in serial) or serial.startswith('-'):raise ValueError('请明确选择目标平板序列号')
    adb=adb_path()
    if command([adb,'-s',serial,'get-state'])!='device':raise RuntimeError('目标平板不在线或未授权 USB 调试')
    output=command([adb,'-s',serial,'shell','dumpsys','package',package])
    code=re.search(r'\bversionCode=(\d+)',output);name=re.search(r'\bversionName=([^\r\n]+)',output)
    return dict(package=package,version_code=int(code[1]) if code else None,version_name=name[1].strip() if name else None)


def inspect(serial,path):
    meta=read_apk(path)
    if meta['package']!=PACKAGE:raise ValueError('APK 包名不匹配：'+meta['package']+'；本入口仅更新 LivFlow')
    before=installed(serial,meta['package'])
    if before['version_code'] is None:raise ValueError('平板未安装 LivFlow；此入口只做覆盖更新，请明确处理首次安装')
    if meta['version_code']<before['version_code']:raise ValueError('APK versionCode 低于当前版本，已阻止降级')
    return {'serial':serial,'apk':meta,'before':before}


def update(serial,path,folder,expected=None,log=lambda value:None):
    from app_patrol_engine import execution_lock
    with execution_lock():
        return _update(serial,path,folder,expected,log)


def _update(serial,path,folder,expected=None,log=lambda value:None):
    evidence=inspect(serial,path)
    if expected and expected.get('apk',{}).get('sha256')!=evidence['apk']['sha256']:raise ValueError('APK 检查后发生变化，请重新选择并检查')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    report=folder/'update.json';evidence.update(created=datetime.now().astimezone().isoformat(),state='安装中')
    def save():report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    save();meta=evidence['apk'];adb=adb_path()
    try:
        log('正在覆盖安装 '+meta['version_name']+' / '+str(meta['version_code'])+'；保留应用数据')
        output=command([adb,'-s',serial,'install','-r','--no-streaming',meta['path']],timeout=300)
        evidence['install_output']=output
        if not re.search(r'^Success\s*$',output,re.M):raise RuntimeError('安装结果没有明确返回 Success')
        after=installed(serial,meta['package']);evidence['after']=after
        if after['version_code']!=meta['version_code'] or after['version_name']!=meta['version_name']:raise RuntimeError('安装后的版本回读不一致')
        launch=command([adb,'-s',serial,'shell','cmd','package','resolve-activity','--brief',meta['package']])
        activity=next((line.strip() for line in reversed(launch.splitlines()) if line.strip().startswith(meta['package']+'/')),None)
        if not activity or not re.fullmatch(r'[A-Za-z0-9_.$/]+',activity):raise RuntimeError('已安装，但未解析到 LivFlow 启动入口')
        started=command([adb,'-s',serial,'shell','am','start','-W','-n',activity],timeout=30)
        evidence['launch_output']=started
        if not re.search(r'^Status:\s*ok\s*$',started,re.M):raise RuntimeError('版本回读成功，但未确认 APP 启动成功')
        evidence['pid']=command([adb,'-s',serial,'shell','pidof',meta['package']])
        if not evidence['pid']:raise RuntimeError('未确认到 APP 进程')
        evidence['state']='更新及启动验证通过';log(evidence['state'])
    except Exception as exc:
        evidence['state']='更新未完成';evidence['error']=str(exc);raise
    finally:save()
    return evidence
