"""找 ego 设备 —— 设备换了 IP / 换了网段，跑这个就知道现在在哪。

用法:
  python find_device.py                          # 扫本机所在网段
  python find_device.py --extra 192.168.199      # 额外扫一个网段（写 /24 前缀即可）
  python find_device.py --save                   # 把第一台记为默认，后续脚本不用带 --host
  python find_device.py --ssh                    # 也列出只开 22 的机器（辅助判断设备是否只起了 SSH）
  python find_device.py --timeout 0.8            # 网络慢的时候放宽
  python find_device.py --list                   # 每行吐一个 IP（全量，供批量巡检用）

判定依据：ego 设备的 REST API 监听 18000，且 /openapi.json 可拉取。
"""

import argparse
import sys

import devip

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def normalize(net):
    net = net.strip()
    if net.endswith("/24"):
        net = net[:-3]
    parts = net.split(".")
    if len(parts) == 4:
        net = ".".join(parts[:3])
    if len(parts) < 3:
        raise SystemExit("网段格式不对，写 192.168.199 或 192.168.199.0/24")
    return net


def main():
    ap = argparse.ArgumentParser(description="发现局域网里的 ego 设备")
    ap.add_argument("--extra", action="append", default=[],
                    help="额外网段（可多次），如 192.168.199")
    ap.add_argument("--timeout", type=float, default=0.45, help="单个端口超时秒数")
    ap.add_argument("--save", action="store_true", help="把发现的设备写入 device_ip.txt")
    ap.add_argument("--quiet", action="store_true", help="只输出第一台设备 IP（供脚本/bat 调用）")
    ap.add_argument("--list", action="store_true",
                    help="每行输出一台设备的 IP（全量）。批量巡检用这个，不要用 --quiet")
    ap.add_argument("--ssh", action="store_true", help="同时扫 22 端口")
    args = ap.parse_args()

    nets = devip.local_networks()
    for extra in args.extra:
        nets.append(normalize(extra))
    nets = sorted(set(nets))
    if not nets:
        raise SystemExit("没识别出本机的私有网段，用 --extra 手动指定")

    total = len(nets) * 254
    if not args.quiet and not args.list:
        print("本机网段 : %s" % ", ".join(n + ".0/24" for n in nets))
        print("扫描目标 : %d 个 IP，端口 %d" % (total, devip.API_PORT))
        print("等待中…（约 %d 秒）" % max(2, int(total / 160 * args.timeout)))

    devices, suspect, ssh_only, _ = devip.discover(nets=nets, timeout=args.timeout, want_ssh=args.ssh)

    if args.list:
        # 全量输出，供 batch_check.py / 界面读取。注意不能用 --quiet，
        # 那个是历史行为「只给第一台」，run.bat 依赖它。
        for d in devices:
            print(d["ip"])
        if args.save and devices:
            devip.save_hosts([d["ip"] for d in devices])
        return

    if args.quiet:
        if devices:
            print(devices[0]["ip"])
            if args.save:
                devip.save_hosts([d["ip"] for d in devices])
        return

    print()
    if devices:
        print("发现 %d 台 ego 设备：" % len(devices))
        print("  %-16s %-6s %-14s %-10s %s" % ("IP", "API", "版本", "状态", "profile"))
        for d in devices:
            print("  %-16s %-6s %-14s %-10s %s" % (
                d["ip"], "OK" if d["api"] else "-",
                d.get("version") or "-", d.get("health") or "-", d.get("profile") or "-"))
    else:
        print("没有发现 ego 设备。")
        print("  排查：1) 设备是否开机、网络灯是否正常")
        print("        2) 本机 WiFi 是否和设备在同一网段（现在只有 %s）" % ", ".join(nets))
        print("        3) 已知设备在别的网段就用 --extra 指过去")

    if suspect:
        print()
        print("18000 端口开着但不像 ego API（已排除，可能是网关/别的服务）：")
        for ip in suspect:
            print("  " + ip)

    if ssh_only:
        print()
        print("只开 22 端口的机器（可能是设备只起了 SSH，或本机/路由器）：")
        for ip in ssh_only:
            print("  " + ip)

    if devices:
        ip = devices[0]["ip"]
        print()
        print("接下来这样用：")
        if args.save:
            path = devip.save_hosts([d["ip"] for d in devices])
            print("  已记住默认设备 -> %s" % path)
            print("  python ego_api_test.py --do-collect     # 不用再带 --host")
        else:
            print("  python find_device.py --save            # 记住默认设备")
            print("  python ego_api_test.py --host %s --do-collect" % ip)


if __name__ == "__main__":
    main()
