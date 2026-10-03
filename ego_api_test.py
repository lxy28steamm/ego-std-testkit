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
import contextlib
import html
import http.client
import io
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

# 型号 -> 可接受的 (宽, 高) 集合。
#
# ⚠️ 不要再写死"型号 = 唯一分辨率"。实测（2026-10-03，Ego-Std-235 / SC235HGS 双目）：
#   相机 V4L2 真实能力 = 3200x1200 / 3840x1200（side_by_side 双目拼接）
#   但 /camera/settings 里配的是 1920x1080
#   而早期这张表写的是 1600x1200
# 三个值互不相符，且 1920x1080 采集完全正常（30fps 满帧、0 丢包）。
# 说明分辨率是**可配置的运行参数**，不是型号的固有属性。
# 所以改成"可接受集合"，并把实际在用的 1080p 收进去。
MODEL_RES = {
    "235": {(3200, 1200), (3840, 1200), (1920, 1080), (1600, 1200)},
    "233": {(1920, 1080), (3840, 1200), (3200, 1200)},
}

# 头环型号 → 设备侧相机槽位（device-selection 的 camera_device 枚举值）
#
# ⚠️ 槽位名和相机芯片型号对不上，别写死：
#   设备 OpenAPI 的 camera_device 枚举只有 ego-lite-01 / ego-std-235 / ego-plus / none，
#   **没有 233**。但 /api/v2/camera/settings 里能看到设备上曾同时配过两台相机：
#       ego_lite_01_device_path : .../usb-YCTC_YCTC_SC233HGS_...      ← 233 芯片
#       ego_std_235_device_path: .../usb-YCTC_YCTC_SC235HGS_MIC_...  ← 235 芯片
#   所以 233 走 `ego-lite-01` 槽位，235 走 `ego-std-235` 槽位。
#
# 认芯片型号不要靠猜，直接看设备上的 by-id 软链接（最权威）：
#   GET /api/v2/camera/settings → *_device_path 里的 SCxxxHGS
#   SSH ls -l /dev/v4l/by-id/  → 实际插着的那台
MODEL_SLOT = {"233": "ego-lite-01", "235": "ego-std-235"}


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
        return self._send("POST", path, payload)

    def put(self, path: str, payload: dict | None = None):
        return self._send("PUT", path, payload)

    def delete(self, path: str, payload: dict | None = None):
        return self._send("DELETE", path, payload)

    def _send(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
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
        self.meta: dict = {}   # 版本/状态/profile/相机数等，批量汇总时直接取用

    def add(self, cid, name, status, detail, src=""):
        self.items.append({"id": cid, "name": name, "status": status,
                           "detail": detail, "src": src})
        flag = {"PASS": "+", "FAIL": "x", "WARN": "!", "SKIP": "-"}[status]
        print(f"  [{flag}] {cid} {name:<12} {status:<4} {detail}")


def summarize(r) -> dict:
    """把检查项压成一份计数 + 判定，批量表格和退出码都用它。"""
    from collections import Counter
    c = Counter(i["status"] for i in r.items)
    s = {k: int(c.get(k, 0)) for k in ("PASS", "FAIL", "WARN", "SKIP")}
    s["total"] = len(r.items)
    s["verdict"] = "FAIL" if s["FAIL"] else ("WARN" if s["WARN"] else "PASS")
    return s


def auto_checks(api: Api, r: R, model: str, do_collect: bool,
                device_id: str = "", account: str = "", password: str = "") -> None:
    print("\n--- 自动检查 ---")

    # 版本号来自 openapi.json 的 info.version。批量巡检的汇总表要显示它，
    # 所以这里顺手收进 meta（不新增检查项，避免影响单台报告的项数）。
    st_v, spec = api.get("/openapi.json")
    if st_v == 200 and isinstance(spec, dict):
        r.meta["version"] = str((spec.get("info") or {}).get("version", ""))
        r.meta["app"] = str((spec.get("info") or {}).get("title", ""))

    # 目标设备在线门禁：设备没接时，后面一堆检查会连锁失败，先说清楚
    st0, h0 = api.get("/api/v2/health")
    # 连不上时 api.get 返回的是错误字符串而不是 dict，这里必须先判类型，
    # 否则批量巡检扫到离线设备会直接 AttributeError 崩掉（而不是记成 FAIL）
    h0 = h0 if isinstance(h0, dict) else {}

    # ---- 设备身份（SN / 序列号 / 平台 ID）--------------------------
    # ⚠️ 这里有好几个"看起来都像 SN"的字段，别混用，各有各的用处：
    #   cloud.device_sn        = pi-<树莓派hostname>   整机 SN，报告里显示这个
    #   cloud.device_serial    = <去掉 pi- 的那段>      同上，只是短写法
    #   cloud.ldp_device_id    = EL-H1-260801-2534     平台侧设备 ID（交付单上用这个）
    #   devices[槽位].hardware_serial = 0152312181647  **相机芯片**序列号，不是整机 SN
    # 混出来的教训：我一度把 camera 的 serial 当成整机 SN 报出去，被一眼看穿。
    cl = (h0 or {}).get("cloud") or {}
    r.meta["sn"] = str(cl.get("device_sn") or "")
    r.meta["serial"] = str(cl.get("device_serial") or "")
    r.meta["ldp_id"] = str(cl.get("ldp_device_id") or "")
    # 相机芯片序列号：留着，坏货返修时厂商要这个。
    # ⚠️ 取值来源踩过坑：同一个字段名在三个地方，值不一样——
    #    selection.devices[槽位].hardware_serial = None   ← 空的！
    #    devices[槽位].hardware_serial            = 0152312181647   ✅
    #    workers[槽位].hardware_serial            = 0152312181647   ✅
    # 所以只能从 health.devices / health.workers 取，取 selection.devices 会拿到 None。
    _dslots = ((h0 or {}).get("selection") or {}).get("devices") or {}
    _adev = (((h0 or {}).get("selection") or {}).get("active_selection") or {})
    _adev = _adev.get("camera_device", "") if isinstance(_adev, dict) else ""
    _hdev = (h0 or {}).get("devices") or {}
    _hwrk = (h0 or {}).get("workers") or {}
    _csn = ((_hdev.get(_adev) or {}).get("hardware_serial")
            or (_hwrk.get(_adev) or {}).get("hardware_serial") or "")
    r.meta["camera_sn"] = str(_csn or "")

    devmap = _dslots
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

    # E-D-004 采集控制台 —— 同时充当「设备可达性」闸门
    st, d = api.get("/api/v2/health")
    if st == 200 and isinstance(d, dict):
        state = d.get("overall_state", "?")
        prof = d.get("profile", "?")
        devs = (d.get("selection") or {}).get("enabled_devices") or []
        # 顺手收进 meta：批量巡检的汇总表直接取这几项，不必再请求一遍设备
        r.meta["state"] = str(state)
        r.meta["profile"] = str(prof)
        r.meta["devices"] = ",".join(str(x) for x in devs)
        # 相机型号：--model 给的是用户口径（233/235），profile 是设备口径（ego-std-235）
        r.meta["model"] = model or ""
        r.meta["slot"] = MODEL_SLOT.get(model, "") or ""
        r.add("E-D-004", "采集控制台", "PASS" if state == "healthy" else "WARN",
              f"state={state} profile={prof} devices={devs}", "/api/v2/health")
    else:
        # 设备根本连不上：后面每一条都会顺带 FAIL 一遍（相机/SSD/网线/画面…），
        # 全是同一个根因的噪音。批量巡检扫到离线设备时，一屏 FAIL 反而看不出真相。
        # 所以这里直接短路：只留一条"设备不可达"，其余不再逐项打。
        r.meta["state"] = "UNREACHABLE"
        r.meta["profile"] = ""
        r.meta["devices"] = ""
        r.meta["cameras"] = 0
        hint = {
            0: "连接超时/拒绝（设备关机？IP 变了？不在同一网段？）",
            404: "路径不存在（这台可能不是 ego 设备，或 API 版本不同）",
        }.get(st, str(d)[:120])
        r.add("E-D-004", "设备可达性", "FAIL", f"HTTP {st} — {hint}", "/api/v2/health")
        r.add("OFFLINE-SKIP", "其余检查", "SKIP", "设备不可达，已跳过全部硬件检查", "")
        return

    # E-D-003 相机检测
    #
    # ⚠️ 原来这里打的是 /api/v2/dex/camera-devices，那是 **Dex 机型专用**接口，
    # 在 Ego-Std / Ego-Lite 上恒返回 []，于是每台都误报 "检出相机 0 台" FAIL。
    # 实测 2026-10-03 确认：相机真实状态在 /api/v2/health 的 selection 里：
    #   selection.active_selection.camera_device = 当前生效槽位
    #   selection.devices[槽位].logical_online / hardware_connected / pipeline_state
    # 判据改成"生效槽位是否硬件已连接"，这才是"相机插没插"的真实答案。
    #
    # 给了 --model 就先把槽位切过去（233 → ego-lite-01，235 → ego-std-235），
    # 否则会出现"插的是 235、但设备还停在 ego-lite-01 槽位"→ 误判相机没插。
    act = active_dev or ""
    if model:
        want_slot = MODEL_SLOT.get(model)
        if want_slot and want_slot != act:
            # ⚠️ 采集中切不了相机：PUT /device-selection 会返
            #    409 session_active "DEVICE cannot be changed during an active Session"
            # 所以先看有没有在录，有就停掉再切。
            st_s, s_s = api.get("/api/v2/session")
            if st_s == 200 and isinstance(s_s, dict) and s_s.get("state") == "recording":
                _stop_session(api, f"autotest-preselect-{int(time.time())}")
                time.sleep(2)
            stx, resx = api.put("/api/v2/device-selection", {"camera_device": want_slot})
            if stx < 400:
                time.sleep(3)          # 等设备重启对应 pipeline
                _, h2 = api.get("/api/v2/health")
                prev = act
                act = ((h2 or {}).get("selection") or {}).get(
                    "active_selection", {}).get("camera_device") or ""
                devmap = (h2 or {}).get("selection", {}).get("devices") or devmap
                r.add("CAM-SEL", "相机选型", "PASS" if act == want_slot else "WARN",
                      f"型号{model} → 槽位 {want_slot}（切换前 {prev or '无'}）"
                      f" 生效={act}", "/api/v2/device-selection")
            else:
                r.add("CAM-SEL", "相机选型", "FAIL",
                      f"型号{model} → 槽位 {want_slot} 切换失败 HTTP {stx}: "
                      f"{str(resx)[:140]}", "/api/v2/device-selection")
        else:
            r.add("CAM-SEL", "相机选型", "PASS",
                  f"型号{model} → 槽位 {want_slot}，已是当前生效槽位，无需切换",
                  "/api/v2/device-selection")
    else:
        r.add("CAM-SEL", "相机选型", "SKIP",
              "未给 --model（233 / 235），按当前生效槽位检查", "")

    act = act or ""
    tgt_slot = devmap.get(act) or {}
    hw_ok = bool(tgt_slot.get("hardware_connected")) and bool(tgt_slot.get("logical_online"))
    r.meta["cameras"] = 1 if hw_ok else 0
    want_chip = ("SC%sHGS" % model) if model else ""
    chip_txt = ""
    if want_chip:
        stc, cs = api.get("/api/v2/camera/settings")
        paths = ""
        if stc == 200 and isinstance(cs, dict):
            for k, v in cs.items():
                if k.endswith("_device_path") and want_chip in str(v or ""):
                    paths = f" 配置路径命中 {k}"
                    break
            else:
                paths = f" 但 /camera/settings 里没找到含 {want_chip} 的 *_device_path"
        chip_txt = f" 期望芯片={want_chip}{paths}"
    if act:
        r.add("E-D-003", "相机检测", "PASS" if hw_ok else "FAIL",
              f"生效槽位={act} hardware_connected={tgt_slot.get('hardware_connected')} "
              f"pipeline={tgt_slot.get('pipeline_state')} 相机SN={r.meta.get('camera_sn') or '未取到'}"
              + chip_txt,
              "/api/v2/health")
    else:
        r.add("E-D-003", "相机检测", "WARN",
              "health.selection 没给 active_selection.camera_device，无法判定", "/api/v2/health")

    # E-L-017 / E-D-003 画面预览（抓一帧）
    st3, img = api.get("/api/v2/preview/snapshot.jpg", raw=True)
    ok_img = st3 == 200 and isinstance(img, (bytes, bytearray)) and len(img) > 5000
    size = f"{len(img)/1024:.0f}KB" if isinstance(img, (bytes, bytearray)) else str(img)[:60]
    if not hw_ok:
        # 相机根本没连上，抓帧必然失败（实测会 timeout 或 500）。
        # 根因已由 E-D-003 报过，这里再报一条 FAIL 只是噪音 —— 判 SKIP。
        r.add("E-L-017", "画面预览", "SKIP",
              f"相机未连接（槽位 {act} hardware_connected=False），跳过抓帧。"
              f"实测 HTTP {st3}", "/api/v2/preview/snapshot.jpg")
    else:
        r.add("E-L-017", "画面预览", "PASS" if ok_img else "FAIL",
              f"抓帧 HTTP {st3} 大小 {size}", "/api/v2/preview/snapshot.jpg")

    # E-D-006 SSD 存储
    # E-D-006 SSD 存储
    #
    # 原来这里同时加了 E-D-006 和 E-L-013 两条，**同接口同判据，纯重复**，
    # 只是名字里带个 "(Livstudio)"。已合并成一条。
    st4, s = api.get("/api/v2/storage")
    if st4 == 200 and isinstance(s, dict):
        kind = s.get("current_kind", "?")
        opts = s.get("options") or []
        ext = [o for o in opts if o.get("kind") not in ("local",)]
        avail = [o for o in ext if o.get("available")]
        # 顺带把容量信息打出来 —— 交付场景很关心"还剩多少"，原来只报有没有
        cap = ""
        for o in (avail or ext):
            tot, free = o.get("total_bytes"), o.get("free_bytes")
            if tot:
                cap = " 容量 %.1f/%.1f GB" % ((free or 0) / 2**30, tot / 2**30)
                break
        if avail:
            r.add("E-D-006", "SSD存储", "PASS",
                  f"当前={kind} 可用={[o.get('kind') for o in avail]}{cap}", "/api/v2/storage")
        else:
            r.add("E-D-006", "SSD存储", "FAIL",
                  f"当前={kind} 候选={[(o.get('kind'), o.get('available')) for o in opts]}",
                  "/api/v2/storage")
    else:
        r.add("E-D-006", "SSD存储", "FAIL", f"HTTP {st4}", "/api/v2/storage")

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
            ok = (w, h) in want
            r.add("E-E-009", "标定检验", "PASS" if ok else "WARN",
                  f"实测 {w}x{h}@{fps} 型号{model} 可接受={sorted(want)}"
                  + ("" if ok else f"；生效设备={active_dev}（不在可接受集合，请确认是否配错）"),
                  "/api/v2/camera/settings")
        else:
            r.add("E-E-009", "标定检验", "WARN",
                  f"实测 {w}x{h}@{fps}，生效设备={active_dev}（未给 --model，跳过比对）",
                  "/api/v2/camera/settings")

        # IMU 专项：端口 / 开关 / 状态
        #
        # ⚠️ 原判据只读 ego_std_235_imu_port，实测该字段是 None，而 IMU 实际挂在
        # ego_lite_01_imu_port 上（SC233HGS 的 USB 串口）→ 每台都误报 FAIL。
        # 正确做法：**任一非空端口即算接上**（不同 profile 字段名不同）。
        # record_imu 只作为参考信息打印，不参与判定 ——
        # 实测 record_imu=False 时照样采出 200Hz IMU 数据（走 USB 内部通道）。
        imu_port = (cs.get("ego_std_235_imu_port") or cs.get("ego_lite_01_imu_port")
                    or cs.get("umi_imu_port") or "")
        rec = cs.get("record_imu")
        rate = cs.get("imu_rate")
        if imu_port:
            r.add("IMU-CFG", "IMU配置", "PASS",
                  f"port={imu_port} rate={rate}（record_imu={rec}，仅供参考）",
                  "/api/v2/camera/settings")
        else:
            r.add("IMU-CFG", "IMU配置", "WARN",
                  f"未读到 IMU 端口（record_imu={rec} rate={rate}）；"
                  f"若本机型 IMU 走 USB 内部通道则属正常", "/api/v2/camera/settings")

    st7, u = api.get("/api/v2/umi/sensor-status")
    if st7 == 200 and isinstance(u, dict):
        # state 可能是 ready / waiting。waiting 多半是"没启采、等指令"，
        # 不是硬件故障，所以只在这两种状态下区分 PASS / WARN。
        st_u = u.get("state")
        bad = [s["side"] for s in (u.get("sides") or []) if s.get("required") and not s.get("valid")]
        if bad:
            st_txt, level = f"未就绪侧={bad}", "WARN"
        elif st_u == "ready":
            st_txt, level = "已就绪", "PASS"
        else:
            st_txt, level = f"state={st_u}（未启采时正常）", "WARN"
        r.add("IMU-SENSOR", "IMU传感器", level, f"{st_txt} sides={len(u.get('sides') or [])}",
              "/api/v2/umi/sensor-status")
    else:
        r.add("IMU-SENSOR", "IMU传感器", "FAIL", f"HTTP {st7}", "/api/v2/umi/sensor-status")

    # E-L-011 页面信息
    #
    # ⚠️ 原判据 `state != "configured"` 是错的：设备正常时 state=**online**
    # 且 configured=true（两个字段语义不同：state=平台连接态，configured=是否已配置）。
    # 照原判据 EVERY healthy device 都被判 WARN。改成：configured 为真即 PASS。
    st8, cl = api.get("/api/v2/cloud")
    if st8 == 200 and isinstance(cl, dict):
        cfg = bool(cl.get("configured"))
        r.add("E-L-011", "页面检查", "PASS" if cfg else "WARN",
              f"state={cl.get('state')} configured={cfg} sn={cl.get('device_sn')} "
              f"ldp_id={cl.get('ldp_device_id')}", "/api/v2/cloud")
    else:
        r.add("E-L-011", "页面检查", "FAIL", f"HTTP {st8}", "/api/v2/cloud")

    # 上一轮采集的队列/丢帧指标（只读，不需要启采）
    _queue_history(api, r)

    # 采集启停（需显式开启）
    if do_collect:
        _collect_round(api, r, expect_imu_hz=expect_imu_hz, expect_cam_hz=expect_cam_hz)
    else:
        r.add("E-D-005", "采集启停", "SKIP", "未加 --do-collect，跳过实测", "")


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


# /api/v2/session/stop 的 body 里 command_id 是**必填**（实测 2026-10-03）：
#   不带 body -> 422 {"loc":["body"],"msg":"Field required"}
#   带 {}     -> 422 {"loc":["body","command_id"],"msg":"Field required"}
# 这个坑很阴险：stop 失败设备不会报错，只是**继续录**，于是 message_count 一路涨，
# 下一轮 DATA-* 全判"条数超预期 262%"，看起来像数据异常，其实是上轮没停干净。
def _stop_session(api: Api, command_id: str, tries: int = 3) -> tuple[int, object]:
    """停采并确认真的停了。返回 (最后一次 HTTP 码, 响应体)。"""
    last: tuple[int, object] = (0, "")
    for i in range(tries):
        last = api.post("/api/v2/session/stop",
                        {"command_id": command_id, "reason": "operator_request"})
        if last[0] < 400:
            break
        time.sleep(1.5)
    if last[0] < 400:
        # 落盘要时间，轮询等它真的离开 recording
        for _ in range(12):
            st, s = api.get("/api/v2/session")
            if st == 200 and isinstance(s, dict) and s.get("state") != "recording":
                break
            time.sleep(1)
    return last


def _collect_round(api: Api, r: R, seconds: float = 8.0,
                   expect_imu_hz: float = 0.0, expect_cam_hz: float = 0.0) -> None:
    """真的采一轮：启采 -> 计时 -> 停采 -> 校验落盘文件与各数据流实际条数。

    这是脚本里唯一做"动作"的部分，判定不看配置、只看结果：
      1. /api/v2/files 里必须出现新文件，且 size_bytes > 100KB、时长 ≈ seconds
      2. IMU 流 message_count 必须 ≈ 频率 × **设备自报时间跨度**，missing_count == 0
      3. 相机流同理
      4. 队列水位 < 50% 且 drop_count == 0

    ⚠️ 期望条数**不能拿本地秒表 elapsed 去乘频率**。踩过的坑：stop 失败设备会继续录，
    下一轮 message_count 是"录了多久就累计多少"，用秒表算会报偏差 262%，
    看起来像数据异常其实是上轮没停干净。设备每条流都带 first_time_ns/last_time_ns，
    用它算跨度才是这台设备自己的口径（实测 cnt 与 hz×span 误差 ≤2 条）。
    """
    # 可用于"再开一轮"的空闲态。注意设备停采后是 complete（不是 idle），
    # 实测 idle 几乎不会出现，只在刚重启时才是。只认 idle 会永远判"非空闲"。
    IDLE_STATES = ("idle", "complete", "", None)

    st, cur = api.get("/api/v2/session")
    if st == 200 and isinstance(cur, dict) and cur.get("state") not in IDLE_STATES:
        # 设备还在录（常见于上一轮跑完没停干净）。直接判 FAIL 会让人以为设备坏了，
        # 其实只是脏状态。先尝试停掉再继续测；停不掉才报 FAIL。
        s_st, s_res = _stop_session(api, f"autotest-cleanup-{int(time.time())}")
        if s_st >= 400:
            r.add("E-D-005", "采集启停", "FAIL",
                  f"设备非空闲且停止失败 HTTP {s_st}: {str(s_res)[:150]}", "/api/v2/session")
            return
        st2, cur2 = api.get("/api/v2/session")
        if st2 == 200 and isinstance(cur2, dict) and cur2.get("state") in IDLE_STATES:
            print(f"  ... 已清理残留采集（原 state={cur.get('state')}）")
        else:
            r.add("E-D-005", "采集启停", "FAIL",
                  f"设备非空闲且无法停止 state={(cur2 or {}).get('state') if isinstance(cur2, dict) else cur2}，"
                  f"请手动停止后再测", "/api/v2/session")
            return

    before = _file_index(api)

    cmd_id = f"autotest-{int(time.time())}"
    st, res = api.post("/api/v2/session/start", {"command_id": cmd_id})
    if st >= 400:
        r.add("E-D-005", "采集启停", "FAIL", f"启动失败 HTTP {st}: {str(res)[:150]}", "/api/v2/session/start")
        return
    t0 = time.time()
    try:
        print(f"  ... 采集中 {seconds}s")
        time.sleep(seconds)

        st, s = api.get("/api/v2/session")
        rec = (s or {}).get("recorder") or {}
        print(f"  ... 采集中队列 depth={rec.get('queue_depth')}/{rec.get('queue_capacity')} "
              f"峰值={rec.get('queue_high_watermark')} 丢帧={rec.get('drop_count')}")
    finally:
        # ⚠️ 必须兜住：任何异常/中断都要停采，否则设备会一直留在 recording，
        # 下一轮体检就会误报"当前非空闲"，而且计数还会污染下一轮 DATA-* 判据。实测踩过。
        st2, res2 = _stop_session(api, cmd_id)
        print(f"  ... 已停采 HTTP {st2} {str(res2)[:120]}")

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
    end_state = (s3 or {}).get("state") if isinstance(s3, dict) else None
    stop_txt = f"停采HTTP={st2} 末态state={end_state}"

    # --- 断言 1：真的落盘了新文件，且大小、时长对得上
    if not new_file:
        r.add("E-D-005", "采集启停", "FAIL",
              f"停采后 /api/v2/files 未出现新文件（{stop_txt}）"
              + (f" 停采返回={str(res2)[:120]}" if st2 >= 400 else ""),
              "/api/v2/files")
    else:
        size = new_file.get("size_bytes") or 0
        dur = new_file.get("duration_seconds") or 0.0
        size_ok = size > 100 * 1024
        dur_ok = (0.5 * seconds) <= dur <= (2.0 * seconds + 2)
        r.add("E-D-005", "采集启停", "PASS" if (size_ok and dur_ok) else "FAIL",
              f"新文件 {new_file.get('name')} 大小={size/1048576:.1f}MB 时长={dur:.1f}s "
              f"(期望≈{seconds}s) {stop_txt}", "/api/v2/files")
        # 原来这里还挂了一条 E-L-018「采集启停(Livstudio)」，判据、文件、详情
        # 与 E-D-005 完全相同，纯重复，已删。

    # --- 断言 2/3：各数据流实际采到多少条（这才是在"测"IMU，而不是看配置）
    streams = r3.get("streams") or {}
    if not streams:
        r.add("DATA-STREAMS", "数据流实测", "FAIL", "停采后无任何数据流记录", "/api/v2/session")

    # 设备自报总时长，用于交叉核对"停采真的生效了"
    rec_span = 0.0
    g_st, g_s = api.get("/api/v2/session")
    if g_st == 200 and isinstance(g_s, dict):
        a, b = g_s.get("start_time_ns"), g_s.get("stop_time_ns")
        if a and b:
            rec_span = (b - a) / 1e9

    for topic, v in streams.items():
        if not isinstance(v, dict):
            continue
        cnt = v.get("message_count") or 0
        miss = v.get("missing_count") or 0
        hz = v.get("actual_rate_hz")
        ft, lt = v.get("first_time_ns"), v.get("last_time_ns")
        # ⚠️ 用设备自己的时间跨度，不用本地秒表。理由见函数 docstring。
        span = (lt - ft) / 1e9 if (ft and lt and lt > ft) else elapsed
        short = topic.rsplit("/", 1)[-1]
        exp_hz = expect_imu_hz if "imu" in topic.lower() else expect_cam_hz
        hz_txt = f"{hz}Hz" if hz else "未知"
        if exp_hz:
            exp_cnt = exp_hz * span
            dev = abs(cnt - exp_cnt) / exp_cnt if exp_cnt else 1.0
            hz_dev = abs((hz or 0) - exp_hz) / exp_hz
            ok = miss == 0 and dev <= 0.25 and hz_dev <= 0.05
            r.add(f"DATA-{short}", f"实测 {short}",
                  "PASS" if ok else "FAIL",
                  f"实测 {cnt} 条 / 期望≈{exp_cnt:.0f} ({exp_hz}Hz×{span:.1f}s 设备自报时长) "
                  f"偏差{dev*100:.0f}% 掉帧={miss} 实测频率={hz_txt}"
                  + (f" 频率偏差{hz_dev*100:.1f}%" if hz_dev > 0.05 else ""),
                  "/api/v2/session")
        else:
            r.add(f"DATA-{short}", f"实测 {short}", "PASS" if cnt > 0 and miss == 0 else "FAIL",
                  f"{cnt} 条 掉帧={miss} 实测={hz_txt} 时长={span:.1f}s（未给期望频率，只校验非零）",
                  "/api/v2/session")

    # 停采时长自检：设备说它录了远超我们要求的时间，说明前面有残留采集没清干净，
    # 或者 stop 没生效。这条是给 DATA-* 兜底的 —— 不然条数超标会误导成"数据异常"。
    if rec_span:
        dur_ok = (0.5 * seconds) <= rec_span <= (2.0 * seconds + 3)
        r.add("E-D-008", "采集时长", "PASS" if dur_ok else "WARN",
              f"设备自报 start→stop={rec_span:.1f}s（本地计时 {elapsed:.1f}s，要求 {seconds}s）",
              "/api/v2/session")

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
        f"<tr class='row' data-st='{i['status']}'><td style='background:{color[i['status']][1]};"
        f"color:{color[i['status']][0]};font-weight:500'>{i['status']}</td>"
        f"<td><code>{html.escape(i['id'])}</code></td>"
        f"<td>{html.escape(i['name'])}</td><td>{html.escape(i['detail'])}</td>"
        f"<td><code>{html.escape(i['src'])}</code></td></tr>" for i in r.items)
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Ego-Std API 检查 {html.escape(device)}</title><style>
body{{font:14px/1.6 system-ui,'Segoe UI',sans-serif;margin:0;padding:28px;color:#222;background:#fff}}
h1{{font-size:18px;font-weight:500;margin:0 0 4px}}
.meta{{color:#666;font-size:12px;margin-bottom:14px}}
.cards{{display:flex;gap:12px;margin:0 0 16px}}
.card{{flex:1;border:1px solid #d8d6cf;border-radius:10px;padding:12px 14px}}
.card b{{display:block;font-size:22px;font-weight:500}}
.bar{{margin:0 0 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.bar button{{font:13px/1 system-ui,'Segoe UI',sans-serif;padding:6px 12px;border-radius:7px;
border:1px solid #c9c6bd;background:#fff;color:#333;cursor:pointer}}
.bar button:hover{{background:#f3f1ec}}
.bar button.on{{background:#27500A;color:#fff;border-color:#27500A}}
.bar .hint{{color:#666;font-size:12px;margin-left:4px}}
table{{width:100%;border-collapse:collapse;border:1px solid #d8d6cf;border-radius:10px;overflow:hidden}}
th{{background:#f5f3ee;text-align:left;font-weight:500;padding:9px 10px;border-bottom:1px solid #d8d6cf;font-size:13px}}
td{{padding:9px 10px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px}}
code{{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#f3f1ec;padding:1px 5px;border-radius:4px}}
tr.hide{{display:none}}
.idbar{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 16px}}
.idbox{{flex:1;min-width:170px;border:1px solid #d8d6cf;border-radius:9px;padding:9px 12px;background:#fbfaf7}}
.idbox .k{{color:#666;font-size:11px;margin-bottom:3px}}
.idbox .v{{font-family:ui-monospace,Consolas,monospace;font-size:14px;font-weight:600;color:#14181f;
word-break:break-all}}
</style></head><body>
<h1>LivUmi-Ego-Std 设备体检报告</h1>
<div class="meta">主机 {html.escape(host)} ·
{html.escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</div>
<div class="idbar">
<div class="idbox"><div class="k">设备 SN</div><div class="v">{html.escape(r.meta.get("sn") or "未取到")}</div></div>
<div class="idbox"><div class="k">软件版本</div><div class="v">{html.escape(r.meta.get("version") or "未取到")}</div></div>
<div class="idbox"><div class="k">平台设备 ID</div><div class="v">{html.escape(r.meta.get("ldp_id") or "未取到")}</div></div>
<div class="idbox"><div class="k">相机型号 / 芯片 SN</div><div class="v">{html.escape((r.meta.get("model") or device) + (" · " + r.meta["camera_sn"] if r.meta.get("camera_sn") else ""))}</div></div>
</div>
<div class="cards">
<div class="card"><b>{len(r.items)}</b>检查项</div>
<div class="card"><b style="color:#27500A">{tally['PASS']}</b>通过</div>
<div class="card"><b style="color:#A32D2D">{tally['FAIL']}</b>失败</div>
<div class="card"><b style="color:#BA7517">{tally['WARN']}</b>告警</div>
<div class="card"><b style="color:#666">{tally['SKIP']}</b>跳过</div>
</div>
<div class="bar">
<button data-f="ALL" class="on">全部</button>
<button data-f="PASS">只看过项</button>
<button data-f="FAIL">只看失败</button>
<button data-f="WARN">只看告警</button>
<button data-f="SKIP">只看跳过</button>
<span class="hint">点按钮筛选表格行</span>
</div>
<table><thead><tr><th>结果</th><th>编号</th><th>项目</th><th>详情</th><th>来源</th></tr></thead>
<tbody>{rows}</tbody></table>
<script>
var bs=[].slice.call(document.querySelectorAll('.bar button'));
bs.forEach(function(b){{b.onclick=function(){{
  var f=b.dataset.f;
  bs.forEach(function(x){{x.classList.toggle('on',x===b)}});
  [].slice.call(document.querySelectorAll('tr.row')).forEach(function(tr){{
    tr.classList.toggle('hide', f!=='ALL' && tr.dataset.st!==f);
  }});
}}}});
</script></body></html>"""
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
    ap.add_argument("--model", default="", choices=["", "233", "235"],
                    help="相机型号：233→设备槽位 ego-lite-01，235→ego-std-235。"
                         "会先切槽位再自检（采集中会自动先停采），并按型号校验收分辨率")
    ap.add_argument("--device-id", default="ego-std-235",
                    help="目标逻辑设备 id，用于门禁检查（默认 ego-std-235）")
    ap.add_argument("--account", default="", help="Livstudio 账号，给了才测登录")
    ap.add_argument("--password", default="", help="Livstudio 密码")
    ap.add_argument("--do-collect", action="store_true", help="实测一轮采集启停（会写文件）")
    ap.add_argument("--no-manual", action="store_true", help="跳过人工确认项")
    ap.add_argument("--upload", action="store_true", help="回填飞书表格")
    ap.add_argument("--dry-run", action="store_true")
    # --- 下面三个是给 batch_check.py 批量调度用的，人工单台跑不需要 ---
    ap.add_argument("--json-only", action="store_true",
                    help="只把结果 JSON 打到 stdout，不写 HTML/JSON 报告文件（批量用）")
    ap.add_argument("--quiet", action="store_true",
                    help="吞掉逐项日志并收进 JSON 的 log 字段（批量用）")
    ap.add_argument("--exit-code", action="store_true",
                    help="有 FAIL 项时以退出码 1 结束（批量用）")
    args = ap.parse_args()
    import devip
    args.host = devip.resolve_host(args.host)

    if args.quiet:
        # redirect_stdout 是进程级的，所以只能整体包住检查流程。
        # 批量并发跑时不能靠这个，必须靠子进程隔离（见 batch_check.py）。
        cap = io.StringIO()
        with contextlib.redirect_stdout(cap):
            payload = _run_checks(args)
        payload["log"] = cap.getvalue()
    else:
        payload = _run_checks(args)

    if args.json_only:
        # 保证 stdout 干净：批量端直接 json.loads 这段
        print(json.dumps(payload, ensure_ascii=False))
    else:
        r = _LAST_R[0]
        os.makedirs(OUT, exist_ok=True)
        jp = os.path.join(OUT, f"apicheck_{args.device}.json")
        with open(jp, "w", encoding="utf-8") as f:
            # ⚠️ 原来只写 host/device/at/items，把 meta 整块丢了 ——
            # 也就是说"读 JSON 的人"拿不到 SN 和版本号，只有看 stdout 才有。
            # 报告要的设备身份字段必须落盘。
            json.dump({"host": args.host, "device": args.device,
                       "at": payload["at"], "summary": payload["summary"],
                       "meta": r.meta, "items": r.items},
                      f, ensure_ascii=False, indent=2)
        p = write_report(r, args.host, args.device)
        print(f"\nJSON -> {jp}")
        print(f"报告 -> {p}")
        if args.upload or args.dry_run:
            upload(r, args.dry_run)

    if args.exit_code and payload["summary"]["FAIL"]:
        sys.exit(1)


_LAST_R: list = [None]


def _run_checks(args) -> dict:
    """跑一遍全部检查并返回可 JSON 序列化的结果。拆出来是为了让 main 便于包 redirect_stdout。"""
    api = Api(args.host, args.port)
    print(f"目标 {args.host}:{args.port}  只读体检{'' if args.do_collect else '（不启停采集）'}")
    r = R()
    auto_checks(api, r, args.model, args.do_collect, args.device_id, args.account, args.password)
    manual_checks(r, args.no_manual)
    _LAST_R[0] = r
    return {"host": args.host, "port": args.port, "device": args.device,
            "at": datetime.now().isoformat(timespec="seconds"),
            "summary": summarize(r), "meta": r.meta, "items": r.items}


if __name__ == "__main__":
    main()
