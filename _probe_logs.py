"""一次性探针：摸清 ego 设备运行日志都在哪。"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else None

CMDS = [
    ("hostname/uptime", "hostname; uptime"),
    ("ego 相关 systemd 服务", "systemctl list-units --type=service --all --no-pager 2>/dev/null | grep -iE 'ego|livumi|laser|backend|docker' | head -20"),
    ("~/raspberry-ego-v2 目录", "ls -la ~/raspberry-ego-v2 2>/dev/null | head -30"),
    ("~/raspberry-ego-v2/logs", "ls -la ~/raspberry-ego-v2/logs 2>/dev/null | tail -20"),
    ("home 下的 .log 文件", "find ~ -maxdepth 4 -name '*.log' -not -path '*/node_modules/*' 2>/dev/null | head -40"),
    ("/var/log 下 ego 相关", "ls -la /var/log 2>/dev/null | head -30"),
    ("docker 日志目录", "ls -la /var/lib/docker/containers 2>/dev/null | head -5; sudo -n ls /var/lib/docker/containers 2>&1 | head -3"),
    ("容器内 /app/logs", "PID=$(ps -eo pid,args | grep backend.main | grep -v grep | awk '{print $1}' | head -1); echo PID=$PID; ls -la /proc/$PID/root/app/logs 2>/dev/null | tail -20"),
    ("容器内 /app/config 日志配置", "PID=$(ps -eo pid,args | grep backend.main | grep -v grep | awk '{print $1}' | head -1); ls -la /proc/$PID/root/app/config 2>/dev/null | head -20; cat /proc/$PID/root/app/config/logging*.y*ml 2>/dev/null | head -40"),
    ("进程 stdout 指向", "PID=$(ps -eo pid,args | grep backend.main | grep -v grep | awk '{print $1}' | head -1); ls -la /proc/$PID/fd/1 /proc/$PID/fd/2 2>&1"),
    ("journalctl 最近 20 行", "journalctl --no-pager -n 20 2>&1 | tail -20"),
]

import devip

HOST = devip.resolve_host(HOST)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username="ls", password="ls", timeout=15)

for title, cmd in CMDS:
    print("=" * 70)
    print("### %s" % title)
    print("-" * 70)
    _, o, e = c.exec_command(cmd, timeout=90)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    print(out if out else ("(stderr) " + err if err else "(空)"))

c.close()
