# -*- coding: utf-8 -*-
"""LivUmi-Ego-Std 设备 API 自动检查（零依赖，标准库 urllib）

用法：
    python ego_api_test.py --host 192.168.199.66                 # 只读体检（不动设备状态）
    python ego_api_test.py --host 192.168.199.66 --do-collect    # 额外实测一轮采集启停（会写文件）
    python ego_api_test.py --host 192.168.199.66 --model 235     # 按型号校验收分辨率
    python ego_api_test.py --host 192.168.199.66 --no-manual     # 跳过人工项
    python ego_api_test.py --host 192.168.199.66 --upload        # 结果回填飞书表格

只读模式不会启动/停止采集，也不会改任何配置。
"""
from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE_DIR, "out")

# 型号 -> 标称分辨率
MODEL_RES = {"235": (1600, 1200), "233": (1920, 1080)}


class Api:
    def __init__(self, host: str, port: int, timeout: float = 8.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout
        self.trace: list[str] = []

    def get(self, path: str, raw: bool = False):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                return r.status, (body if raw else json.loads(body.decode("utf-8", "replace")))
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:300]
        except Exception as e:  # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"

    def post(self, path: str, payload: dict | None = None):
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                try:
                    return r.status, json.loads(body.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    return r.status, body.decode("utf-8", "replace")[:300]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:300]
        except Exception as e:  # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------ 检查项


class R:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, cid, name, status, detail, src=""):
        self.items.append({"id": cid, "name": name, "status": status,
                           "detail": detail, "src": src})
        flag = {"PASS": "+", "FAIL": "x", "WARN": "!", "SKIP": "-"}[status]
        print(f"  [{flag}] {cid} {name:<12} {status:<4} {detail}")


def auto_checks(api: Api, r: R, model: str, do_collect: bool,
                device_id: str = "", account: str = "", password: str = "") -> None:
    print("\n--- 自动检查 ---")

    # 目标设备在线门禁：设备没接时，后面一堆检查会连锁失败，先说清楚
    st0, h0 = api.get("/api/v2/health")
    devmap = ((h0 or {}).get("selection") or {}).get("devices") or {} if isinstance(h0, dict) else {}
    active_dev = ((h0 or {}).get("selection") or {}).get("active_selection") or {}
    active_dev = active_dev.get("camera_device", "") if isinstance(active_dev, dict) else ""
    if device_id:
        tgt = devmap.get(device_id)
        if tgt:
            online = bool(tgt.get("logical_online")) and bool(tgt.get("hardware_connected"))
            r.add("DEV-ONLINE", f"目标设备 {device_id}", "PASS" if online else "FAIL",
                  f"online={tgt.get('logical_online')} hardware={tgt.get('hardware_connected')} "
                  f"pipeline={tgt.get('pipeline_state')} calib={tgt.get('calibration_state')}",
                  "/api/v2/health")
            if not online:
                r.add("DEV-HINT", "排查提示", "WARN",
                      f"{device_id} 未接入，硬件相关检查会连锁失败；接好头环/SSD 后重测", "")
        else:
            r.add("DEV-ONLINE", f"目标设备 {device_id}", "WARN",
                  f"设备清单中无此项，已知={list(devmap)}", "/api/v2/health")

    # E-D-004 采集控制台
    st, d = api.get("/api/v2/health")
    if st == 200 and isinstance(d, dict):
        state = d.get("overall_state", "?")
        prof = d.get("profile", "?")
        devs = (d.get("selection") or {}).get("enabled_devices") or []
        r.add("E-D-004", "采集控制台", "PASS" if state == "healthy" else "WARN",
              f"state={state} profile={prof} devices={devs}", "/api/v2/health")
    else:
        r.add("E-D-004", "采集控制台", "FAIL", f"HTTP {st}: {str(d)[:120]}", "/api/v2/health")

    # E-D-003 / E-L-015 头环与相机检测
    st2, cam = api.get("/api/v2/dex/camera-devices")
    n_cam = len(cam) if st2 == 200 and isinstance(cam, list) else -1
    r.add("E-L-015", "相机检测", "PASS" if n_cam > 0 else "FAIL",
          f"检出相机 {n_cam} 台" if n_cam >= 0 else f"HTTP {st2}", "/api/v2/dex/camera-devices")

    # E-L-017 / E-D-003 画面预览（抓一帧）
    st3, img = api.get("/api/v2/preview/snapshot.jpg", raw=True)
    ok_img = st3 == 200 and isinstance(img, (bytes, bytearray)) and len(img) > 5000
    size = f"{len(img)/1024:.0f}KB" if isinstance(img, (bytes, bytearray)) else str(img)[:60]
    r.add("E-L-017", "画面预览", "PASS" if ok_img else "FAIL",
          f"抓帧 HTTP {st3} 大小 {size}", "/api/v2/preview/snapshot.jpg")

    # E-D-006 / E-L-013 SSD 存储
    st4, s = api.get("/api/v2/storage")
    if st4 == 200 and isinstance(s, dict):
        kind = s.get("current_kind", "?")
        opts = s.get("options") or []
        ext = [o for o in opts if o.get("kind") not in ("local",)]
        r.add("E-D-006", "SSD存储", "PASS" if (ext and any(o.get("available") for o in ext))
              else "FAIL",
              f"当前={kind} 候选={[(o.get('kind'), o.get('available')) for o in opts]}",
              "/api/v2/storage")
        r.add("E-L-013", "SSD存储(Livstudio)", "PASS" if ext else "FAIL",
              f"外部存储候选 {len(ext)} 个", "/api/v2/storage")
    else:
        r.add("E-D-006", "SSD存储", "FAIL", f"HTTP {st4}", "/api/v2/storage")
        r.add("E-L-013", "SSD存储(Livstudio)", "FAIL", f"HTTP {st4}", "/api/v2/storage")

    # E-D-007 网线
    st5, n = api.get("/api/v2/network")
    if st5 == 200 and isinstance(n, dict):
        eth = [i for i in (n.get("interfaces") or []) if i.get("interface") == "eth0"]
        ipup = eth and eth[0].get("connected") and eth[0].get("ip_address")
        r.add("E-D-007", "网线功能", "PASS" if ipup else "WARN",
              f"wlan={n.get('ip_address')} eth0={(eth[0].get('ip_address') or '未连接') if eth else '无 eth0'}",
              "/api/v2/network")
    else:
        r.add("E-D-007", "网线功能", "FAIL", f"HTTP {st5}", "/api/v2/network")

    # E-D-002 网络：WiFi 列表可自动读，删网/重连仍要人工点
    st9, wifi = api.get("/api/v2/network/wifi")
    n_wifi = len(wifi) if st9 == 200 and isinstance(wifi, list) else -1
    cur = [w.get("ssid") for w in wifi if w.get("active")] if n_wifi > 0 else []
    r.add("E-D-002", "网络功能", "PASS" if n_wifi > 0 else "FAIL",
          f"扫描到 WiFi {n_wifi} 个，当前连接 {cur or '无'}", "/api/v2/network/wifi")

    # E-L-012 登录：正向 + 错误密码反向（给了账号才跑）
    if account:
        stl, lr = api.post("/api/v2/cloud/login", {"account": account, "password": password})
        ok_login = stl < 400 and isinstance(lr, dict) and not lr.get("error_code")
        r.add("E-L-012", "登录功能", "PASS" if ok_login else "FAIL",
              f"正向登录 HTTP {stl} {str(lr)[:110]}", "/api/v2/cloud/login")
        stb, br = api.post("/api/v2/cloud/login",
                           {"account": account, "password": password + "__wrong__"})
        rejected = stb >= 400 or (isinstance(br, dict) and br.get("error_code"))
        r.add("E-L-012B", "错误密码被拒", "PASS" if rejected else "FAIL",
              f"HTTP {stb} {str(br)[:110]}", "/api/v2/cloud/login")
    else:
        r.add("E-L-012", "登录功能", "SKIP", "未给 --account，跳过", "")

    # E-E-009 分辨率 / 标定
    expect_imu_hz, expect_cam_hz = 0.0, 0.0
    st6, cs = api.get("/api/v2/camera/settings")
    if st6 == 200 and isinstance(cs, dict):
        w, h, fps = cs.get("width"), cs.get("height"), cs.get("fps")
        # 采集实测用的期望频率：摄像头取 fps，IMU 取 imu_rate 兜底 200
        try:
            expect_cam_hz = float(fps) if fps else 0.0
        except (TypeError, ValueError):
            expect_cam_hz = 0.0
        try:
            expect_imu_hz = float(cs.get("imu_rate") or 200)
        except (TypeError, ValueError):
            expect_imu_hz = 200.0
        want = MODEL_RES.get(model)
        if want:
            ok = (w, h) == want
            r.add("E-E-009", "标定检验", "PASS" if ok else "FAIL",
                  f"实测 {w}x{h}@{fps} 型号{model}期望 {want[0]}x{want[1]}；生效设备={active_dev}"
                  + ("" if ok else "（注意：生效设备不是目标设备时该结果无效）"),
                  "/api/v2/camera/settings")
        else:
            r.add("E-E-009", "标定检验", "WARN",
                  f"实测 {w}x{h}@{fps}，生效设备={active_dev}（未给 --model，跳过比对）",
                  "/api/v2/camera/settings")

        # IMU 专项：端口 / 开关 / 状态
        imu_port = cs.get("ego_std_235_imu_port") or cs.get("ego_lite_01_imu_port")
        rec = cs.get("record_imu")
        rate = cs.get("imu_rate")
        r.add("IMU-CFG", "IMU配置", "PASS" if (imu_port and rec) else "FAIL",
              f"port={imu_port} record_imu={rec} rate={rate}", "/api/v2/camera/settings")
    else:
        r.add("E-E-009", "标定检验", "FAIL", f"HTTP {st6}", "/api/v2/camera/settings")

    st7, u = api.get("/api/v2/umi/sensor-status")
    if st7 == 200 and isinstance(u, dict):
        bad = [s["side"] for s in (u.get("sides") or []) if s.get("required") and not s.get("valid")]
        r.add("IMU-SENSOR", "IMU传感器", "PASS" if u.get("state") == "ready" and not bad else "WARN",
              f"state={u.get('state')} 未就绪侧={bad or '无'}", "/api/v2/umi/sensor-status")
    else:
        r.add("IMU-SENSOR", "IMU传感器", "FAIL", f"HTTP {st7}", "/api/v2/umi/sensor-status")

    # E-L-011 页面信息（版本 / 激活）
    st8, cl = api.get("/api/v2/cloud")
    if st8 == 200 and isinstance(cl, dict):
        r.add("E-L-011", "页面检查", "WARN" if cl.get("state") != "configured" else "PASS",
              f"cloud={cl.get('state')} sn={cl.get('device_sn')}", "/api/v2/cloud")

    # 上一轮采集的队列/丢帧指标（只读，不需要启采）
    _queue_history(api, r)

    # 采集启停（需显式开启）
    if do_collect:
        _collect_round(api, r, expect_imu_hz=expect_imu_hz, expect_cam_hz=expect_cam_hz)
    else:
        r.add("E-D-005", "采集启停", "SKIP", "未加 --do-collect，跳过实测", "")
        r.add("E-L-018", "采集启停(Livstudio)", "SKIP", "未加 --do-collect，跳过实测", "")


def _queue_history(api: Api, r: R) -> None:
    """读上一轮采集残留在 /api/v2/session 里的队列与丢帧指标。

    这是判定"队列溢出"最直接的证据，且不需要真的启一轮采集：
      queue_high_watermark / queue_capacity  -> 队列峰值水位
      drop_count / writer_rejected_count     -> 丢弃数
      streams[].missing_count / actual_rate_hz / max_gap_ns -> 单流掉帧与抖动
    """
    st, s = api.get("/api/v2/session")
    if st != 200 or not isinstance(s, dict):
        r.add("QUEUE-HIST", "上轮队列指标", "WARN", f"读取失败 HTTP {st}", "/api/v2/session")
        return
    rec = s.get("recorder") or {}
    if not rec:
        r.add("QUEUE-HIST", "上轮队列指标", "SKIP", "无历史采集记录，需先跑一轮 --do-collect", "/api/v2/session")
        return

    cap = rec.get("queue_capacity") or 0
    hwm = rec.get("queue_high_watermark") or 0
    pct = (hwm / cap * 100) if cap else 0.0
    drop = rec.get("drop_count") or 0
    rej = rec.get("writer_rejected_count") or 0
    lat = rec.get("disk_latency_ms") or 0.0

    ok = (drop == 0 and rej == 0 and (pct < 50 if cap else True))
    r.add("QUEUE-HIST", "上轮队列指标",
          "PASS" if ok else "FAIL",
          f"水位 {hwm}/{cap} ({pct:.2f}%) drop={drop} rejected={rej} "
          f"磁盘延迟={lat:.1f}ms session={rec.get('session_id', '-')}",
          "/api/v2/session")

    # 逐条数据流：掉帧与频率稳定性
    for topic, v in (rec.get("streams") or {}).items():
        miss = v.get("missing_count") or 0
        hz = v.get("actual_rate_hz")
        gap = v.get("max_gap_ns")
        gap_ms = (gap / 1e6) if gap else 0.0
        short = topic.rsplit("/", 1)[-1]
        sok = miss == 0 and (gap_ms < 50 if gap else True)
        r.add(f"STREAM-{short}", f"数据流 {short}",
              "PASS" if sok else "FAIL",
              f"{v.get('message_count')} 条 掉帧={miss} 实测={hz}Hz 最大间隔={gap_ms:.1f}ms",
              "/api/v2/session")


def _file_index(api: Api) -> dict:
    """/api/v2/files 的 {path: 文件信息} 索引，用于比对采集前后新增了哪个文件。"""
    st, j = api.get("/api/v2/files")
    if st == 200 and isinstance(j, list):
        return {f.get("path"): f for f in j if isinstance(f, dict)}
    return {}


def _collect_round(api: Api, r: R, seconds: float = 8.0,
                   expect_imu_hz: float = 0.0, expect_cam_hz: float = 0.0) -> None:
    """真的采一轮：启采 -> 计时 -> 停采 -> 校验落盘文件与各数据流实际条数。

    这是脚本里唯一做"动作"的部分，判定不看配置、只看结果：
      1. /api/v2/files 里必须出现新文件，且 size_bytes > 100KB、时长 ≈ seconds
      2. IMU 流 message_count 必须 ≈ 期望频率 × 秒数，missing_count == 0
      3. 相机流同理
      4. 队列水位 < 50% 且 drop_count == 0
    """
    st, cur = api.get("/api/v2/session")
    if st == 200 and isinstance(cur, dict) and cur.get("state") != "idle":
        r.add("E-D-005", "采集启停", "FAIL", f"当前非空闲 state={cur.get('state')}，先停掉再测", "")
        return

    before = _file_index(api)

    st, res = api.post("/api/v2/session/start",
                       {"command_id": f"autotest-{int(time.time())}"})
    if st >= 400:
        r.add("E-D-005", "采集启停", "FAIL", f"启动失败 HTTP {st}: {str(res)[:150]}", "/api/v2/session/start")
        return
    t0 = time.time()
    print(f"  ... 采集中 {seconds}s")
    time.sleep(seconds)

    st, s = api.get("/api/v2/session")
    rec = (s or {}).get("recorder") or {}
    print(f"  ... 采集中队列 depth={rec.get('queue_depth')}/{rec.get('queue_capacity')} "
          f"峰值={rec.get('queue_high_watermark')} 丢帧={rec.get('drop_count')}")

    st2, res2 = api.post("/api/v2/session/stop")
    elapsed = time.time() - t0

    # 等落盘：轮询 /api/v2/files，最多 20s
    new_file = None
    for _ in range(20):
        time.sleep(1)
        after = _file_index(api)
        added = [f for p, f in after.items() if p not in before]
        added = [f for f in added if not str(f.get("name", "")).endswith(".partial")]
        if added:
            new_file = max(added, key=lambda f: f.get("modified_time_ns") or 0)
            break
    if new_file is None:
        after = _file_index(api)
        added = [f for p, f in after.items() if p not in before]
        if added:
            new_file = max(added, key=lambda f: f.get("modified_time_ns") or 0)

    st3, s3 = api.get("/api/v2/session")
    r3 = (s3 or {}).get("recorder") or {}

    # --- 断言 1：真的落盘了新文件，且大小、时长对得上
    if not new_file:
        r.add("E-D-005", "采集启停", "FAIL",
              f"停采后 /api/v2/files 未出现新文件（停采HTTP {st2}）", "/api/v2/files")
    else:
        size = new_file.get("size_bytes") or 0
        dur = new_file.get("duration_seconds") or 0.0
        size_ok = size > 100 * 1024
        dur_ok = (0.5 * seconds) <= dur <= (2.0 * seconds + 2)
        r.add("E-D-005", "采集启停", "PASS" if (size_ok and dur_ok) else "FAIL",
              f"新文件 {new_file.get('name')} 大小={size/1048576:.1f}MB 时长={dur:.1f}s "
              f"(期望≈{seconds}s) 停采HTTP={st2}", "/api/v2/files")
        r.add("E-L-018", "采集启停(Livstudio)", "PASS" if (size_ok and dur_ok) else "FAIL",
              f"新文件 {new_file.get('name')} 大小={size/1048576:.1f}MB 时长={dur:.1f}s", "/api/v2/files")

    # --- 断言 2/3：各数据流实际采到多少条（这才是在"测"IMU，而不是看配置）
    streams = r3.get("streams") or {}
    if not streams:
        r.add("DATA-STREAMS", "数据流实测", "FAIL", "停采后无任何数据流记录", "/api/v2/session")
    for topic, v in streams.items():
        cnt = v.get("message_count") or 0
        miss = v.get("missing_count") or 0
        hz = v.get("actual_rate_hz")
        short = topic.rsplit("/", 1)[-1]
        exp_hz = expect_imu_hz if "imu" in topic.lower() else expect_cam_hz
        if exp_hz:
            exp_cnt = exp_hz * elapsed
            dev = abs(cnt - exp_cnt) / exp_cnt if exp_cnt else 1.0
            ok = miss == 0 and dev <= 0.25
            r.add(f"DATA-{short}", f"实测 {short}",
                  "PASS" if ok else "FAIL",
                  f"实测 {cnt} 条 / 期望≈{exp_cnt:.0f} ({exp_hz}Hz×{elapsed:.1f}s) 偏差{dev*100:.0f}% "
                  f"掉帧={miss} 实测频率={hz}Hz", "/api/v2/session")
        else:
            r.add(f"DATA-{short}", f"实测 {short}", "PASS" if cnt > 0 and miss == 0 else "FAIL",
                  f"{cnt} 条 掉帧={miss} 实测={hz}Hz（未给期望频率，只校验非零）", "/api/v2/session")

    # --- 断言 4：队列没有溢出
    cap = r3.get("queue_capacity") or 0
    hwm = r3.get("queue_high_watermark") or 0
    drop = r3.get("drop_count") or 0
    pct = (hwm / cap * 100) if cap else 0.0
    r.add("IMU-QUEUE", "队列溢出", "PASS" if (drop == 0 and pct < 50) else "FAIL",
          f"本次实测 水位 {hwm}/{cap} ({pct:.2f}%) drop={drop} 磁盘延迟={r3.get('disk_latency_ms', 0):.1f}ms",
          "/api/v2/session")


# ------------------------------------------------------------------ 人工项


MANUAL = ["E-D-001", "E-D-002", "E-E-008", "E-L-012", "E-L-014", "E-L-016"]


def manual_checks(r: R, skip: bool) -> None:
    appeared = {i["id"] for i in r.items}
    # 自动检查已给出结论的（非 SKIP）不再问人工；--no-manual 时只要出现过就不重复记
    covered = appeared if skip else {i["id"] for i in r.items if i["status"] != "SKIP"}
    todo = [c for c in MANUAL if c not in covered]
    if skip:
        for cid in todo:
            r.add(cid, next((c["func"] for c in CASES if c["id"] == cid), cid), "SKIP", "已跳过", "")
        return
    if not todo:
        print("\n--- 人工确认项 ---（已全部被自动检查覆盖）")
        return
    print("\n--- 人工确认项 ---")
    idx = {c["id"]: c for c in CASES}
    for cid in todo:
        c = idx.get(cid)
        if not c:
            continue
        print(f"\n{cid}  {c['module']} / {c['func']}")
        print(f"  步骤: {c['steps']}")
        print(f"  标准: {c['crit']}")
        print(f"  要点: {c.get('hint','')}")
        try:
            while True:
                v = input("  [P]通过 [F]失败 [B]阻塞 [S]跳过 > ").strip().upper()
                if v and v[0] in "PFBS":
                    break
            note = input("  备注(可留空) > ").strip()
            if v[0] == "F" and not note:
                note = input("  [失败必填] 现象 > ").strip() or "(未填写)"
        except EOFError:
            print("\n(输入中断，剩余人工项标记为跳过)")
            for rest in todo[todo.index(cid):]:
                rc = idx.get(rest)
                r.add(rest, rc["func"] if rc else rest, "SKIP", "输入中断未确认", "人工")
            return
        st = {"P": "PASS", "F": "FAIL", "B": "WARN", "S": "SKIP"}[v[0]]
        r.add(cid, c["func"], st, note or "(无备注)", "人工")


# ------------------------------------------------------------------ 报告


def write_report(r: R, host: str, device: str) -> str:
    tally = {k: 0 for k in ("PASS", "FAIL", "WARN", "SKIP")}
    for it in r.items:
        tally[it["status"]] += 1
    color = {"PASS": ("#27500A", "#EAF3DE"), "FAIL": ("#791F1F", "#FCEBEB"),
             "WARN": ("#633806", "#FAEEDA"), "SKIP": ("#666", "#F1F1EF")}
    rows = "".join(
        f"<tr><td style='background:{color[i['status']][1]};color:{color[i['status']][0]};"
        f"font-weight:500'>{i['status']}</td><td><code>{html.escape(i['id'])}</code></td>"
        f"<td>{html.escape(i['name'])}</td><td>{html.escape(i['detail'])}</td>"
        f"<td><code>{html.escape(i['src'])}</code></td></tr>" for i in r.items)
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Ego-Std API 检查 {html.escape(device)}</title><style>
body{{font:14px/1.6 system-ui,'Segoe UI',sans-serif;margin:0;padding:28px;color:#222;background:#fff}}
h1{{font-size:18px;font-weight:500;margin:0 0 4px}}
.meta{{color:#666;font-size:12px;margin-bottom:18px}}
.cards{{display:flex;gap:12px;margin:0 0 20px}}
.card{{flex:1;border:1px solid #d8d6cf;border-radius:10px;padding:12px 14px}}
.card b{{display:block;font-size:22px;font-weight:500}}
table{{width:100%;border-collapse:collapse;border:1px solid #d8d6cf;border-radius:10px;overflow:hidden}}
th{{background:#f5f3ee;text-align:left;font-weight:500;padding:9px 10px;border-bottom:1px solid #d8d6cf;font-size:13px}}
td{{padding:9px 10px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px}}
code{{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#f3f1ec;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>LivUmi-Ego-Std 设备体检报告</h1>
<div class="meta">设备 {html.escape(device)} · 主机 {html.escape(host)} ·
{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
<div class="cards">
<div class="card"><b>{len(r.items)}</b>检查项</div>
<div class="card"><b style="color:#27500A">{tally['PASS']}</b>通过</div>
<div class="card"><b style="color:#A32D2D">{tally['FAIL']}</b>失败</div>
<div class="card"><b style="color:#BA7517">{tally['WARN']}</b>告警</div>
<div class="card"><b style="color:#666">{tally['SKIP']}</b>跳过</div>
</div>
<table><thead><tr><th>结果</th><th>编号</th><th>项目</th><th>详情</th><th>来源</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, f"apicheck_{device}.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(doc)
    return p


def upload(r: R, dry: bool) -> None:
    by_id = {i["id"]: i for i in r.items}
    first, last = FEISHU["first_row"], FEISHU["first_row"] + len(CASES) - 1
    rng = f"{FEISHU['sheet_id']}!{FEISHU['result_col']}{first}:{FEISHU['note_col']}{last}"
    mp = {"PASS": "P", "FAIL": "F", "WARN": "B", "SKIP": "S"}
    values = [[mp.get(by_id[c["id"]]["status"], ""),
               by_id[c["id"]]["detail"][:200] if c["id"] in by_id else ""]
              for c in CASES]
    payload = json.dumps({"valueRange": {"range": rng, "values": values}}, ensure_ascii=False)
    if dry:
        print("将写入", rng)
        print(json.dumps(values, ensure_ascii=False, indent=1))
        return
    import subprocess
    r2 = subprocess.run(["lark-cli", "api", "PUT",
                         f"/open-apis/sheets/v2/spreadsheets/{FEISHU['sheet_token']}/values",
                         "--data", payload], capture_output=True, text=True, encoding="utf-8")
    print(r2.stdout or r2.stderr)


# ------------------------------------------------------------------ main


def main() -> None:
    ap = argparse.ArgumentParser(description="Ego-Std 设备 API 自动体检")
    ap.add_argument("--host", default=None,
                    help="设备 IP；省略则读 device_ip.txt，没有就自动扫描本机网段")
    ap.add_argument("--port", type=int, default=18000)
    ap.add_argument("--device", "-d", default="DUT")
    ap.add_argument("--model", default="", choices=["", "233", "235"], help="头环型号，用于校验收分辨率")
    ap.add_argument("--device-id", default="ego-std-235",
                    help="目标逻辑设备 id，用于门禁检查（默认 ego-std-235）")
    ap.add_argument("--account", default="", help="Livstudio 账号，给了才测登录")
    ap.add_argument("--password", default="", help="Livstudio 密码")
    ap.add_argument("--do-collect", action="store_true", help="实测一轮采集启停（会写文件）")
    ap.add_argument("--no-manual", action="store_true", help="跳过人工确认项")
    ap.add_argument("--upload", action="store_true", help="回填飞书表格")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import devip
    args.host = devip.resolve_host(args.host)

    api = Api(args.host, args.port)
    print(f"目标 {args.host}:{args.port}  只读体检{'' if args.do_collect else '（不启停采集）'}")
    r = R()
    auto_checks(api, r, args.model, args.do_collect, args.device_id, args.account, args.password)
    manual_checks(r, args.no_manual)

    os.makedirs(OUT, exist_ok=True)
    jp = os.path.join(OUT, f"apicheck_{args.device}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump({"host": args.host, "device": args.device, "at": datetime.now().isoformat(),
                   "items": r.items}, f, ensure_ascii=False, indent=2)
    p = write_report(r, args.host, args.device)
    print(f"\nJSON -> {jp}")
    print(f"报告 -> {p}")
    if args.upload or args.dry_run:
        upload(r, args.dry_run)


if __name__ == "__main__":
    main()
