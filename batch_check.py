# -*- coding: utf-8 -*-
"""Ego-Std 批量巡检（17 台设备一次性过一遍）

单台用 ego_api_test.py，批量用这个：把同一套 18 项检查并发打到多台设备上，
最后汇总成一张总表（IP / 结论 / 版本 / 相机 / PASS / FAIL / WARN / 耗时），
不合格的一眼挑出来，交付现场直接拿这张表。

用法：
    python batch_check.py                                  # 读 device_ip.txt
    python batch_check.py --discover                       # 自动扫本机网段全部设备
    python batch_check.py --file ips.txt                   # 文件里每行一个 IP
    python batch_check.py --ip 192.168.195.38,192.168.195.66
    python batch_check.py --discover --workers 8 --xlsx out/batch.xlsx

设计要点（踩过的坑都在这）：
  1. 每台设备跑在**独立子进程**里，不用线程内直接调 ego_api_test。
     原因：ego_api_test 的 --quiet 用 contextlib.redirect_stdout 捕获日志，
     那是**进程级全局状态、非线程安全**，并发起来会互相串台。
     子进程同时还解决「一台卡死拖垮整批」和「一台崩了整批全崩」。
  2. 单台超时用 kill 而非等它自己结束，默认 180s（18 项里有几个 8s 超时接口）。
  3. 扫描是全量的、选用是全量的 —— devip.discover() 返回列表，这里不再只取第一台。
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE_DIR, "out")
DEVICE_FILE = os.path.join(BASE_DIR, "device_ip.txt")
ENGINE = os.path.join(BASE_DIR, "ego_api_test.py")

sys.path.insert(0, BASE_DIR)


def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ 取设备列表


def read_ip_file(path: str) -> list[str]:
    """读 IP 清单。# 开头是注释，空行忽略，第一行也允许是注释。"""
    out: list[str] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.split("#", 1)[0].strip()
            if s:
                out.append(s)
    return out


def collect_ips(ip_arg: str = "", file_arg: str = "", discover: bool = False,
                verbose: bool = True) -> list[str]:
    """按 --ip / --file / --discover / device_ip.txt 的优先级收集设备 IP（去重、保序）。"""
    ips: list[str] = []

    def add(s: str) -> None:
        s = s.strip()
        if s and s not in ips:
            ips.append(s)

    for s in re_split_commas(ip_arg):
        add(s)
    for s in (read_ip_file(file_arg) if file_arg else []):
        add(s)
    if discover:
        import devip
        devices, _, _, nets = devip.discover()
        if verbose:
            print(f"扫描网段 {nets}  发现 {len(devices)} 台 ego 设备")
        for d in devices:
            add(d["ip"])
    if not ips:
        # 都没指定就退回 device_ip.txt。注意 read_saved_host() 只取第一行，
        # 批量场景要全部，所以这里直接读整个文件。
        for s in read_ip_file(DEVICE_FILE):
            add(s)
        if verbose and ips:
            print(f"未指定设备，读取 {os.path.basename(DEVICE_FILE)}：{', '.join(ips)}")
    return ips


def re_split_commas(s: str) -> list[str]:
    """支持 --ip "a,b" 和 --ip a --ip b 两种写法（argparse 里逗号分隔最省事）。"""
    if not s:
        return []
    return [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]


# ------------------------------------------------------------------ 单台执行


def run_one(ip: str, port: int = 18000, timeout: int = 180, do_collect: bool = False,
            model: str = "", device_id: str = "", python_exe: str = "") -> dict:
    """在一台设备上跑完整体检，返回一条扁平记录。永不抛异常。

    device_id 传空串 = 跳过 DEV-ONLINE 门禁。批量场景必须这样：不同批次设备的
    逻辑 id 并不一样（现场见到 ego-std-235，现场也有 ego-lite-01），
    写死一个会让每台都因「设备清单中无此项」判 FAIL。
    """
    exe = python_exe or sys.executable
    cmd = [exe, ENGINE, "--host", ip, "--port", str(port),
           "--json-only", "--quiet", "--no-manual", "--exit-code",
           "--device-id", device_id]      # 空串也要显式传，否则 ego_api_test 会用自己的默认值
    if do_collect:
        cmd.append("--do-collect")
    if model:
        cmd += ["--model", model]

    t0 = time.time()
    rec = {"ip": ip, "verdict": "FAIL", "PASS": 0, "FAIL": 1, "WARN": 0, "SKIP": 0,
           "total": 0, "secs": 0.0, "state": "", "profile": "", "cameras": "",
           "version": "", "items": [], "log": "", "error": ""}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           cwd=BASE_DIR)
    except subprocess.TimeoutExpired:
        rec["error"] = f"超时（>{timeout}s），已终止"
        rec["secs"] = round(time.time() - t0, 1)
        return rec
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["secs"] = round(time.time() - t0, 1)
        return rec

    rec["secs"] = round(time.time() - t0, 1)
    out = (p.stdout or "").strip()
    if not out:
        rec["error"] = ("无输出（进程异常退出 rc=%s）: %s"
                        % (p.returncode, (p.stderr or "").strip()[:200]))
        return rec
    try:
        d = json.loads(out)
    except json.JSONDecodeError as exc:
        rec["error"] = f"输出不是合法 JSON（{exc}）: {out[:200]}"
        return rec

    s = d.get("summary") or {}
    m = d.get("meta") or {}
    rec.update({
        "verdict": s.get("verdict", "FAIL"),
        "PASS": s.get("PASS", 0), "FAIL": s.get("FAIL", 0),
        "WARN": s.get("WARN", 0), "SKIP": s.get("SKIP", 0),
        "total": s.get("total", 0),
        "state": m.get("state", ""), "profile": m.get("profile", ""),
        "cameras": m.get("cameras", ""), "version": m.get("version", ""),
        "items": d.get("items", []), "log": d.get("log", ""),
    })
    if p.returncode not in (0, 1):
        rec["error"] = f"退出码 {p.returncode}: {(p.stderr or '').strip()[:200]}"
    return rec


def run_batch(ips: list[str], workers: int = 5, timeout: int = 180,
              do_collect: bool = False, model: str = "",
              device_id: str = "", port: int = 18000,
              on_progress=None) -> list[dict]:
    """并发跑一批设备。on_progress(done, total, rec) 用于 GUI 实时刷新。"""
    if not ips:
        return []
    recs: list[dict] = []
    total = len(ips)
    # workers 至少 1，最多不超过设备数 —— 17 台机器并发 17 也没意义，还容易把网络打满
    n = max(1, min(int(workers), total))
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = {pool.submit(run_one, ip, port, timeout, do_collect, model,
                            device_id): ip for ip in ips}
        done = 0
        for fut in as_completed(futs):
            ip = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001  兜底：子进程都杀了还炸说明有 bug
                rec = {"ip": ip, "verdict": "FAIL", "PASS": 0, "FAIL": 1, "WARN": 0,
                       "SKIP": 0, "total": 0, "secs": 0.0, "state": "", "profile": "",
                       "cameras": "", "version": "", "items": [], "log": "",
                       "error": f"{type(exc).__name__}: {exc}"}
            recs.append(rec)
            done += 1
            if on_progress:
                try:
                    on_progress(done, total, rec)
                except Exception:  # noqa: BLE001  回调异常不许影响采集
                    pass
    # 结果按传入的 IP 顺序排，方便和设备清单对照
    order = {ip: i for i, ip in enumerate(ips)}
    recs.sort(key=lambda r: order.get(r["ip"], 9999))
    return recs


# ------------------------------------------------------------------ 汇总 / 导出


def summarize_batch(recs: list[dict]) -> dict:
    ok = sum(1 for r in recs if r["verdict"] == "PASS")
    warn = sum(1 for r in recs if r["verdict"] == "WARN")
    bad = sum(1 for r in recs if r["verdict"] == "FAIL")
    return {"total": len(recs), "PASS": ok, "WARN": warn, "FAIL": bad,
            "verdict": "FAIL" if bad else ("WARN" if warn else "PASS")}


COLS = [("ip", "IP", 130), ("verdict", "结论", 70), ("state", "设备状态", 100),
        ("cameras", "相机", 55), ("profile", "Profile", 100),
        ("PASS", "PASS", 55), ("FAIL", "FAIL", 55), ("WARN", "WARN", 55),
        ("secs", "耗时(s)", 75), ("version", "版本", 90), ("error", "错误", 220)]


def _cell(rec: dict, key: str) -> str:
    v = rec.get(key, "")
    if v is None:
        return ""
    return str(v)


def export_html(recs: list[dict], path: str, s: dict) -> str:
    def row(rec: dict) -> str:
        cls = {"PASS": "ok", "WARN": "warn", "FAIL": "bad"}.get(rec["verdict"], "")
        tds = "".join(f"<td>{html.escape(_cell(rec, k))}</td>" for k, _, _ in COLS)
        return f'<tr class="{cls}">{tds}</tr>'

    # 每台的失败项明细，展开可看
    details = []
    for rec in recs:
        bad = [i for i in rec.get("items", []) if i.get("status") in ("FAIL", "WARN")]
        if not bad and not rec.get("error"):
            continue
        li = "".join(
            f'<li><span class="tag {html.escape(i["status"])}">{i["status"]}</span>'
            f'<code>{html.escape(i["id"])}</code> {html.escape(i["name"])}'
            f'<span class="d">{html.escape(str(i["detail"])[:200])}</span></li>'
            for i in bad)
        if rec.get("error"):
            li += f'<li><span class="tag bad">ERROR</span><span class="d">{html.escape(rec["error"])}</span></li>'
        details.append(
            f'<details><summary>{html.escape(rec["ip"])} — {len(bad)} 项待看</summary><ul>{li}</ul></details>')

    doc = f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>Ego-Std 批量巡检 {s['total']} 台</title>
<style>
 body{{font:14px/1.6 "Microsoft YaHei",sans-serif;margin:24px;color:#1f2430;background:#f7f8fa}}
 h1{{font-size:20px;margin:0 0 4px}}
 .meta{{color:#6b7280;font-size:12px;margin-bottom:16px}}
 .cards{{display:flex;gap:12px;margin-bottom:18px}}
 .card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 20px;min-width:96px}}
 .card b{{display:block;font-size:24px;line-height:1.2}}
 .card span{{font-size:12px;color:#6b7280}}
 .ok{{background:#eefaf0}} .ok b{{color:#15803d}}
 .warn{{background:#fff8ec}} .warn b{{color:#b45309}}
 .bad{{background:#fef0f0}} .bad b{{color:#b91c1c}}
 table{{border-collapse:collapse;width:100%;background:#fff;border-radius:10px;overflow:hidden}}
 th,td{{padding:7px 10px;border-bottom:1px solid #eef0f3;text-align:left;font-size:13px}}
 th{{background:#f0f2f5;font-weight:600;position:sticky;top:0}}
 tr.ok td:first-child{{border-left:3px solid #16a34a}}
 tr.warn td:first-child{{border-left:3px solid #f59e0b}}
 tr.bad td:first-child{{border-left:3px solid #dc2626}}
 tr.bad{{background:#fffafa}}
 details{{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:8px 12px;margin:6px 0}}
 summary{{cursor:pointer;font-weight:600}}
 ul{{margin:8px 0 4px;padding-left:18px}}
 li{{margin:3px 0}}
 code{{background:#f0f2f5;padding:1px 5px;border-radius:4px;margin-right:6px;font-size:12px}}
 .tag{{display:inline-block;min-width:44px;text-align:center;padding:1px 5px;border-radius:4px;
   font-size:11px;margin-right:6px;color:#fff;background:#9ca3af}}
 .tag.PASS{{background:#16a34a}} .tag.WARN{{background:#f59e0b}} .tag.FAIL{{background:#dc2626}}
 .d{{color:#6b7280;font-size:12px}}
</style>
<h1>Ego-Std 批量巡检报告</h1>
<div class="meta">生成时间 {html.escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}
 · 共 {s['total']} 台 · 并发只读体检 · 不启停采集</div>
<div class="cards">
 <div class="card"><b>{s['total']}</b><span>设备总数</span></div>
 <div class="card ok"><b>{s['PASS']}</b><span>合格 PASS</span></div>
 <div class="card warn"><b>{s['WARN']}</b><span>有告警 WARN</span></div>
 <div class="card bad"><b>{s['FAIL']}</b><span>不合格 FAIL</span></div>
</div>
<table><thead><tr>{''.join(f'<th>{t}</th>' for _, t, _ in COLS)}</tr></thead>
<tbody>{''.join(row(r) for r in recs)}</tbody></table>
<h2>待看明细</h2>
{''.join(details) if details else '<p style="color:#15803d">全部设备 18 项全通过，无待看项。</p>'}
</html>"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def export_csv(recs: list[dict], path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([t for _, t, _ in COLS])
        for r in recs:
            w.writerow([_cell(r, k) for k, _, _ in COLS])
        w.writerow([])
        w.writerow(["明细", "状态", "ID", "检查项", "详情"])
        for r in recs:
            for i in r.get("items", []):
                if i.get("status") in ("FAIL", "WARN"):
                    w.writerow([r["ip"], i["status"], i["id"], i["name"], i["detail"]])
    return path


def export_xlsx(recs: list[dict], path: str, s: dict) -> str:
    """openpyxl 不在环境里就静默跳过 —— 报告导不出不该让整批巡检失败。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return ""
    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    head_font = Font(color="FFFFFF", bold=True)
    ws.append(["Ego-Std 批量巡检汇总", "", "", "", ""])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"生成时间 {datetime.now():%Y-%m-%d %H:%M:%S}  共 {s['total']} 台"
               f"  合格 {s['PASS']}  告警 {s['WARN']}  不合格 {s['FAIL']}"])
    ws.append([])
    ws.append([t for _, t, _ in COLS])
    for c in range(1, len(COLS) + 1):
        cell = ws.cell(row=4, column=c)
        cell.fill, cell.font = head_fill, head_font
        cell.alignment = Alignment(horizontal="center")
    vfill = {"PASS": PatternFill("solid", fgColor="C6EFCE"),
             "WARN": PatternFill("solid", fgColor="FFEB9C"),
             "FAIL": PatternFill("solid", fgColor="FFC7CE")}
    for r in recs:
        ws.append([_cell(r, k) for k, _, _ in COLS])
        row_i = ws.max_row
        v = vfill.get(r["verdict"])
        if v:
            ws.cell(row=row_i, column=2).fill = v
        for c in range(1, len(COLS) + 1):
            ws.cell(row=row_i, column=c).border = None
    for i, (_, _, w) in enumerate(COLS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w / 7.2
    ws.freeze_panes = "A5"

    ws2 = wb.create_sheet("失败明细")
    ws2.append(["IP", "状态", "ID", "检查项", "详情", "来源接口"])
    for c in range(1, 7):
        ws2.cell(row=1, column=c).fill = head_fill
        ws2.cell(row=1, column=c).font = head_font
    for r in recs:
        for i in r.get("items", []):
            if i.get("status") in ("FAIL", "WARN"):
                ws2.append([r["ip"], i["status"], i["id"], i["name"],
                            str(i["detail"])[:500], i.get("src", "")])
    for i, w in enumerate([16, 8, 12, 16, 90, 34], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"

    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)
    return path


# ------------------------------------------------------------------ 命令行


def _w(s: str) -> int:
    """字符串在等宽终端里的显示宽度：中文/全角算 2 列。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, n: int, right: bool = False) -> str:
    """按显示宽度对齐（ljust 会把中文排歪）。"""
    s = "" if s is None else str(s)
    gap = max(0, n - _w(s))
    return s + " " * gap if right else " " * gap + s


def _print_table(recs: list[dict], s: dict) -> None:
    head = (f"{_pad('IP', 17)}{_pad('结论', 8)}{_pad('状态', 12)}{_pad('相机', 6)}"
            f"{_pad('PASS', 5, True)}{_pad('FAIL', 5, True)}{_pad('WARN', 5, True)}"
            f"{_pad('秒', 8, True)}  备注")
    print("\n" + "=" * 100)
    print(head)
    print("-" * 100)
    for r in recs:
        note = r.get("error") or ""
        if not note:
            bad = [f"{i['id']}" for i in r.get("items", []) if i.get("status") == "FAIL"]
            note = ("失败:" + ",".join(bad[:4])) if bad else ""
        print(f"{_pad(r['ip'], 17)}{_pad(r['verdict'], 8)}"
              f"{_pad(str(r.get('state', ''))[:10], 12)}{_pad(str(r.get('cameras', '')), 6)}"
              f"{_pad(r['PASS'], 5, True)}{_pad(r['FAIL'], 5, True)}{_pad(r['WARN'], 5, True)}"
              f"{_pad(r['secs'], 8, True)}  {note[:40]}")
    print("=" * 100)
    print(f"共 {s['total']} 台：合格 {s['PASS']} / 告警 {s['WARN']} / 不合格 {s['FAIL']}")


def main() -> None:
    _fix_console()
    ap = argparse.ArgumentParser(description="Ego-Std 批量巡检（多台设备并发体检）")
    ap.add_argument("--ip", default="", help="逗号分隔的 IP 列表")
    ap.add_argument("--file", default="", help="IP 清单文件（每行一个，# 注释）")
    ap.add_argument("--discover", action="store_true", help="自动扫描本机网段全部 ego 设备")
    ap.add_argument("--workers", type=int, default=5, help="并发数（默认 5）")
    ap.add_argument("--timeout", type=int, default=180, help="单台超时秒数（默认 180）")
    ap.add_argument("--port", type=int, default=18000)
    ap.add_argument("--model", default="", choices=["", "233", "235"])
    ap.add_argument("--device-id", default="",
                    help="目标逻辑设备 id 做门禁检查；留空跳过（批量推荐，"
                         "因为不同批次设备 id 不同：ego-std-235 / ego-lite-01 …）")
    ap.add_argument("--do-collect", action="store_true", help="实测采集启停（会写文件）")
    ap.add_argument("--html", default="", help="HTML 报告输出路径")
    ap.add_argument("--xlsx", default="", help="Excel 报告输出路径（需 openpyxl）")
    ap.add_argument("--csv", default="", help="CSV 输出路径")
    ap.add_argument("--json", default="", help="原始结果 JSON 输出路径")
    ap.add_argument("--no-report", action="store_true", help="不导出任何报告文件")
    args = ap.parse_args()

    ips = collect_ips(args.ip, args.file, args.discover)
    if not ips:
        print("没有可测设备。用 --ip / --file / --discover 指定，或先跑一次自动查找。")
        sys.exit(2)

    print(f"批量巡检 {len(ips)} 台，并发 {min(args.workers, len(ips))}，"
          f"单台超时 {args.timeout}s，{'含实测采集' if args.do_collect else '只读不启停'}")
    t0 = time.time()

    def prog(done: int, total: int, rec: dict) -> None:
        flag = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL"}[rec["verdict"]]
        print(f"  [{done}/{total}] {flag} {rec['ip']}  {rec['secs']}s  "
              f"P{rec['PASS']}/F{rec['FAIL']}/W{rec['WARN']}"
              + (f"  {rec['error'][:60]}" if rec.get("error") else ""))

    recs = run_batch(ips, args.workers, args.timeout, args.do_collect, args.model,
                     args.device_id, args.port, on_progress=prog)
    s = summarize_batch(recs)
    _print_table(recs, s)
    print(f"总耗时 {time.time() - t0:.1f}s")

    if not args.no_report:
        os.makedirs(OUT, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        paths = []
        paths.append(export_html(recs, args.html or os.path.join(OUT, f"batch_{ts}.html"), s))
        paths.append(export_csv(recs, args.csv or os.path.join(OUT, f"batch_{ts}.csv")))
        if args.xlsx:
            p = export_xlsx(recs, args.xlsx, s)
            if p:
                paths.append(p)
            else:
                print("提示：未安装 openpyxl，跳过 Excel（pip install openpyxl 可启用）")
        if args.json:
            paths.append(args.json)
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"at": datetime.now().isoformat(timespec="seconds"),
                           "summary": s, "results": recs}, f, ensure_ascii=False, indent=2)
        for p in paths:
            print(f"报告 -> {p}")

    sys.exit(1 if s["FAIL"] else 0)


if __name__ == "__main__":
    main()
