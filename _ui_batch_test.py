# -*- coding: utf-8 -*-
"""批量巡检页的端到端自测：真起界面、真点开始、等跑完、验结果表和导出。

不是渲染截图那种「画得出来就算过」——这里会真的并发跑设备，
所以只有在设备在线时才有意义。退出码 0 表示通过。

用法： .venv-gui/Scripts/python.exe _ui_batch_test.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QEventLoop, QTimer, QPoint
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QPixmap

import app as A

OUT = A.EXPORT_DIR / "_ui_batch"
fails = []


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def wait_until(pred, timeout: float, tick: int = 300) -> bool:
    """跑事件循环直到 pred() 为真或超时。比 sleep 靠谱，不会卡住界面线程。"""
    loop = QEventLoop()
    box = {"ok": False}

    def probe() -> None:
        if pred():
            box["ok"] = True
            loop.quit()

    t = QTimer()
    t.timeout.connect(probe)
    t.start(tick)
    QTimer.singleShot(int(timeout * 1000), loop.quit)
    loop.exec()
    t.stop()
    return box["ok"]


def main() -> None:
    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setStyle("Fusion")
    qapp.setFont(QFont("Microsoft YaHei UI", 9))

    win = A.MainWindow()
    A.WINDOW = win
    win.resize(1180, 820)
    win.show()
    qapp.processEvents()

    # 定位批量巡检页（插在第 2 位）
    win.nav.setCurrentRow(1)
    qapp.processEvents()
    p = win.stack.currentWidget()
    print("\n=== 批量巡检页端到端自测 ===")
    check(isinstance(p, A.BatchPanel), f"第 2 页是 BatchPanel（实际 {type(p).__name__}）")
    check(p.title == "② 批量巡检", f"标题正确：{p.title}")

    # 等自动扫描完成
    ok = wait_until(lambda: p.list.count() > 0, timeout=40)
    n = p.list.count()
    check(ok and n > 0, f"自动扫描到设备：{n} 台")
    if n == 0:
        print("没有设备在线，跳过实测部分。")
        return

    check(p._checked_ips() == [p.list.item(i).text() for i in range(n)],
          "默认全勾选")

    # --- 各「设备来源」解析 ---
    src_idx = {p.src.itemData(i): i for i in range(p.src.count())}
    check(set(src_idx) >= {"discover", "range", "net", "file", "manual"},
          f"来源下拉含全部选项：{sorted(src_idx)}")

    # 自动扫描：有清单时直接用清单
    p.src.setCurrentIndex(src_idx["discover"])
    ips, err = p._resolve_src()
    check(not err and ips == [], "自动扫描模式：有清单时沿用清单，不重复扫")

    # IP 范围：取一台真实在线设备的所在段
    online = p._checked_ips()
    if online:
        target = online[0]
        o = target.rsplit(".", 1)
        p.src.setCurrentIndex(src_idx["range"])
        p.path.setText(f"{o[0]}.{o[1]}-{int(o[1]) + 2}")
        ips, err = p._resolve_src()
        check(not err and target in ips,
              f"IP 范围 {o[0]}.{o[1]}-{int(o[1]) + 2} 解析出 {len(ips)} 台，含 {target}")

    # IP 范围：格式错误要给出提示而不是崩
    p.path.setText("192.168.195")
    ips, err = p._resolve_src()
    check(bool(err) and not ips, f"范围格式错误被拦下：{err[:40]}")

    # IP 范围：探不到设备的段
    p.path.setText("192.168.199.1-3")
    ips, err = p._resolve_src()
    check(bool(err), "探不到设备时给出提示")

    # 指定网段
    p.src.setCurrentIndex(src_idx["net"])
    p.path.setText("192.168.195")
    ips, err = p._resolve_src()
    check(not err and len(ips) > 0, f"指定网段 192.168.195 解析出 {len(ips)} 台")

    # 指定网段：格式错误
    p.path.setText("not-a-net")
    ips, err = p._resolve_src()
    check(bool(err), f"网段格式错误被拦下：{err[:40]}")

    # 手动输入
    p.src.setCurrentIndex(src_idx["manual"])
    p.path.setText("192.168.195.21, 192.168.195.44")
    ips, err = p._resolve_src()
    check(not err and ips == ["192.168.195.21", "192.168.195.44"],
          f"手动输入解析：{ips}")

    # 手动输入：混入非 IP
    p.path.setText("192.168.195.21, garbage")
    ips, err = p._resolve_src()
    check(bool(err), f"手动输入含非法项被拦下：{err[:40]}")

    # 清单文件
    p.src.setCurrentIndex(src_idx["file"])
    p.path.setText(str(OUT / "ips.txt"))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ips.txt").write_text("# 测试清单\n192.168.195.21\n\n192.168.195.44\n",
                                 encoding="utf-8")
    ips, err = p._resolve_src()
    check(not err and ips == ["192.168.195.21", "192.168.195.44"],
          f"清单文件解析（跳过 # 注释与空行）：{ips}")

    # 回到自动扫描，清空重扫
    p.src.setCurrentIndex(src_idx["discover"])
    p.list.clear()
    p.scan_devices()
    check(p.list.count() > 0, f"重新扫描回到 {p.list.count()} 台")

    online = p._checked_ips()
    # 取消勾选一部分（最多一半，且至少留 1 台），验证「只测勾选的」
    p._check_all(True)
    drop = min(n // 2, max(0, n - 1))
    for i in range(n - drop, n):
        p.list.item(i).setCheckState(Qt.Unchecked)
    want = p._checked_ips()
    check(len(want) == n - drop and len(want) >= 1,
          f"取消勾选 {drop} 台后剩 {len(want)} 台待测")

    p.workers.setValue(2)
    p.timeout.setValue(120)
    p.on_run()
    check(not p.btn_run.isEnabled(), "运行中「开始」置灰")
    check(p.btn_stop.isEnabled(), "运行中「停止」可用")

    done = wait_until(lambda: p.btn_run.isEnabled() and bool(p._recs), timeout=240)
    check(done, "批量巡检跑完并回填结果")
    if not p._recs:
        return

    recs, s = p._recs, p._summary
    check(len(recs) == len(want), f"结果台数 == 勾选台数（{len(recs)} == {len(want)}）")
    check(p.table.rowCount() == len(recs), f"结果表行数 == {len(recs)}")
    check(s.get("total") == len(recs), f"汇总 total == {s.get('total')}")
    check(sum(s.get(k, 0) for k in ("PASS", "WARN", "FAIL")) == s.get("total"),
          f"合格+告警+不合格 == 总数（{s.get('PASS')}+{s.get('WARN')}+{s.get('FAIL')}"
          f"={s.get('total')}）")
    check(p.prog.value() == p.prog.maximum() == len(recs), "进度条走满")
    check(p.banner.isVisible(), "汇总 banner 已显示")
    check(p.table.maximumHeight() > 0, "结果表已展开")

    for r in recs:
        print(f"    {r['ip']:<16} {r['verdict']:<5} 版本={r.get('version') or '-':<8} "
              f"相机={r.get('cameras')} {r['PASS']}P/{r['FAIL']}F/{r['WARN']}W {r['secs']}s")
        if not r.get("error"):
            check("state" in r and "items" in r,
                  f"{r['ip']} 记录了 meta 与检查项明细")

    # 导出三种报告
    OUT.mkdir(parents=True, exist_ok=True)
    import batch_check
    html_p = batch_check.export_html(recs, str(OUT / "t.html"), s)
    csv_p = batch_check.export_csv(recs, str(OUT / "t.csv"))
    check(os.path.getsize(html_p) > 2000, f"HTML 报告非空（{os.path.getsize(html_p)} 字节）")
    check(os.path.getsize(csv_p) > 100, f"CSV 报告非空（{os.path.getsize(csv_p)} 字节）")
    xl_p = batch_check.export_xlsx(recs, str(OUT / "t.xlsx"), s)
    check(bool(xl_p) and os.path.getsize(xl_p) > 3000, "Excel 报告非空")

    # 空结果时导出应被拒绝而不是崩
    keep = p._recs
    p._recs = []
    p._export("html")
    p._recs = keep
    check("还没有结果" in p.out.toPlainText(), "无结果时导出给出提示而非崩溃")

    # 跑完后的截图
    pm = QPixmap(win.size())
    win.render(pm)
    shot = OUT / "batch_done.png"
    pm.save(str(shot))
    print(f"\n截图 -> {shot}")

    win.close()
    print("\n" + ("全部通过 ✔" if not fails else f"失败 {len(fails)} 项 ✘"))
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
