"""Small Windows GUI for lossless H.264 UVC capture."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime

HERE=Path(__file__).resolve().parent
BACKEND=HERE/'backend.py'
NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)

class App:
    def __init__(self,root):
        self.root=root; root.title('USB 相机原始 H.264 录制');root.geometry('700x490')
        self.devices=[];self.modes=[];self.process=None;self.events=queue.Queue();self.busy=False
        self.device=tk.StringVar();self.mode=tk.StringVar();self.folder=tk.StringVar(value=str(Path.home()/'Videos'));self.status=tk.StringVar(value='请先刷新设备并选择相机')
        frame=ttk.Frame(root,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='USB 相机原始 H.264 录制',font=('Microsoft YaHei UI',15,'bold')).grid(row=0,column=0,columnspan=3,sticky='w',pady=(0,12))
        ttk.Label(frame,text='相机').grid(row=1,column=0,sticky='w')
        self.dc=ttk.Combobox(frame,textvariable=self.device,state='readonly',width=55);self.dc.grid(row=1,column=1,sticky='ew',padx=8);self.dc.bind('<<ComboboxSelected>>',self.select_device)
        self.refresh=ttk.Button(frame,text='刷新设备',command=self.load_devices);self.refresh.grid(row=1,column=2)
        ttk.Label(frame,text='采集模式').grid(row=2,column=0,sticky='w',pady=12)
        self.mc=ttk.Combobox(frame,textvariable=self.mode,state='readonly');self.mc.grid(row=2,column=1,sticky='ew',padx=8)
        ttk.Label(frame,text='保存位置').grid(row=3,column=0,sticky='w')
        ttk.Entry(frame,textvariable=self.folder).grid(row=3,column=1,sticky='ew',padx=8)
        ttk.Button(frame,text='选择文件夹',command=self.choose_folder).grid(row=3,column=2)
        buttons=ttk.Frame(frame);buttons.grid(row=4,column=0,columnspan=3,sticky='w',pady=16)
        self.start=ttk.Button(buttons,text='开始录制',command=self.start_record);self.start.pack(side='left',padx=(0,12))
        self.stop=ttk.Button(buttons,text='停止并保存',command=self.stop_record,state='disabled');self.stop.pack(side='left')
        ttk.Label(frame,textvariable=self.status,wraplength=620,foreground='#145d90').grid(row=5,column=0,columnspan=3,sticky='w')
        text=('选择设备提供的 H.264 模式，直接保存原始数据，不重编码。\n'
              '启动时会等待 SPS 参数集，并丢弃此之前收到的数据；请以实际文件校验为准。\n'
              '录制期间不要拔出相机；停止后检查分辨率、画面和 IMU 数据。')
        ttk.Label(frame,text=text,wraplength=620,justify='left').grid(row=6,column=0,columnspan=3,sticky='w',pady=16)
        self.log=tk.Text(frame,height=6,width=70,state='disabled');self.log.grid(row=7,column=0,columnspan=3,sticky='nsew');frame.columnconfigure(1,weight=1);frame.rowconfigure(7,weight=1)
        root.protocol('WM_DELETE_WINDOW',self.close);root.after(150,self.poll);self.load_devices()

    def append(self,text):
        self.log.config(state='normal');self.log.insert('end',text+'\n');self.log.see('end');self.log.config(state='disabled')
    def choose_folder(self):
        value=filedialog.askdirectory(initialdir=self.folder.get())
        if value:self.folder.set(value)
    def query(self,args,kind):
        self.status.set('正在读取设备信息……');self.busy=True
        def worker():
            try:
                p=subprocess.run([sys.executable,str(BACKEND),*args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=25,creationflags=NO_WINDOW)
                events=[json.loads(x) for x in p.stdout.splitlines() if x.strip().startswith('{')]
                self.events.put(('query',kind,p.returncode,events,p.stderr[-500:]))
            except Exception as exc:self.events.put(('query',kind,1,[],str(exc)))
        threading.Thread(target=worker,daemon=True).start()
    def load_devices(self):
        if self.process or self.busy:return
        self.query(['devices'],'devices')
    def select_device(self,_event=None):
        if self.process or self.busy:return
        i=self.dc.current()
        if i<0:return
        self.modes=[];self.mc['values']=();self.mode.set('');self.query(['formats','--device',str(i),'--name',self.devices[i]],'formats')
    def start_record(self):
        if self.process or self.busy:return
        i=self.dc.current();j=self.mc.current()
        if i<0 or j<0:messagebox.showerror('无法开始','请选择相机及 H.264 采集模式。');return
        dest=Path(self.folder.get())
        if not dest.is_dir():messagebox.showerror('无法开始','请选择存在的保存文件夹。');return
        m=self.modes[j];target=dest/('uvc_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.h264')
        cmd=[sys.executable,str(BACKEND),'record','--device',str(i),'--name',self.devices[i],'--format',str(m['index']),'--width',str(m['width']),'--height',str(m['height']),'--output',str(target)]
        try:self.process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',creationflags=NO_WINDOW)
        except Exception as exc:messagebox.showerror('启动失败',str(exc));return
        self.start.config(state='disabled');self.stop.config(state='normal');self.refresh.config(state='disabled');self.dc.config(state='disabled');self.mc.config(state='disabled');self.status.set('正在启动录制……');self.append('保存：'+str(target))
        proc=self.process
        def read():
            for line in proc.stdout:
                try:self.events.put(('record',json.loads(line)))
                except ValueError:self.events.put(('text',line.strip()))
            err=proc.stderr.read();code=proc.wait();self.events.put(('exit',code,err[-1500:]))
        threading.Thread(target=read,daemon=True).start()
    def stop_record(self):
        if self.process and self.process.poll() is None:
            self.stop.config(state='disabled');self.status.set('正在停止并保存……')
            try:self.process.stdin.write('stop\n');self.process.stdin.flush()
            except OSError:pass
    def poll(self):
        while True:
            try:item=self.events.get_nowait()
            except queue.Empty:break
            if item[0]=='query':
                _,kind,code,ev,err=item;self.busy=False
                if code or not ev or ev[-1]['type']=='error':self.status.set('读取失败：'+(ev[-1].get('message','') if ev else err));continue
                if kind=='devices':
                    self.devices=ev[-1]['devices'];self.dc['values']=[f'{i+1}. {x}' for i,x in enumerate(self.devices)];self.device.set('');self.mode.set('');self.mc['values']=();self.status.set('请选择目标相机' if self.devices else '没有发现相机')
                else:
                    self.modes=ev[-1]['formats'];self.mc['values']=[f"{x['width']} × {x['height']} · H.264 · {x['fps'] or '?'} fps" for x in self.modes]
                    preferred=next((i for i,x in enumerate(self.modes) if (x['width'],x['height'])==(3840,1080)),0)
                    if self.modes:self.mc.current(preferred)
                    self.status.set('请选择模式，再开始录制' if self.modes else '该相机没有报告 H.264 模式')
            elif item[0]=='record':
                e=item[1]
                if e['type']=='progress':self.status.set(('正在录制' if e['aligned'] else '等待关键帧')+f" · {e['seconds']} 秒 · {e['bytes']/1048576:.1f} MB")
                elif e['type']=='finished':
                    probe=e.get('probe',{});self.append(f"保存结束：{e['bytes']} 字节，样本 {e['samples']}，YCTC 标记 {e['yctc_markers']}；丢弃启动前样本 {e['skipped_samples_before_sps']}。分辨率检查：{probe.get('status','未执行')}。")
                elif e['type']=='error':self.append('错误：'+e['message'])
            elif item[0]=='text':self.append(item[1])
            elif item[0]=='exit':
                _,code,err=item;self.process=None;self.start.config(state='normal');self.stop.config(state='disabled');self.refresh.config(state='normal');self.dc.config(state='readonly');self.mc.config(state='readonly');self.status.set('保存完成：请核对画面与 IMU 数据' if code==0 else '录制未完成，请查看错误')
                if err:self.append(err)
        self.root.after(150,self.poll)
    def close(self):
        if self.process:
            messagebox.showinfo('请先停止','请先点击“停止并保存”，等待保存完成。');return
        self.root.destroy()

if __name__=='__main__':
    root=tk.Tk();App(root);root.mainloop()
