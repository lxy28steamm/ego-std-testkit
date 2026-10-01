# -*- coding: utf-8 -*-
"""盯电池充电曲线，定位"充不到上限"到底卡在哪一环。

数据源：/api/v2/health → host.battery（上位机每 5s 透过 I2C 读 UPS HAT，
percent / voltage_mv / current_ma 均由 UPS 芯片给出，上位机只做 clamp 后透传）。

用法：
    python battery_watch.py --host 192.168.195.38                  # 一直盯，Ctrl+C 停
    python battery_watch.py --host 192.168.195.38 --minutes 60     # 盯 1 小时
    python battery_watch.py --host 192.168.195.38 --minutes 480 --csv out/batt.csv
                                                                   # 充一晚上，存 CSV

结束后会给出判定，四种结论见 VERDICT_* 常量说明。
零依赖，只用标准库。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# 判定阈值
STALL_MINUTES = 20.0     # percent 连续多久不变 = 判定停滞
TAIL_CURRENT_MA = 150.0  # |电流| 低于它 = 已进入涓流/截止
FULL_PERCENT = 99        # 达到它视为"满"
MIN_POLL_SEC = 6.0       # 后端 battery 轮询间隔是 5s，采样别比它快


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


def read_battery(host: str, port: int = 18000, timeout: float = 10.0) -> dict | None:
    url = f"http://{host}:{port}/api/v2/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    host_info = data.get("host") or {}
    batt = host_info.get("battery")
    if not isinstance(batt, dict):
        return None
    batt = dict(batt)
    batt["_cpu_percent"] = host_info.get("cpu_average_percent")
    batt["_temperature_c"] = host_info.get("temperature_c")
    return batt


def fmt_row(ts: str, b: dict) -> str:
    pct = b.get("percent")
    status = b.get("status") or "-"
    volt = b.get("voltage_mv")
    cur = b.get("current_ma")
    temp = b.get("_temperature_c")
    v = f"{volt/1000:.2f}V" if isinstance(volt, int) else "-"
    c = f"{cur:+d}mA" if isinstance(cur, int) else "-"
    t = f"{temp:.1f}C" if isinstance(temp, (int, float)) else "-"
    p = f"{pct}%" if isinstance(pct, int) else "--%"
    flag = ""
    if isinstance(cur, int) and cur < 0:
        flag = "  < 净放电"
    return f"{ts}  {p:>5}  {status:<14} {v:>8}  {c:>9}  {t:>7}{flag}"


def analyse(samples: list[tuple[float, dict]]) -> None:
    print()
    print("=" * 78)
    print("充电过程判定")
    print("=" * 78)
    if len(samples) < 3:
        print("样本太少，无法判定（至少采 3 次）")
        return

    t0, b0 = samples[0]
    tn, bn = samples[-1]
    pcts = [b.get("percent") for _, b in samples if isinstance(b.get("percent"), int)]
    volts = [b.get("voltage_mv") for _, b in samples if isinstance(b.get("voltage_mv"), int)]
    curs = [b.get("current_ma") for _, b in samples if isinstance(b.get("current_ma"), int)]

    p0, pn = pcts[0], pcts[-1]
    peak = max(pcts)
    dur_min = (tn - t0) / 60.0
    print(f"时长        : {dur_min:.1f} 分钟（{len(samples)} 个样本）")
    print(f"电量        : {p0}% -> {pn}%   峰值 {peak}%")
    if volts:
        print(f"电压        : {volts[0]/1000:.2f}V -> {volts[-1]/1000:.2f}V   峰值 {max(volts)/1000:.2f}V")
    if curs:
        print(f"电流        : 最大充电 {max(curs)}mA  最小 {min(curs)}mA  末值 {curs[-1]}mA")
        neg = sum(1 for c in curs if c < 0)
        if neg:
            print(f"             其中 {neg}/{len(curs)} 次为负（净放电）")

    # 停滞检测
    stall_sec = None
    last_change_t = t0
    last_pct = pcts[0]
    for t, b in samples:
        p = b.get("percent")
        if isinstance(p, int) and p != last_pct:
            last_pct = p
            last_change_t = t
    stall_sec = tn - last_change_t
    print(f"电量停滞    : 最后一次变化在 {stall_sec/60:.1f} 分钟前")

    print()
    last = bn
    cur_n = last.get("current_ma")
    status_n = last.get("status")
    charging_n = last.get("charging")

    if peak >= FULL_PERCENT:
        print("结论        : [PASS] 充到过 %d%%，充电链路正常。" % peak)
        print("             若界面仍显示不满，那是 UPS 电量计显示滞后，非充电故障。")
        return

    if stall_sec >= STALL_MINUTES * 60:
        if isinstance(cur_n, int) and abs(cur_n) < TAIL_CURRENT_MA:
            print("结论        : [电量计未校准] 电流已降到 %dmA（接近截止），但电量停在 %d%%。" % (cur_n, pn))
            print("             芯片认为已充满、电量计读数却不认 —— 需要一次完整充放电循环校准，")
            print("             或 UPS 芯片的满电容量参数与实际电池不匹配。找开发要校准方法。")
        else:
            print("结论        : [充不进去] 持续 %d 分钟电量不涨，电流仍有 %smA。" % (
                stall_sec / 60, cur_n))
            print("             电量被消耗抵消掉了 —— 查充电器功率/线材，或在充电时不要跑采集。")
        return

    if isinstance(cur_n, int) and cur_n < 0 and pn < FULL_PERCENT:
        print("结论        : [净放电] 插着充电器却在放电（电流 %dmA）。" % cur_n)
        print("             充电器功率 < 设备整机功耗，先换更大功率充电头/短线再测。")
        return

    rate = (pn - p0) / dur_min if dur_min > 0 else 0
    print("结论        : [仍在充电] 速率约 %.1f%%/分钟，按此速率到 100%% 还需约 %.0f 分钟。" % (
        rate, max(0.0, (FULL_PERCENT - pn) / rate) if rate > 0 else 0))
    print("             继续观察即可；若长时间维持此低速率，按上面'充不进去'处理。")
    if charging_n:
        print("             当前状态: %s" % status_n)


def main() -> int:
    ap = argparse.ArgumentParser(description="盯电池充电曲线")
    ap.add_argument("--host", default=None,
                    help="设备 IP；省略则读 device_ip.txt，没有就自动扫描本机网段")
    ap.add_argument("--port", type=int, default=18000)
    ap.add_argument("--interval", type=float, default=30.0, help="采样间隔秒（不小于 6）")
    ap.add_argument("--minutes", type=float, default=0, help="运行时长分钟，0=一直跑")
    ap.add_argument("--csv", default="", help="同时写入 CSV 的路径")
    args = ap.parse_args()
    import devip
    args.host = devip.resolve_host(args.host)

    interval = max(MIN_POLL_SEC, args.interval)
    deadline = time.time() + args.minutes * 60 if args.minutes > 0 else None

    csv_f = None
    csv_w = None
    if args.csv:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)), exist_ok=True)
        csv_f = open(args.csv, "w", newline="", encoding="utf-8")
        csv_w = csv.writer(csv_f)
        csv_w.writerow(["time", "percent", "status", "charging",
                        "voltage_mv", "current_ma", "temperature_c", "cpu_percent"])

    print("=" * 78)
    print(f"电池监控  设备 {args.host}:{args.port}   间隔 {interval:.0f}s"
          + (f"   时长 {args.minutes:.0f} 分钟" if args.minutes else "   直到 Ctrl+C"))
    print("=" * 78)
    print("时间      电量    状态           电压      电流      温度")
    print("-" * 78)

    samples: list[tuple[float, dict]] = []
    try:
        while deadline is None or time.time() < deadline:
            b = read_battery(args.host, args.port)
            now = time.time()
            ts = datetime.now().strftime("%H:%M:%S")
            if b is None:
                print(f"{ts}  <读取失败>")
            else:
                samples.append((now, b))
                print(fmt_row(ts, b))
                if csv_w:
                    csv_w.writerow([
                        datetime.now().isoformat(timespec="seconds"),
                        b.get("percent"), b.get("status"), b.get("charging"),
                        b.get("voltage_mv"), b.get("current_ma"),
                        b.get("_temperature_c"), b.get("_cpu_percent"),
                    ])
                    csv_f.flush()
            if deadline is None or time.time() + interval < deadline:
                time.sleep(interval)
            else:
                break
    except KeyboardInterrupt:
        print("\n(已中断)")

    if csv_f:
        csv_f.close()
        print(f"\nCSV 已存: {os.path.abspath(args.csv)}")

    analyse(samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
