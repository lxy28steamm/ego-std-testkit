# -*- coding: utf-8 -*-
"""实时盯 IMU / 队列，抓"队列溢出"这类一闪而过的瞬时故障。

用法：
    python imu_watch.py --host 192.168.199.66                # 一直盯，Ctrl+C 停
    python imu_watch.py --host 192.168.199.66 --seconds 60   # 只盯 60 秒
    python imu_watch.py --host 192.168.199.66 --interval 0.2 # 每 0.2s 采一次（更密）

零依赖，只用标准库。判定口径见 VERDICT 常量。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime


def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass


_fix_console()

# 判定阈值
WARN_PCT = 50.0    # 水位占比超过它 = 预警
FAIL_PCT = 90.0    # 水位占比超过它 = 判溢出
GAP_MS_IMU = 50.0  # IMU 最大间隔超过它 = 判卡顿（200Hz 正常应约 5ms）


def probe(base: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(base + "/api/v2/session", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}"}


def fmt_num(n) -> str:
    return f"{n:>7}" if isinstance(n, int) else f"{str(n):>7}"


def main() -> None:
    ap = argparse.ArgumentParser(description="实时监控 ego 设备 IMU 与队列水位")
    ap.add_argument("--host", default=None,
                    help="设备 IP；省略则读 device_ip.txt，没有就自动扫描本机网段")
    ap.add_argument("--port", type=int, default=18000)
    ap.add_argument("--interval", type=float, default=1.0, help="采样间隔秒（默认 1s）")
    ap.add_argument("--seconds", type=float, default=0, help="盯多久，0=一直盯到 Ctrl+C")
    ap.add_argument("--imu-topic", default="", help="IMU topic 关键字，默认自动匹配含 imu 的")
    args = ap.parse_args()
    import devip
    args.host = devip.resolve_host(args.host)

    base = f"http://{args.host}:{args.port}"
    print(f"盯 {base}  间隔 {args.interval}s"
          + (f"  时长 {args.seconds:.0f}s" if args.seconds else "  (Ctrl+C 停止)"))
    print("-" * 96)
    print(f"{'时间':<10}{'状态':<10}{'当前水位':>10}{'峰值水位':>12}{'占比':>8}"
          f"{'drop':>7}  {'IMU条数':>9}{'丢帧':>6}{'频率':>10}{'间隔ms':>9}")
    print("-" * 96)

    peak = 0
    cap = 0
    drop_first = None
    drop_last = 0
    imu_cnt_last = 0
    max_gap_ms = 0.0
    was_recording = False
    t0 = time.time()
    n = 0

    try:
        while True:
            s = probe(base)
            if "_error" in s:
                print(f"{datetime.now():%H:%M:%S}  读取失败: {s['_error']}")
            else:
                r = s.get("recorder") or {}
                state = r.get("state") or s.get("state") or "-"
                cap = r.get("queue_capacity") or cap
                dep = r.get("queue_depth") or 0
                hwm = r.get("queue_high_watermark") or 0
                drop = r.get("drop_count") or 0
                peak = max(peak, hwm)
                if drop_first is None:
                    drop_first = drop
                drop_last = drop
                if state in ("recording", "running"):
                    was_recording = True

                # 找 IMU 流
                imu = None
                for topic, v in (r.get("streams") or {}).items():
                    key = args.imu_topic or "imu"
                    if key.lower() in topic.lower():
                        imu = v
                        break
                if imu:
                    cnt = imu.get("message_count") or 0
                    miss = imu.get("missing_count") or 0
                    hz = imu.get("actual_rate_hz") or 0.0
                    gap = (imu.get("max_gap_ns") or 0) / 1e6
                    max_gap_ms = max(max_gap_ms, gap)
                    imu_cnt_last = cnt
                else:
                    cnt = miss = 0
                    hz = 0.0
                    gap = 0.0

                pct = (hwm / cap * 100) if cap else 0.0
                mark = ""
                if pct >= FAIL_PCT or drop > 0 or miss > 0 or gap > GAP_MS_IMU:
                    mark = "   <<< 异常"
                elif pct >= WARN_PCT:
                    mark = "   <<< 偏高"
                print(f"{datetime.now():%H:%M:%S}  {state:<10}"
                      f"{dep:>6}/{cap:<4}{hwm:>8}"
                      f"{pct:>7.2f}%{drop:>7}{cnt:>9}{miss:>6}{hz:>10.3f}{gap:>9.1f}{mark}")

            n += 1
            if args.seconds and (time.time() - t0) >= args.seconds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    print("-" * 96)
    pct = (peak / cap * 100) if cap else 0.0
    print(f"采样次数 {n}   队列容量 {cap}")
    print(f"峰值水位 {peak} ({pct:.2f}%)   drop_count {drop_first} -> {drop_last}")
    print(f"IMU 最终条数 {imu_cnt_last}   观测到的最大间隔 {max_gap_ms:.1f}ms")
    if not was_recording:
        print("注意：全程未见到 recording 状态，上面的数字可能是【上一次采集的残留】，")
        print("      要抓实时溢出请先在设备上开始采集，再跑本脚本。")

    if drop_last > (drop_first or 0):
        print("结论：FAIL —— 采集期间有数据被丢弃（drop_count 上涨）。")
    elif pct >= FAIL_PCT:
        print(f"结论：FAIL —— 队列峰值 {pct:.1f}% ≥ {FAIL_PCT}%，发生溢出。")
    elif pct >= WARN_PCT:
        print(f"结论：WARN —— 队列峰值 {pct:.1f}% ≥ {WARN_PCT}%，余量不足。")
    elif max_gap_ms > GAP_MS_IMU:
        print(f"结论：WARN —— IMU 最大间隔 {max_gap_ms:.1f}ms > {GAP_MS_IMU}ms，有卡顿。")
    elif not was_recording:
        print("结论：无有效采集过程，无法判定。")
    else:
        print("结论：PASS —— 队列水位正常，无丢弃，IMU 无掉帧。")


if __name__ == "__main__":
    main()
