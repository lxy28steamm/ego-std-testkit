"""数据质量与接口一致性增强检查 —— 移植进 Ego-Std 交付测试台。

来源：2026-10-08 在 192.168.50.x 网段实测发现的问题，以及由此沉淀的判定规则。
本模块只做**只读**检查（GET），不改变设备任何状态。

与ego_api_test.py 的关系
------------------------
- `ego_api_test.auto_checks()` 是原有体检项（49 项，含采集启停/IMU/登录等）
- 本模块补的是它**没覆盖**的四类：
  1. 产物假成功（result=complete 但 0 字节 / 仍是 .partial）
  2. 充电链路一致性（status 与电流符号矛盾）
  3. 状态字段自相矛盾（overall=healthy 但下层全 idle 等）
  4. 只读 API 探针（相机型号、多网卡、存储选项、云端心跳等20+ 项）

ID 命名沿用测试台规范：`<类别>-<组号>`，组号避开 E-D-002..008 / E-E-009 / E-L-011..017。

阈值集中在 THRESHOLDS，按需覆盖即可调整判定标准。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

# ==================================================================
# 阈值（唯一权威来源）
# ==================================================================
THRESHOLDS = {
    # 采集流
    "video_rate_hz": 30.0,
    "video_rate_ratio": 0.05,
    "imu_rate_hz": 200.0,
    "imu_rate_ratio": 0.02,
    "missing_ratio_max": 0.001,
    "max_gap_ms": 100.0,

    # 产物
    "file_size_min_bytes": 1024,

    # 存储
    "storage_free_min_gb": 2.0,
    "disk_latency_ms_max": 50.0,

    # 资源
    "cpu_percent_max": 85.0,
    "temp_c_max": 80.0,
    "battery_percent_min": 10.0,
    "battery_mv_min": 13000.0,
    "battery_mv_max": 17500.0,

    # 云端
    "cloud_heartbeat_max_min": 5.0,

    # 长稳
    "service_restart_count_max": 0,
}

# 已知相机型号（实测：.167=ZXCZ_SC233HGS_Dual / .70=YCTC_SC235HGS_MIC）
KNOWN_CAMERA_MODELS = ("ZXCZ_SC233HGS_Dual", "YCTC_SC235HGS_MIC")

# 设计性不可达的接口：配对二维码只在设备 HDMI 屏显示，不应算异常
EXPECTED_UNREACHABLE = {"access_pairing": "配对码仅 HDMI 屏显示，设计如此"}


# ==================================================================
# 判定结果累积器（与 ego_api_test.R 兼容的最小接口）
# ==================================================================
class DQ:
    """收集增强检查项。`r` 可传 ego_api_test.R 实例（自动调r.add）。"""

    def __init__(self, r=None):
        self.r = r                      # 兼容旧套件的 R 对象
        self.own: list[dict] = []

    def add(self, cid: str, name: str, status: str, detail: str, src: str = ""):
        self.own.append({"id": cid, "name": name, "status": status,
                         "detail": detail, "src": src})
        if self.r is not None:
            self.r.add(cid, name, status, detail, src)
        else:
            flag = {"PASS": "+", "FAIL": "x", "WARN": "!", "SKIP": "-"}[status]
            print(f"  [{flag}] {cid} {name:<12} {status:<4} {detail}")


# ==================================================================
# HTTP 工具（显式清空代理，否则局域网请求会被 Clash 7897 劫持）
# ==================================================================
def get_json(base: str, path: str, timeout: float = 6.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + path, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body[:300]
    except urllib.error.HTTPError as e:
        # 注意：403/404 时body 里是 {"detail": "..."}，不能只看有无内容
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


# ==================================================================
# 1. 产物假成功（DQ-100 组）
# ==================================================================
def check_fake_success(base: str, h: dict, dq: DQ, th: dict = THRESHOLDS) -> None:
    """采集标complete 但产物 0 字节 / 仍是 .partial —— 数据交付的致命缺陷。

    实测案例：2026-10-08 11:22 报 state=fault + stream_startup_timeout
    （相机与 IMU 零帧），但 12秒后仍标记「采集完成 size_bytes=2469762」。
    """
    sess = h.get("session") or {}
    rec = h.get("recorder") or {}
    result = sess.get("result") or rec.get("state")

    if result == "complete":
        paths = {
            "session.mcap_path": sess.get("mcap_path"),
            "recorder.final_path": rec.get("final_path"),
            "recorder.partial_path": rec.get("partial_path"),
        }
        paths = {k: v for k, v in paths.items() if v and ".mcap" in str(v)}
        partial = [k for k, v in paths.items() if str(v).endswith(".partial")]
        final = [k for k, v in paths.items() if not str(v).endswith(".partial")]

        if partial and not final:
            dq.add("DQ-101", "产物已收尾", "FAIL",
                   f"complete 但仍指向 .partial：{'、'.join(partial)}；采集未正常收尾",
                   src="health.session+recorder")
        elif final:
            dq.add("DQ-101", "产物已收尾", "PASS", "final路径有效",
                   src="health.session+recorder")
        else:
            dq.add("DQ-101", "产物已收尾", "WARN", "接口未返回产物路径，无法确认",
                   src="health")

        # recorder 状态与产物路径自相矛盾
        if rec.get("state") == "finished" and str(rec.get("final_path", "")).endswith(".partial"):
            dq.add("DQ-102", "recorder收尾一致", "FAIL",
                   "state=finished 但产物为 .partial，状态与路径矛盾",
                   src="health.recorder")
        else:
            dq.add("DQ-102", "recorder收尾一致", "SKIP", "无 finished 态", src="health.recorder")
    elif result in ("aborted", "failed", "interrupted", "error"):
        dq.add("DQ-101", "产物已收尾", "FAIL", f"上次采集异常终止（{result}）",
               src="health.session")
    else:
        dq.add("DQ-101", "产物已收尾", "SKIP", f"无完成态采集（{result or '无'}）",
               src="health.session")

    # 流数据有效性：标完成但流无数据 = 假成功核心特征
    streams = rec.get("streams") or {}
    if result == "complete" and streams:
        for topic, st in streams.items():
            short = topic.split("/")[-3] if len(topic.split("/")) >= 3 else topic
            cnt = st.get("message_count", 0)
            rate = st.get("actual_rate_hz")
            if cnt == 0 or not rate:
                dq.add("DQ-103", f"{short} 有数据", "FAIL",
                       "complete 但该流零消息 —— 假成功典型特征", src="health.recorder")
            else:
                dq.add("DQ-103", f"{short} 有数据", "PASS",
                       f"{cnt} 条 / {rate}Hz", src="health.recorder")
    elif not streams:
        dq.add("DQ-103", "流数据有效性", "SKIP", "无流数据（未采集）", src="health.recorder")


# ==================================================================
# 2. 充电链路一致性（DQ-200 组）
# ==================================================================
def check_battery(h: dict, dq: DQ, th: dict = THRESHOLDS) -> None:
    """供电是数采设备命脉——采集中掉电会毁掉整段数据。

    实测四台基线：
        .70  53% fast_charging 15633mV  +972mA
        .131  5% fast_charging 13943mV +1435mA
        .138 63% idle          15408mV  -370mA
        .167 94% idle          16485mV  -406mA
    **电流符号是充电/放电的真判据**：正=流入，负=流出。
    """
    bat = (h.get("host") or {}).get("battery") or {}
    if not bat:
        dq.add("DQ-200", "电池信息", "SKIP", "health 无 battery 段", src="health.host")
        return

    if not bat.get("present"):
        dq.add("DQ-200", "UPS 存在", "WARN", "未检测到 UPS Hat，供电保障未知",
               src="health.host.battery")
        return
    dq.add("DQ-200", "UPS 存在", "PASS", f"source={bat.get('source', '-')}",
           src="health.host.battery")

    pct = bat.get("percent")
    st = str(bat.get("status") or "")
    ma = bat.get("current_ma")
    charging = bool(bat.get("charging")) or st.startswith("charg")

    if pct is not None:
        # 低电量但在充电 = 正常补电，不判 FAIL
        if charging and pct < th["battery_percent_min"]:
            dq.add("DQ-201", "UPS 电量", "WARN",
                   f"{pct}%（充电中，{st}）—— 低电量但在补电，非故障",
                   src="health.host.battery")
        else:
            dq.add("DQ-201", "UPS 电量", "PASS" if pct >= th["battery_percent_min"] else "FAIL",
                   f"{pct}% / {st}", src="health.host.battery")

    # status 与电流符号交叉 —— 抓「插着充电器却在放电」
    if ma is not None:
        sign = 1 if ma > 0 else (-1 if ma < 0 else 0)
        if sign > 0 and not charging:
            dq.add("DQ-202", "充电状态一致", "WARN",
                   f"电流 +{ma}mA 流入但 status={st}未标记充电", src="health.host.battery")
        elif sign < 0 and charging:
            dq.add("DQ-202", "充电状态一致", "FAIL",
                   f"status={st} 说在充电，但电流 {ma}mA（实际放电）—— "
                   "充电链路异常，查充电器或 UPS Hat", src="health.host.battery")
        elif sign > 0:
            dq.add("DQ-202", "充电状态一致", "PASS", f"+{ma}mA（{st}）",
                   src="health.host.battery")
        elif sign < 0:
            dq.add("DQ-202", "充电状态一致", "PASS", f"{ma}mA（{st or '放电'}）",
                   src="health.host.battery")
        else:
            dq.add("DQ-202", "充电状态一致", "SKIP", "电流 0mA", src="health.host.battery")

    mv = bat.get("voltage_mv")
    if mv:
        ok = th["battery_mv_min"] <= mv <= th["battery_mv_max"]
        dq.add("DQ-203", "电池电压", "PASS" if ok else "FAIL",
               f"{mv}mV（有效 {th['battery_mv_min']}~{th['battery_mv_max']}mV）"
               + ("" if ok else " —— 欠压或过充"),
               src="health.host.battery")


# ==================================================================
# 3. 状态字段自相矛盾（DQ-300 组）
# ==================================================================
def check_consistency(h: dict, dq: DQ) -> None:
    """状态字段不可信是今天最反复的坑——多处接口各说各话。

    实测踩过的：
    1. overall_state=healthy 但所有 worker hardware_connected=false
       → 若原因是 hardware_not_detected（没插设备）则属正常待机，不能判FAIL
    2. device-selection 报 calibration_state=not_checked
       而 health 报 valid → 设备切换期瞬时状态，连续采样3 次确认非缺陷
    """
    workers = h.get("workers") or {}
    overall = h.get("overall_state")

    if not workers:
        dq.add("DQ-301", "健康聚合一致", "WARN", "health 未返回任何 worker",
               src="health")
        return

    waiting = all((not w.get("hardware_connected"))
                  and "hardware_not_detected" in (w.get("error_code") or "")
                  for w in workers.values())
    all_idle = all((not w.get("hardware_connected"))
                   or w.get("pipeline_state") != "ready"
                   for w in workers.values())

    if overall == "healthy" and waiting:
        dq.add("DQ-301", "健康聚合一致", "WARN",
               "overall=healthy 但下层均在等待硬件接入 —— "
               "未插设备时属合理，当前不可测", src="health")
    elif overall == "healthy" and all_idle:
        dq.add("DQ-301", "健康聚合一致", "FAIL",
               "overall=healthy 但下层全 idle 且非等待接入 —— "
               "**聚合状态字段未聚合下层，overall_state 不可单独作为放行依据**",
               src="health")
    else:
        dq.add("DQ-301", "健康聚合一致", "PASS", f"overall={overall}", src="health")

    # worker 层交叉：设备就绪但标定无效 = 数据不可用于训练
    for w in workers.values():
        dev = w.get("device_id", "?")
        if w.get("pipeline_state") == "ready" and w.get("hardware_connected"):
            cal = w.get("calibration_state")
            if cal and cal != "valid":
                dq.add("DQ-302", f"{dev} 标定", "FAIL",
                       f"已就绪但 calibration_state={cal}，采集数据不可用于训练",
                       src="health.workers")
            elif cal == "valid":
                dq.add("DQ-302", f"{dev} 标定", "PASS", "valid", src="health.workers")
            rc = w.get("restart_count", 0)
            dq.add("DQ-303", f"{dev} worker重启", "PASS" if rc == 0 else "WARN",
                   f"restart_count={rc}" + ("" if rc == 0 else " —— 曾崩溃，可能伴随数据断流"),
                   src="health.workers")

    # 未接入设备的标定不该判 FAIL（slots 里 5 个槽位多数是 not_checked）
    inactive = [w for w in workers.values()
                if not w.get("hardware_connected")]
    if inactive:
        dq.add("DQ-304", "未接入设备处理", "SKIP",
               f"{len(inactive)} 个worker 未接入硬件，其标定/流水线不检查",
               src="health.workers")


# ==================================================================
# 4. 数据流质量（DQ-400 组）
# ==================================================================
def check_streams(h: dict, dq: DQ, th: dict = THRESHOLDS) -> None:
    rec = h.get("recorder") or {}
    streams = rec.get("streams") or {}
    if not streams:
        dq.add("DQ-400", "数据流实测", "SKIP", "无流数据（未采集）", src="health.recorder")
    else:
        for topic, st in streams.items():
            short = topic.split("/")[-3] if len(topic.split("/")) >= 3 else topic
            cnt = st.get("message_count", 0)
            miss = st.get("missing_count", 0)
            rate = st.get("actual_rate_hz")
            if "imu" in topic:
                nom, tol = th["imu_rate_hz"], th["imu_rate_ratio"]
            else:
                nom, tol = th["video_rate_hz"], th["video_rate_ratio"]
            if not rate or cnt == 0:
                continue# 已由 DQ-103 报过，不重复
            lo, hi = nom * (1 - tol), nom * (1 + tol)
            dq.add("DQ-401", f"{short} 帧率",
                   "PASS" if lo <= rate <= hi else "FAIL",
                   f"{rate:.2f}Hz（标称 {nom}Hz，允许 {lo:.1f}~{hi:.1f}）",
                   src="health.recorder")
            mr = (miss / cnt) if cnt else 0
            dq.add("DQ-402", f"{short} 丢帧",
                   "PASS" if mr <= th["missing_ratio_max"] else "FAIL",
                   f"{miss}/{cnt} = {mr:.3%}（阈值 {th['missing_ratio_max']:.2%}）",
                   src="health.recorder")

    # 写盘链路
    drop = rec.get("drop_count", 0)
    rej = rec.get("writer_rejected_count", 0)
    canc = rec.get("writer_canceled_count", 0)
    dq.add("DQ-403", "无丢帧/拒写",
           "PASS" if (drop == 0 and rej == 0) else "FAIL",
           f"drop={drop} rejected={rej} canceled={canc}"
           + ("" if drop == 0 or rej == 0 else " —— 队列溢出或写线程拒写"),
           src="health.recorder")
    lat = rec.get("disk_latency_ms")
    if lat is not None:
        ok = lat <= th["disk_latency_ms_max"]
        dq.add("DQ-404", "写盘延迟", "PASS" if ok else "FAIL",
               f"{lat:.2f}ms（阈值 {th['disk_latency_ms_max']}ms）",
               src="health.recorder")


# ==================================================================
# 5. 只读 API 探针（DQ-500 组）
# ==================================================================
PROBES = [
    ("camera_devices", "/api/v2/camera/stereo-uvc/devices", "相机设备枚举"),
    ("umi_devices",    "/api/v2/umi/serial-devices",       "UMI 串口设备"),
    ("device_selection", "/api/v2/device-selection",      "设备选择状态"),
    ("storage_options", "/api/v2/storage",                "存储选项"),
    ("network",        "/api/v2/network",                 "网络状态"),
    ("cloud",          "/api/v2/cloud",                   "云端连接"),
    ("session",        "/api/v2/session",                 "采集会话"),
    ("camera_settings", "/api/v2/camera/settings",        "相机参数"),
]


def check_api_probes(base: str, h: dict, dq: DQ, th: dict = THRESHOLDS) -> None:
    """8 个只读 GET 接口。全部无副作用，不碰 preview（会开视频流）。"""
    data = {}
    for key, path, label in PROBES:
        st, body = get_json(base, path)
        data[key] = {"ok": st == 200, "status": st, "body": body, "label": label}

    # ---- 相机型号与兼容性
    c = data.get("camera_devices")
    if c and c["ok"] and isinstance(c["body"], list):
        if not c["body"]:
            waiting = any("hardware_not_detected" in (w.get("error_code") or "")
                          for w in (h.get("workers") or {}).values())
            dq.add("DQ-500", "相机枚举",
                   "WARN" if waiting else "FAIL",
                   "未枚举到相机" + ("（设备等待接入，正常）" if waiting
                                     else "且无等待态 —— 采集无法进行"),
                   src="camera/stereo-uvc/devices")
        else:
            models = []
            for d in c["body"]:
                p = str(d.get("device_path") or "")
                # 真实path： usb-ZXCZ_ZXCZ_SC233HGS_Dual_064014231235-video-index0
                # 厂商名重复（ZXCZ_ZXCZ_...），需去掉重复前缀。
                # 做法：取 usb- 之后、序列号（长数字）之前的那段，再折叠相邻重复词。
                name = p.split("/")[-1]
                if name.startswith("usb-"):
                    name = name[4:]
                seg = re.split(r"_\d{6,}", name)[0]        # 去掉尾部序列号
                parts = seg.split("_")
                folded = [parts[0]]
                for w in parts[1:]:
                    # 厂商名重复（ZXCZ_ZXCZ_）→ 跳过与前一个相同的词
                    if w and w != folded[-1]:
                        folded.append(w)
                models.append("_".join(folded) or seg or name[:24])
            uniq = sorted(set(models))
            known = uniq[0] in KNOWN_CAMERA_MODELS
            dq.add("DQ-500", "相机枚举",
                   "PASS" if known and len(uniq) == 1 else "WARN",
                   f"{'、'.join(uniq)}（已知型号 {'/'.join(KNOWN_CAMERA_MODELS)}）"
                   + ("" if known else " —— 新型号，建议与开发确认兼容性"),
                   src="camera/stereo-uvc/devices")
            nr = [d.get("label", "?") for d in c["body"] if not d.get("ready")]
            dq.add("DQ-501", "相机就绪", "FAIL" if nr else "PASS",
                   f"{len(nr)} 个未就绪" if nr else f"全部就绪（{len(c['body'])} 路）",
                   src="camera/stereo-uvc/devices")
    else:
        dq.add("DQ-500", "相机枚举", "WARN",
               f"接口不可用（HTTP {c['status'] if c else '?'}）", src="camera")

    # ---- 存储选项
    # ⚠️ 实测坑：current 返回的是**路径**（/home/ls/.../data），
    #而 options[].storage_id 是 local。必须两种都试匹配。
    s = data.get("storage_options")
    if s and s["ok"] and isinstance(s["body"], dict):
        opts = s["body"].get("options") or []
        cur = s["body"].get("current") or ""
        co = next((o for o in opts
                   if cur and (o.get("storage_id") == cur or o.get("path") == cur)), None)
        if co is None:
            dq.add("DQ-510", "存储选项", "FAIL",
                   f"current={cur} 匹配不到 option（可能 storage_id/path 字段变了）",
                   src="storage")
        else:
            free = co.get("free_bytes") or 0
            fgb = free / 1024**3
            dq.add("DQ-510", "存储选项",
                   "PASS" if co.get("available") else "FAIL",
                   f"{co.get('label') or co.get('storage_id')} "
                   f"available={co.get('available')} free={fgb:.1f}GB",
                   src="storage")
            if fgb < th["storage_free_min_gb"]:
                dq.add("DQ-511", "存储剩余", "FAIL", f"{fgb:.1f}GB < "
                       f"{th['storage_free_min_gb']}GB", src="storage")
    else:
        dq.add("DQ-510", "存储选项", "WARN", "接口不可用", src="storage")

    # ---- 网络（多网卡是访问不通的常见原因）
    n = data.get("network")
    if n and n["ok"] and isinstance(n["body"], dict):
        b = n["body"]
        if not b.get("connected"):
            dq.add("DQ-520", "网络状态", "FAIL", f"{b.get('state')} —— 设备未联网",
                   src="network")
        else:
            dq.add("DQ-520", "网络状态", "PASS",
                   f"{b.get('interface')} / {b.get('ssid')} / {b.get('ip_address')}",
                   src="network")
            ifs = b.get("interfaces") or []
            avail = [i for i in ifs if i.get("connected")]
            un = [f"{i.get('interface')}({i.get('connection_type')})"
                  for i in ifs if not i.get("connected")]
            if avail and un:
                dq.add("DQ-521", "多网卡提示", "WARN",
                       f"当前走 {avail[0].get('interface')}（"
                       f"{avail[0].get('ip_address')}）；{'、'.join(un)} 未连接 —— "
                       "**访问设备必须用上面的 IP**", src="network")
    else:
        dq.add("DQ-520", "网络状态", "WARN", "接口不可用", src="network")

    # ---- 云端
    cl = data.get("cloud")
    if cl and cl["ok"] and isinstance(cl["body"], dict):
        b = cl["body"]
        dq.add("DQ-530", "云端连接",
               "PASS" if b.get("state") == "online" else "FAIL",
               f"{b.get('state')}" + ("" if b.get("state") == "online"
                                      else " —— 无法上传/同步"),
               src="cloud")
        last = b.get("last_success_time_ns") or 0
        if last:
            age = (time.time_ns() - last) / 1e9 / 60
            dq.add("DQ-531", "云端心跳",
                   "PASS" if age <= th["cloud_heartbeat_max_min"] else "WARN",
                   f"{age:.1f}min 前（阈值 {th['cloud_heartbeat_max_min']}min）",
                   src="cloud")
        if b.get("error_code"):
            dq.add("DQ-532", "云端错误", "FAIL",
                   f"{b.get('error_code')} {b.get('error_message', '')}"[:70], src="cloud")
    else:
        dq.add("DQ-530", "云端连接", "WARN", "接口不可用", src="cloud")


# ==================================================================
# 入口
# ==================================================================
def run_all(base: str, health: dict, r=None, th: dict = THRESHOLDS) -> DQ:
    """一次性跑完所有增强检查。base形如 http://192.168.50.167:18000。

    用法（接旧套件）：
        import dq_extra
        dq_extra.run_all(base, health, r)     # r 是 ego_api_test.R 实例
    """
    dq = DQ(r)
    h = health if isinstance(health, dict) else {}

    check_fake_success(base, h, dq, th)
    check_battery(h, dq, th)
    check_consistency(h, dq)
    check_streams(h, dq, th)
    check_api_probes(base, h, dq, th)

    return dq


if __name__ == "__main__":
    # 自测：python dq_extra.py <ip> [port]
    import sys
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.50.167"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 18000
    base = f"http://{ip}:{port}"
    st, hh = get_json(base, "/api/v2/health")
    if st != 200:
        print(f"拉取 health 失败：HTTP {st} {str(hh)[:80]}")
        raise SystemExit(1)
    from collections import Counter
    d = run_all(base, hh)
    c = Counter(i["status"] for i in d.own)
    print("\n" + "=" * 70)
    print("增强检查汇总：" + "  ".join(f"{k}={c.get(k, 0)}" for k in
                                    ("PASS", "FAIL", "WARN", "SKIP")))
    verdict = "FAIL" if c.get("FAIL") else ("WARN" if c.get("WARN") else "PASS")
    print(f"判定={verdict}")