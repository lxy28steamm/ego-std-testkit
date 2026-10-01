# -*- coding: utf-8 -*-
"""SSH 到 ego 主机，直接读 IMU 串口，测真实输出速率与帧结构。

用途：排查 "YCTC IMU queue overflow" —— 判断 IMU 到底吐多快、服务配置的 200Hz 对不对得上。

用法：
    python imu_serial_probe.py --host 192.168.195.38
    python imu_serial_probe.py --host 192.168.199.66 --seconds 5
    python imu_serial_probe.py --host 192.168.195.38 --hexdump 64   # 看原始字节

依赖：paramiko（workspace .venv 里已装）。设备上只需 python3，无需额外库。
"""
from __future__ import annotations

import argparse
import base64
import sys

try:
    import paramiko
except ImportError:
    print("缺少 paramiko，先跑： .venv/Scripts/python.exe -m pip install paramiko")
    sys.exit(1)


def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


_fix_console()

REMOTE = r'''
import time, collections, sys
dev = sys.argv[1]
dur = float(sys.argv[2])
hexdump = int(sys.argv[3]) if len(sys.argv) > 3 else 0

try:
    f = open(dev, "rb", buffering=0)
except Exception as e:
    print("打开失败: %s" % e)
    print("提示：确认设备已接入，且当前用户在 dialout 组")
    sys.exit(2)

time.sleep(0.3)
try:
    f.read(1 << 20)          # 丢弃缓冲区里的陈旧数据
except Exception:
    pass

t0 = time.time()
data = b""
while time.time() - t0 < dur:
    try:
        ch = f.read(65536)
    except Exception as e:
        print("读取中断: %s" % e)
        break
    if ch:
        data += ch
    else:
        time.sleep(0.003)
el = time.time() - t0

print("=" * 62)
print("设备      : %s" % dev)
print("采样时长  : %.2f s" % el)
print("总字节    : %d" % len(data))
print("字节速率  : %.0f B/s   (%.1f KB/s)" % (len(data) / el, len(data) / el / 1024))
if not data:
    print("!! 没有读到任何数据 —— 设备未输出或服务已占用串口")
    sys.exit(0)

if hexdump:
    print("-" * 62)
    print("前 %d 字节:" % hexdump)
    b = data[:hexdump]
    for i in range(0, len(b), 16):
        print("  %04x  %s" % (i, b[i:i + 16].hex(" ")))

print("-" * 62)
print("按不同帧长折算帧率（找出与配置 200Hz 最接近的那个）:")
for fl in (20, 24, 28, 30, 36, 40, 46, 64, 76, 100, 128, 130, 216):
    hz = (len(data) / el) / fl
    mark = ""
    if 180 <= hz <= 220:
        mark = "   <== 接近配置 200Hz"
    print("  帧长 %4d 字节 -> %8.1f Hz%s" % (fl, hz, mark))

print("-" * 62)
print("自动检测帧长（字节自相关，找重复周期）:")
best = []
for p in range(6, 241):
    idx = range(0, len(data) - p, 13)
    n = 0
    m = 0
    for i in idx:
        n += 1
        if data[i] == data[i + p]:
            m += 1
    if n:
        best.append((m / n, p))
best.sort(reverse=True)
seen = set()
shown = 0
for rate, p in best:
    if shown >= 5:
        break
    # 跳过倍数周期（真正的帧长通常是最小的那个强周期）
    if any(abs(p - q) < 3 for q in seen):
        continue
    seen.add(p)
    hz = (len(data) / el) / p
    flag = "   <== 疑似真实帧长" if rate > 0.25 else ""
    print("  周期 %3d 字节  匹配率 %.0f%%  -> 帧率 %.1f Hz%s" % (p, rate * 100, hz, flag))
    shown += 1

print("-" * 62)
print("候选帧头扫描（出现频率稳定的才是真帧头）:")
for h in ("5359", "aa55", "55aa", "a55a", "5555", "5953"):
    pat = bytes.fromhex(h)
    pos, i = [], 0
    while True:
        j = data.find(pat, i)
        if j < 0:
            break
        pos.append(j)
        i = j + 1
    if len(pos) < 3:
        continue
    gaps = [pos[k + 1] - pos[k] for k in range(len(pos) - 1)]
    c = collections.Counter(gaps)
    common = c.most_common(3)
    top_gap, top_n = common[0]
    # 帧间隔越集中说明越像真帧头
    ratio = top_n / len(gaps) if gaps else 0
    print("  %s : %d 次 -> %6.1f Hz | 最常见间隔 %d 字节(占%.0f%%) %s"
          % (h, len(pos), len(pos) / el, top_gap, ratio * 100,
             "  <== 间隔集中，很像帧头" if ratio > 0.5 else ""))
print("=" * 62)
'''


def main() -> None:
    ap = argparse.ArgumentParser(description="测量 ego 设备 IMU 串口真实输出速率")
    ap.add_argument("--host", default=None,
                    help="设备 IP；省略则读 device_ip.txt，没有就自动扫描本机网段")
    ap.add_argument("--user", default="ls")
    ap.add_argument("--password", default="ls")
    ap.add_argument("--device", default="/dev/ttyACM0", help="串口设备路径")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--hexdump", type=int, default=0, help="打印前 N 字节十六进制")
    args = ap.parse_args()
    import devip
    args.host = devip.resolve_host(args.host)

    b64 = base64.b64encode(REMOTE.encode()).decode()
    cmd = ("echo '%s' | base64 -d > /tmp/_imu_probe.py && "
           "python3 /tmp/_imu_probe.py %s %s %s"
           % (b64, args.device, args.seconds, args.hexdump))

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(args.host, username=args.user, password=args.password, timeout=12)
    except Exception as exc:  # noqa: BLE001
        print(f"SSH 连接失败: {exc}")
        sys.exit(1)

    print(f"探测 {args.host} 的 {args.device}（{args.seconds}s）...")
    _, o, e = c.exec_command(cmd, timeout=int(args.seconds * 5 + 60))
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    print(out or err)
    c.close()


if __name__ == "__main__":
    main()
