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
print(f"目标 {HOST}（ssh ls@…，只读，不改设备）")

# 设备连不上是常态（关机 / 换网段 / IP 漂了），不能让它甩一坨 traceback 到面板日志里 ——
# 其他兄弟脚本（container_src / imu_serial_probe / collect_and_upload）都给了一行人话。
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    c.connect(HOST, username="ls", password="ls", timeout=15)
except Exception as exc:  # noqa: BLE001
    print(f"SSH 连接失败：{type(exc).__name__}: {exc}")
    print("先确认：设备开机了吗 / IP 变了吗（用 ④设备发现 扫一遍，或 run.bat 选 8） / "
          "和本机在同一网段吗（ipconfig 看本机 IPv4）")
    sys.exit(2)
print(f"已连上 {HOST}，开始逐项探日志…")

for title, cmd in CMDS:
    print("=" * 70)
    print("### %s" % title)
    print("-" * 70)
    try:
        _, o, e = c.exec_command(cmd, timeout=90)
        out = o.read().decode("utf-8", "replace").strip()
        err = e.read().decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"(这条没取到：{type(exc).__name__}: {exc})")
        continue
    print(out if out else ("(stderr) " + err if err else "(空)"))

c.close()
