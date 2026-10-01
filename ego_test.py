# -*- coding: utf-8 -*-
"""LivUmi-Ego-Std 出厂测试引导 / 记录 / 报告 / 回填 工具

用法：
    python ego_test.py run    --device SN001 --tester 刘鑫亿 --fw 4.0.34 [--ip 192.168.1.50]
    python ego_test.py run    --device SN001 --module Livstudio        # 只跑某模块
    python ego_test.py resume --device SN001                            # 接着上次未完成的跑
    python ego_test.py report --device SN001                            # 生成 HTML 报告
    python ego_test.py export --device SN001                            # 导出 xlsx
    python ego_test.py upload --device SN001                            # 回填飞书表格
    python ego_test.py sync                                             # 从飞书表格拉取最新用例

交互键位（每条用例）：
    P 通过 / F 失败 / B 阻塞 / N 不适用 / S 跳过 / ! 截屏 / Q 退出保存
备注直接回车表示留空。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cases import CASES, FEISHU  # noqa: E402

def _fix_console() -> None:
    """Windows 中文控制台下避免 UnicodeEncodeError / 乱码头。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        try:  # 把控制台代码页切到 UTF-8，解决 GBK 终端乱码
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass


_fix_console()

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "out")
SHOTS = os.path.join(OUT, "shots")

VERDICT = {
    "P": ("通过", "#27500A", "#EAF3DE"),
    "F": ("失败", "#791F1F", "#FCEBEB"),
    "B": ("阻塞", "#633806", "#FAEEDA"),
    "N": ("不适用", "#3C3C3C", "#F1F1EF"),
    "S": ("跳过", "#3C3C3C", "#F1F1EF"),
}
KEYS = "PFBNS!Q"


# ------------------------------------------------------------------ 工具


def case_index() -> dict[str, dict]:
    return {c["id"]: c for c in CASES}


def session_path(device: str) -> str:
    return os.path.join(OUT, f"{device}.json")


def load_session(device: str) -> dict:
    p = session_path(device)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_session(device: str, data: dict) -> str:
    os.makedirs(OUT, exist_ok=True)
    p = session_path(device)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def screenshot(device: str, case_id: str) -> str:
    """调用 PowerShell 截全屏，失败不影响主流程。"""
    os.makedirs(SHOTS, exist_ok=True)
    name = f"{device}_{case_id}_{datetime.now().strftime('%H%M%S')}.png"
    path = os.path.join(SHOTS, name).replace("\\", "/")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
        f"$bmp.Save('{path}');$g.Dispose();$bmp.Dispose()"
    )
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=30)
        return path if os.path.exists(path) else f"(截屏失败 rc={r.returncode})"
    except Exception as exc:  # noqa: BLE001
        return f"(截屏失败 {exc})"


def _safe_input(prompt: str) -> str:
    """stdin 不可用（如 Code Runner 输出面板/管道中断）时返回空串，不抛 EOFError。"""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def pick(prompt: str, allowed: str = KEYS) -> str:
    while True:
        raw = _safe_input(prompt)
        if raw == "":
            print("  [stdin 不可用] 自动判为跳过 S —— 请在 VSCode 集成终端(Ctrl+`)里运行，不要用 Run Code")
            return "S"
        v = raw.strip().upper()
        if v and v[0] in allowed:
            return v[0]
        print(f"  只认 {'/'.join(allowed)}")


# ------------------------------------------------------------------ run


def cmd_run(args) -> None:
    idx = case_index()
    sess = load_session(args.device) if args.resume else {}
    sess.setdefault("device", args.device)
    sess.setdefault("tester", args.tester)
    sess.setdefault("fw", args.fw)
    sess.setdefault("ip", args.ip)
    sess.setdefault("started", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    sess.setdefault("results", {})
    if args.tester:
        sess["tester"] = args.tester
    if args.fw:
        sess["fw"] = args.fw
    if args.ip:
        sess["ip"] = args.ip

    todo = [c for c in CASES
            if (not args.module or c["module"] == args.module)
            and (not args.only or c["id"] in args.only)]
    if not todo:
        print("没有匹配的用例")
        return

    if args.ip and args.open_page:
        webbrowser.open(f"http://{args.ip}")
        print(f"已打开控制台 http://{args.ip}")

    done = 0
    for n, c in enumerate(todo, 1):
        prev = sess["results"].get(c["id"])
        if prev and not args.force:
            print(f"[{n}/{len(todo)}] {c['id']} 已有结果 {prev['verdict']}，跳过（--force 可重跑）")
            continue

        print("\n" + "=" * 68)
        print(f"[{n}/{len(todo)}] {c['id']}  {c['module']} / {c['func']}")
        print(f"  步骤 : {c['steps']}")
        print(f"  标准 : {c['crit']}")
        if c.get("hint"):
            print(f"  要点 : {c['hint']}")
        if c.get("page") and sess.get("ip"):
            print(f"  页面 : http://{sess['ip']}{c['page']}")

        shots = []
        while True:
            v = pick("  判定 [P]通过 [F]失败 [B]阻塞 [N]不适用 [S]跳过 [!]截屏 [Q]退出 > ")
            if v == "!":
                p = screenshot(sess["device"], c["id"])
                shots.append(p)
                print(f"  已存证: {p}")
                continue
            if v == "Q":
                save_session(args.device, sess)
                print(f"\n已保存 -> {session_path(args.device)}")
                return
            break

        note = _safe_input("  备注(可留空) > ").strip()
        if v == "F" and not note:
            note = _safe_input("  [失败必填] 现象描述 > ").strip() or "(未填写)"
        sess["results"][c["id"]] = {
            "verdict": v, "note": note, "shots": shots,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_session(args.device, sess)
        done += 1

    sess["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_session(args.device, sess)
    print(f"\n完成 {done} 条 -> {session_path(args.device)}")
    print_stats(sess)


def print_stats(sess: dict) -> None:
    rs = sess.get("results", {})
    tally = {k: 0 for k in VERDICT}
    for r in rs.values():
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("  " + "  ".join(f"{VERDICT[k][0]}={v}" for k, v in tally.items() if v))
    print(f"  剩余未测: {len(CASES) - len(rs)}")


# ------------------------------------------------------------------ 报告


def cmd_report(args) -> None:
    sess = load_session(args.device)
    if not sess:
        print(f"没有 {args.device} 的记录")
        return
    rs = sess.get("results", {})
    tally = {k: 0 for k in VERDICT}
    for r in rs.values():
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1

    rows = []
    for c in CASES:
        r = rs.get(c["id"])
        if not r:
            v, note, color, bg = "—", "未测", "#777", "#FAFAF8"
        else:
            v = VERDICT[r["verdict"]][0]
            note = html.escape(r.get("note", ""))
            color, bg = VERDICT[r["verdict"]][1], VERDICT[r["verdict"]][2]
        rows.append(
            f"<tr><td style='background:{bg};color:{color};font-weight:500'>{v}</td>"
            f"<td><code>{html.escape(c['id'])}</code></td><td>{html.escape(c['module'])}</td>"
            f"<td>{html.escape(c['func'])}</td><td>{html.escape(c['crit'])}</td>"
            f"<td>{note or '—'}</td></tr>")

    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Ego-Std 测试报告 {html.escape(sess['device'])}</title><style>
body{{font:14px/1.6 system-ui,'Segoe UI',sans-serif;margin:0;padding:28px;color:#222;background:#fff}}
h1{{font-size:18px;font-weight:500;margin:0 0 4px}}
.meta{{color:#666;font-size:12px;margin-bottom:18px}}
.cards{{display:flex;gap:12px;margin:0 0 20px}}
.card{{flex:1;border:1px solid #d8d6cf;border-radius:10px;padding:12px 14px}}
.card b{{display:block;font-size:22px;font-weight:500}}
table{{width:100%;border-collapse:collapse;border:1px solid #d8d6cf;border-radius:10px;overflow:hidden}}
th{{background:#f5f3ee;text-align:left;font-weight:500;padding:9px 10px;border-bottom:1px solid #d8d6cf;font-size:13px}}
td{{padding:9px 10px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px}}
td:first-child{{width:64px}}
code{{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#f3f1ec;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>LivUmi-Ego-Std 出厂测试报告</h1>
<div class="meta">设备 {html.escape(sess['device'])} · 固件 {html.escape(sess.get('fw') or '-')} ·
测试人 {html.escape(sess.get('tester') or '-')} · {html.escape(sess.get('started', '-'))}
· 控制台 {html.escape(sess.get('ip') or '-')}</div>
<div class="cards">
<div class="card"><b>{len(CASES)}</b>用例总数</div>
<div class="card"><b style="color:#27500A">{tally['P']}</b>通过</div>
<div class="card"><b style="color:#A32D2D">{tally['F']}</b>失败</div>
<div class="card"><b style="color:#BA7517">{tally['B']}</b>阻塞</div>
<div class="card"><b style="color:#777">{len(CASES) - len(rs)}</b>未测</div>
</div>
<table><thead><tr><th>结果</th><th>编号</th><th>模块</th><th>功能</th><th>通过标准</th><th>备注</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""
    p = os.path.join(OUT, f"report_{sess['device']}.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"报告 -> {p}")


# ------------------------------------------------------------------ 导出 xlsx


def cmd_export(args) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError:
        print(f"缺少 openpyxl，先跑： {sys.executable} -m pip install openpyxl")
        return

    sess = load_session(args.device)
    if not sess:
        print(f"没有 {args.device} 的记录")
        return
    rs = sess.get("results", {})

    FILL = {k: PatternFill("solid", fgColor=VERDICT[k][2][1:]) for k in VERDICT}
    THIN = Side(style="thin", color="D8D6CF")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    wb = Workbook()
    ws = wb.active
    ws.title = "测试结果"
    head = ["序号", "模块", "测试功能", "测试步骤", "通过标准", "测试结果", "备注"]
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="F5F3EE")
        cell.border = BORDER
        cell.alignment = Alignment(vertical="center")

    for c in CASES:
        r = rs.get(c["id"])
        ws.append([c["id"], c["module"], c["func"], c["steps"], c["crit"],
                   VERDICT[r["verdict"]][0] if r else "", (r or {}).get("note", "")])
        row = ws.max_row
        for cell in ws[row]:
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        if r:
            ws.cell(row=row, column=6).fill = FILL[r["verdict"]]

    for col, w in zip("ABCDEFG", (12, 12, 16, 34, 44, 10, 30)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"

    p = os.path.join(OUT, f"Ego-Std测试结果_{sess['device']}.xlsx")
    wb.save(p)
    print(f"xlsx -> {p}")


# ------------------------------------------------------------------ 回填飞书


def cmd_upload(args) -> None:
    sess = load_session(args.device)
    if not sess:
        print(f"没有 {args.device} 的记录")
        return
    rs = sess.get("results", {})
    s, first = FEISHU["sheet_id"], FEISHU["first_row"]
    last = first + len(CASES) - 1
    rng = f"{s}!{FEISHU['result_col']}{first}:{FEISHU['note_col']}{last}"
    values = [[rs.get(c["id"], {}).get("verdict", ""),
               rs.get(c["id"], {}).get("note", "")] for c in CASES]

    payload = json.dumps({"valueRange": {"range": rng, "values": values}}, ensure_ascii=False)
    cmd = ["lark-cli", "api", "PUT",
           f"/open-apis/sheets/v2/spreadsheets/{FEISHU['sheet_token']}/values",
           "--data", payload]
    if args.dry_run:
        print("将写入范围:", rng)
        print(json.dumps(values, ensure_ascii=False, indent=1))
        return
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    print(r.stdout or r.stderr)
    if r.returncode == 0:
        print(f"已回填 {len(values)} 行 -> {FEISHU['wiki_url']}")


# ------------------------------------------------------------------ 同步用例


def cmd_sync(args) -> None:
    """从飞书表格重新拉取用例文本，更新 cases.py 中的字段（编号集合不变）。"""
    r = subprocess.run(
        ["lark-cli", "api", "GET",
         f"/open-apis/sheets/v2/spreadsheets/{FEISHU['sheet_token']}/values/"
         f"{FEISHU['sheet_id']}!A1:G{first_row_guess()}"],
        capture_output=True, text=True, encoding="utf-8")
    try:
        data = json.loads(r.stdout)
        vals = data["data"]["valueRange"]["values"]
    except Exception:  # noqa: BLE001
        print("拉取失败：", r.stdout[:400] or r.stderr[:400])
        return
    remote = {}
    for row in vals[1:]:
        row = (row + [None] * 7)[:7]
        if row[0]:
            remote[row[0]] = row
    updated = 0
    for c in CASES:
        row = remote.get(c["id"])
        if not row:
            continue
        for key, col in (("module", 1), ("func", 2), ("steps", 3), ("crit", 4)):
            v = (row[col] or "").strip()
            if v and v != c[key]:
                c[key] = v
                updated += 1
    p = os.path.join(BASE, "cases_synced.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(CASES, f, ensure_ascii=False, indent=2)
    print(f"同步完成，变更 {updated} 处 -> {p}（确认后可覆盖 cases.py）")


def first_row_guess() -> int:
    return FEISHU["first_row"] + len(CASES) - 1


# ------------------------------------------------------------------ CLI


def main() -> None:
    ap = argparse.ArgumentParser(description="Ego-Std 出厂测试助手")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--device", "-d", required=True, help="设备编号/SN")

    p = sub.add_parser("run", help="交互式执行测试")
    common(p)
    p.add_argument("--tester", default=os.environ.get("USERNAME", ""))
    p.add_argument("--fw", default="", help="固件版本")
    p.add_argument("--ip", default="", help="控制台 IP，用于打开页面")
    p.add_argument("--module", default="", help="只跑指定模块")
    p.add_argument("--only", default="", help="逗号分隔的用例编号")
    p.add_argument("--resume", action="store_true", help="续跑已有记录")
    p.add_argument("--force", action="store_true", help="重跑已有结果的用例")
    p.add_argument("--open-page", action="store_true", help="开始时自动打开控制台页面")

    p = sub.add_parser("resume", help="续跑")
    common(p)
    p.add_argument("--module", default="")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("report", help="生成 HTML 报告")
    common(p)
    p = sub.add_parser("export", help="导出 xlsx")
    common(p)
    p = sub.add_parser("upload", help="回填飞书表格")
    common(p)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("sync", help="从飞书同步用例文本")

    args = ap.parse_args()
    if args.cmd == "run":
        args.only = [x.strip() for x in args.only.split(",") if x.strip()]
        cmd_run(args)
    elif args.cmd == "resume":
        args.resume = True
        args.only = []
        args.tester = args.fw = args.ip = ""
        args.open_page = False
        cmd_run(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "export":
        cmd_export(args)
    elif args.cmd == "upload":
        cmd_upload(args)
    elif args.cmd == "sync":
        cmd_sync(args)


if __name__ == "__main__":
    main()
