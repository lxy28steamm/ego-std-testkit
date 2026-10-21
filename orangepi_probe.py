"""Read-only Linux probe sent to Python over SSH stdin; never installed remotely."""
import datetime
import glob
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import time


def read(path):
    try:
        return Path(path).read_text(errors='replace').replace('\x00','').strip()
    except (OSError, ValueError):
        return None


def command(args, timeout=3):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ,LC_ALL='C'))
        restricted=any(x in p.stderr.lower() for x in ('not seeing messages','permission denied','no journal files'))
        return {'ok':p.returncode==0, 'text':p.stdout[:20000], 'restricted':restricted}
    except (OSError, subprocess.TimeoutExpired):
        return {'ok':False, 'text':''}


def cpu_ticks():
    text = read('/proc/stat')
    if not text:
        return None
    parts = [int(x) for x in text.splitlines()[0].split()[1:9]]
    return sum(parts), parts[3]+(parts[4] if len(parts)>4 else 0)


def collect(services):
    data = {'schema':1,'model':read('/proc/device-tree/model'), 'hostname':socket.gethostname(),
            'kernel':platform.release(),'architecture':platform.machine(), 'cpu_count':os.cpu_count(),
            'uptime':read('/proc/uptime'), 'boot_id':read('/proc/sys/kernel/random/boot_id')}
    release = read('/etc/os-release')
    data['system'] = {}
    for line in (release or '').splitlines():
        if '=' in line:
            k,v = line.split('=',1)
            if k in ('ID','PRETTY_NAME','VERSION_ID'):
                data['system'][k]=v.strip('"')
    first = cpu_ticks()
    time.sleep(1)
    second = cpu_ticks()
    data['cpu_busy'] = None
    if first and second and second[0]>first[0]:
        data['cpu_busy']=round(100*(1-(second[1]-first[1])/(second[0]-first[0])),2)
    try:
        data['load_average']=list(os.getloadavg())
    except OSError:
        data['load_average']=None
    memory = {}
    for line in (read('/proc/meminfo') or '').splitlines():
        parts=line.replace(':','').split()
        if len(parts)>1 and parts[0] in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):
            memory[parts[0]]=int(parts[1])*1024
    data['memory']=memory
    try:
        fs=os.statvfs('/')
        data['root_fs']={'bytes':fs.f_blocks*fs.f_frsize,'available':fs.f_bavail*fs.f_frsize,
                         'free':fs.f_bfree*fs.f_frsize,'readonly':bool(fs.f_flag & getattr(os,'ST_RDONLY',1))}
    except OSError:
        data['root_fs']=None
    temperatures=[]
    for name in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
        value=read(name+'/temp')
        try:
            celsius=float(value)/1000
            if -20<=celsius<=150:
                temperatures.append({'name':read(name+'/type') or Path(name).name,'celsius':celsius})
        except (TypeError,ValueError):
            pass
    data['temperatures']=temperatures
    data['storage']=[]
    for node in sorted(glob.glob('/sys/block/*')):
        name=Path(node).name
        if name.startswith(('loop','ram','zram','dm-')):
            continue
        try:
            size=int(read(node+'/size'))*512
            data['storage'].append({'name':name,'bytes':size,'model':read(node+'/device/model') or ''})
        except (TypeError,ValueError):
            pass
    ip=command(['ip','-j','address','show'])
    route=command(['ip','-j','route','show','default'])
    for key,result in [('interfaces',ip),('routes',route)]:
        try:
            data[key]=json.loads(result['text']) if result['ok'] else None
        except ValueError:
            data[key]=None
    data['ntp']=command(['timedatectl','show','--property=NTPSynchronized','--value'])
    data['services']={}
    for unit in services:
        status=command(['systemctl','show','--no-pager','--property=LoadState,ActiveState,SubState,Result','--',unit])
        data['services'][unit]={'available':status['ok']}
        for line in status['text'].splitlines():
            if '=' in line:
                k,v=line.split('=',1)
                if k in ('LoadState','ActiveState','SubState','Result'):
                    data['services'][unit][k]=v
    failed=command(['systemctl','--failed','--type=service','--no-legend','--plain','--no-pager'])
    data['failed_services']={'available':failed['ok'],
                             'units':[line.split()[0] for line in failed['text'].splitlines() if line.strip()][:50]}
    kernel=command(['journalctl','-k','-b','-p','err','-n','50','--no-pager','-o','cat'],timeout=4)
    lines=[x for x in kernel['text'].splitlines() if x.strip() and x.strip()!='-- No entries --']
    # Only counts/categories leave the board. Kernel log bodies are not retained.
    data['kernel_errors']={'available':kernel['ok'] and not kernel.get('restricted'), 'sample_count':len(lines),
        'categories':{key:sum(key in x.lower() for x in lines) for key in ('timeout','i/o error','oom','thermal','usb','mmc')}}
    usb=[]
    for node in sorted(glob.glob('/sys/bus/usb/devices/*')):
        vendor,product=read(node+'/idVendor'),read(node+'/idProduct')
        if vendor and product:
            usb.append({'id':vendor+':'+product,'name':read(node+'/product') or ''})
    data['usb']=usb
    data['video_nodes']=[{'node':'/dev/'+Path(x).name,'name':read(x+'/name') or ''}
                         for x in sorted(glob.glob('/sys/class/video4linux/video*'))]
    data['serial_nodes']=sorted(glob.glob('/dev/ttyUSB*')+glob.glob('/dev/ttyACM*'))
    data['gpio_nodes']=sorted(glob.glob('/dev/gpiochip*'))
    data['accelerator_nodes']=sorted(glob.glob('/dev/dri/renderD*')+glob.glob('/dev/rknpu*'))
    data['utc']=datetime.datetime.now(datetime.timezone.utc).timestamp()
    return data


if __name__=='__main__':
    print(json.dumps(collect(globals().get('SERVICE_UNITS',[])),ensure_ascii=False))
