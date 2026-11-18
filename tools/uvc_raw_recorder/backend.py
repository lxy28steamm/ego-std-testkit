"""DirectShow H.264 capture: write camera samples without transcoding."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

DEPS = Path(__file__).resolve().parents[2] / 'out' / 'usbcam-build-deps'
if DEPS.exists():
    sys.path.insert(0, str(DEPS))
from comtypes import COMObject
from pygrabber.dshow_core import IAMStreamConfig, VIDEOINFOHEADER, qedit
from pygrabber.dshow_graph import FilterGraph, FilterType

H264 = '{34363248-0000-0010-8000-00AA00389B71}'
VIDEO = '{73646976-0000-0010-8000-00AA00389B71}'
VIDEOINFO = '{05589F80-C356-11CE-BF01-00AA0055595A}'
START = re.compile(b'\x00\x00\x00\x01|\x00\x00\x01')


def event(kind, **values):
    print(json.dumps({'type':kind, **values}, ensure_ascii=False), flush=True)


def formats(graph, device_index):
    graph.add_video_input_device(device_index)
    cfg = graph.filters[FilterType.video_input].get_out().QueryInterface(IAMStreamConfig)
    count, _ = cfg.GetNumberOfCapabilities()
    result = []
    for index in range(count):
        mt, _ = cfg.GetStreamCaps(index)
        item = mt.contents
        if str(item.subtype).upper() != H264 or str(item.formattype).upper() != VIDEOINFO:
            continue
        info = ctypes.cast(item.pbFormat, ctypes.POINTER(VIDEOINFOHEADER)).contents
        width, height = info.bmi_header.biWidth, abs(info.bmi_header.biHeight)
        duration = info.avg_time_per_frame
        if width > 0 and height > 0:
            result.append({'index':index,'width':width,'height':height,
                           'fps':round(10000000 / duration, 2) if duration else None,
                           'codec':'H.264'})
    return result


class RawCallback(COMObject):
    _com_interfaces_ = [qedit.ISampleGrabberCB]

    def __init__(self, target):
        super().__init__()
        self.target = target
        self.started = False
        self.samples = 0
        self.bytes = 0
        self.skipped_samples = 0
        self.yctc = 0
        self.error = None

    def SampleCB(self, this, sample_time, sample):
        return 0

    def BufferCB(self, this, sample_time, buffer, length):
        try:
            if length <= 0 or self.error:
                return 0
            data = ctypes.string_at(buffer, length)
            if not self.started:
                start = next((m.start() for m in START.finditer(data)
                              if m.end() < len(data) and data[m.end()] & 31 == 7), None)
                if start is None:
                    self.skipped_samples += 1
                    return 0
                data = data[start:]
                self.started = True
            self.target.write(data)
            self.samples += 1
            self.bytes += len(data)
            self.yctc += data.count(b'YCTC')
        except Exception as exc:
            self.error = repr(exc)
        return 0


def run(args):
    graph = FilterGraph()
    devices = graph.get_input_devices()
    if args.device < 0 or args.device >= len(devices) or devices[args.device] != args.name:
        raise ValueError('设备列表发生变化，请重新刷新并选择设备')
    if devices.count(args.name) != 1:
        raise ValueError('存在同名相机，无法唯一确认设备；请只保留目标相机后重试')
    found = formats(graph, args.device)
    matches = [f for f in found if f['index'] == args.format and f['width'] == args.width and f['height'] == args.height]
    if len(matches) != 1:
        raise ValueError('采集模式已变化，请重新刷新')
    graph.filters[FilterType.video_input].set_format(args.format)
    target = Path(args.output)
    with target.open('xb') as out:
        callback = RawCallback(out)
        graph._FilterGraph__add_filter(FilterType.sample_grabber, None)
        grabber = graph.filters[FilterType.sample_grabber]
        grabber.set_callback(callback, 1)
        grabber.set_media_type(VIDEO, H264)
        graph.add_null_render()
        graph.graph_builder.ConnectDirect(graph.filters[FilterType.video_input].get_out(), grabber.get_in(), None)
        graph.graph_builder.ConnectDirect(grabber.get_out(), graph.filters[FilterType.render].get_in(), None)
        stop = threading.Event()
        threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
        started = time.monotonic()
        try:
            graph.media_control.Run()
            event('started', file=str(target), mode=matches[0])
            while not stop.wait(1):
                if callback.error:
                    break
                event('progress', seconds=round(time.monotonic()-started), bytes=callback.bytes,
                      samples=callback.samples, aligned=callback.started)
                if args.seconds and time.monotonic()-started >= args.seconds:
                    break
        finally:
            graph.media_control.Stop()
            out.flush()
            os.fsync(out.fileno())
        ok = callback.started and callback.bytes > 0 and not callback.error
        probe = {'status':'not_run'}
        ffprobe = shutil.which('ffprobe')
        if ok and ffprobe:
            try:
                p = subprocess.run([ffprobe,'-v','error','-select_streams','v:0','-show_entries',
                                    'stream=codec_name,width,height','-of','json',str(target)],
                                   capture_output=True,text=True,encoding='utf-8',timeout=20,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                streams = json.loads(p.stdout).get('streams',[]) if p.returncode == 0 else []
                info = streams[0] if streams else {}
                matches_dimensions = info.get('codec_name')=='h264' and info.get('width')==args.width and info.get('height')==args.height
                probe = {'status':'matched' if matches_dimensions else 'mismatch','stream':info}
            except Exception as exc:
                probe = {'status':'error','error':str(exc)}
        result = {'file':str(target), 'device':devices[args.device], 'format':matches[0],
                  'samples':callback.samples, 'bytes':callback.bytes, 'yctc_markers':callback.yctc,
                  'skipped_samples_before_sps':callback.skipped_samples, 'error':callback.error,
                  'probe':probe, 'status':'completed_unverified' if ok else 'failed'}
        target.with_suffix(target.suffix+'.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        event('finished', **result)
        if not ok:
            raise RuntimeError('未获取可用数据，文件保留供排查')


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('devices')
    p=sub.add_parser('formats');p.add_argument('--device',type=int,required=True);p.add_argument('--name',required=True)
    p=sub.add_parser('record');p.add_argument('--device',type=int,required=True);p.add_argument('--name',required=True);p.add_argument('--format',type=int,required=True);p.add_argument('--width',type=int,required=True);p.add_argument('--height',type=int,required=True);p.add_argument('--output',required=True);p.add_argument('--seconds',type=int,default=0)
    args=parser.parse_args()
    try:
        if args.command=='record':run(args)
        else:
            graph=FilterGraph(); devices=graph.get_input_devices()
            if args.command=='devices':event('devices', devices=devices)
            else:
                if args.device<0 or args.device>=len(devices) or devices[args.device]!=args.name:raise ValueError('设备列表发生变化')
                event('formats',formats=formats(graph,args.device))
    except Exception as exc:
        event('error',message=str(exc));sys.exit(1)

if __name__=='__main__':main()
