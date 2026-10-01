"""设备 IP 解析与自动发现 —— 让 ego 脚本不用再手写 IP。

IP 变了的时候：
    python find_device.py --save     # 扫一遍，把结果记到 device_ip.txt
之后所有脚本都可以不带 --host 直接跑；显式传 --host 仍然优先。

ego 设备的辨识特征：REST API 监听 18000（本机可直接访问），SSH 开 22。
"""

import concurrent.futures as cf
import json
import os
import socket
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API_PORT = 18000
SSH_PORT = 22
SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_ip.txt")


# ---------------------------------------------------------------- 本机网段

def local_networks():
    """本机所有私有 IPv4 的 /24 前缀，例如 ['192.168.195', '10.2.0']。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    for probe in (("8.8.8.8", 80), ("223.5.5.5", 80)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(probe)
            ips.add(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()

    nets = []
    for ip in ips:
        parts = ip.split(".")
        if len(parts) != 4 or ip.startswith("127."):
            continue
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            nets.append(".".join(parts[:3]))
    return sorted(set(nets))


# ---------------------------------------------------------------- 端口扫描

def tcp_open(ip, port, timeout):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def scan_port(nets, port, timeout, workers=160):
    targets = ["%s.%d" % (n, i) for n in nets for i in range(1, 255)]
    hits = []
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(tcp_open, ip, port, timeout): ip for ip in targets}
        for f in cf.as_completed(futs):
            if f.result():
                hits.append(futs[f])
    return sorted(hits, key=lambda x: [int(p) for p in x.split(".")])


# ---------------------------------------------------------------- 设备信息

def http_json(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def describe(ip):
    """拉一次 API 确认这是 ego 设备，并取版本/状态。"""
    info = {"ip": ip, "api": False, "version": "", "app": "", "health": ""}
    health = http_json("http://%s:%d/api/v2/health" % (ip, API_PORT))
    spec = http_json("http://%s:%d/openapi.json" % (ip, API_PORT), timeout=6)
    if health is None and spec is None:
        return info
    info["api"] = True
    if spec:
        meta = spec.get("info") or {}
        info["app"] = meta.get("title", "")
        info["version"] = str(meta.get("version", ""))
        info["paths"] = len(spec.get("paths") or {})
    if health:
        data = health.get("data", health)
        if isinstance(data, dict):
            for key in ("overall_state", "state", "status"):
                if key in data:
                    info["health"] = str(data[key])
                    break
            sel = data.get("selection") or {}
            if isinstance(sel, dict):
                info["profile"] = str(sel.get("profile_id") or sel.get("profile") or "")
    return info


def discover(nets=None, timeout=0.45, want_ssh=False):
    """返回 (ego 设备列表, 端口开但 API 不通的 IP, 仅开 SSH 的 IP, 扫描的网段)。

    端口开放 != ego 设备：必须拉通 /openapi.json 或 /api/v2/health 才算数，
    否则 VPN 网关、别的服务（比如 10.2.0.1）会被误报成设备。
    """
    nets = nets or local_networks()
    if not nets:
        return [], [], [], []
    api_hits = scan_port(nets, API_PORT, timeout)
    devices, suspect = [], []
    for ip in api_hits:
        info = describe(ip)
        if info["api"]:
            devices.append(info)
        else:
            suspect.append(ip)
    ssh_only = []
    if want_ssh:
        ssh_hits = scan_port(nets, SSH_PORT, timeout)
        ssh_only = [ip for ip in ssh_hits if ip not in api_hits]
    return devices, suspect, ssh_only, nets


# ---------------------------------------------------------------- 记录读写

def read_saved_host():
    """读 device_ip.txt 里的第一个可用 IP。"""
    try:
        with open(SAVE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    return line
    except Exception:
        pass
    return None


def save_hosts(ips):
    """第一行必须是纯 IP —— run.bat 用 set /p 直接读它当默认设备。"""
    with open(SAVE_FILE, "w", encoding="utf-8") as f:
        f.write((ips[0] + "\n") if ips else "\n")
        if len(ips) > 1:
            f.write("# 以下为同网段发现的其它设备，脚本默认只用第一行\n")
            for ip in ips[1:]:
                f.write(ip + "\n")
    return SAVE_FILE


def resolve_host(hint=None, verbose=True):
    """脚本统一入口：显式 --host > device_ip.txt > 自动扫描。"""
    if hint:
        return hint
    saved = read_saved_host()
    if saved:
        return saved
    if verbose:
        print("[devip] 没有 device_ip.txt，正在扫描本机网段找设备…")
    devices, _, _, nets = discover()
    if devices:
        ip = devices[0]["ip"]
        if verbose:
            print("[devip] 发现设备 %s（网段 %s），已使用；跑 find_device.py --save 可记住"
                  % (ip, ",".join(nets)))
        return ip
    raise SystemExit("[devip] 未发现 ego 设备。检查：设备是否开机、本机 WiFi 是否和设备同一网段、"
                     "然后用 find_device.py 指定网段再试。")
