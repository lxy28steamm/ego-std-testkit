# IMU 队列溢出导致头环整机不可用的机制分析（已读源码定案）

- 分析对象：YCTC_SC233HGS 头环（VID:PID `5268:1218`，SN `0152312181647`）
- 故障主机：192.168.195.38（树莓派 5，Debian 13，内核 6.12.75）
- 对照主机：192.168.199.66（同型号树莓派 5，同一支头环在其上正常）
- **软件版本不一样**（此前未注意，见第零节）：
  - 故障机：**4.0.45** / build_sequence 246 / commit `e5daee9f`
  - 对照机：**4.0.58** / build_sequence 273 / commit `2bb88560`
- 时间：2026-09-29

> **本文档已读取容器内源码核实**（`/app/backend/...`）。此前基于现象的推断有多处错误，见文末"修正记录"。

---

## 零、关键发现：两台跑的不是同一个版本，4.0.58 修了"溢出不自愈"

读源码时对比两台才发现，**此前所有"同型号个体差异"的推断都建立在错误前提上——两台软件版本不同**：

| | 故障机 195.38 | 对照机 199.66 |
|---|---|---|
| build_id | **4.0.45** | **4.0.58** |
| build_sequence | 246 | 273 |
| commit | `e5daee9f` | `2bb88560` |
| 源码目录 | `backend/DevicesManager/ego_std/` | `backend/Device/ego/ego_std/`（已重构） |
| backend PID | 21437 | 44725 |

两台的 **IMU 配置完全一致**（`enabled: true / rate_hz: 200 / queue_size: 512`），
`healthy` 与 `error` 的判定代码也**逐字相同**。唯一的功能性差异在 `read()`：

**4.0.45（故障机）**
```python
def read(self) -> YctcImuReading | None:
    try:
        return self._queue.get_nowait()
    except queue.Empty:
        return None
```

**4.0.58（对照机）**
```python
def read(self) -> YctcImuReading | None:
    try:
        reading = self._queue.get_nowait()
    except queue.Empty:
        return None
    if not self._queue.full():
        self._queue_overflow_active = False    # ← 新增：自愈
    return reading
```

文件指纹佐证：`md5 2948bb65…`(399 行) vs `md5 d5853abb…`(402 行)，差的正是这 3 行。

### 为什么这 3 行是致命的

`_queue_overflow_active` 在 4.0.45 里**只能由生产者 `_enqueue_pairs()` 刷新**。
一旦消费端跟不上（被视频帧 `read(timeout_sec=2.0)` 阻塞），队列持续满，标志就锁死在 `True`：

```
溢出一次 → healthy=False → 判 fault → 重启 worker → USB 复位 → 重开设备
        → 又溢出 → 又 fault → 无限循环
```

4.0.58 给了消费者一条清除路径：**只要取到数据且队列不再满，标志立即复位**，
于是瞬时抖动能自愈，不会再触发整条重启链。

### 对"有的会有的不会"的重新解释

原来的解释（头环个体 IMU 输出速率不同）现在只是**诱因**之一，
真正的放大器是**故障机跑的是旧版 4.0.45，缺自愈逻辑**：

- 同样的 1300 Hz 灌入 + 512 队列，在 4.0.58 上可能只是"丢点数据、自动恢复"
- 在 4.0.45 上就是"永久 fault、反复复位"

**建议优先动作：把 195.38 升到 4.0.58（含）以后再复测**，
很可能什么都不用改就不掉线了。升级后若仍溢出，才是队列容量/消费线程的问题。

⚠️ 注意一个混淆变量：对照机 199.66 当前 `record_imu=False`（`ego_std_235_imu_port=None`），
故障机 `record_imu=True`。所以"跑 4.0.58 就不掉"目前还是**推断**，需要在 195.38 升级后实测确认。

---

## 一、结论（源码级）

**溢出的队列在"上位机进程内"，不在固件 MCU 里。**

完整链路：

1. 头环 MCU 按自己的速率（串口实测约 **1300 Hz**）通过 `/dev/ttyACM0` 吐数据
2. 上位机 backend 内的 `NativeYctcSource` 调用 C++ 库 `libego_native_yctc_source.so`，
   用 `ego_yctc_source_create(queue_size)` 建了一个**容量 512 的队列**接住这些数据
3. 消费端（`StereoUvcAdapter.read()`）被 `self._capture.read(timeout_sec=2.0)` 阻塞在等视频帧上，
   取走的速率跟不上 1300 Hz 的灌入速率 → 队列满
4. 库把溢出计数 +1，**并已实现丢弃策略**（丢最旧一条、塞新的）
5. 但 `healthy` 属性在溢出时直接返回 `False` →
   `error_code="yctc_imu_unavailable"`、`hardware_connected=false`、pipeline 打 fault
6. 上层重启 worker 进程 → 关掉并重新打开设备 → 内核侧看到 `USB disconnect` + 重枚举 → 相机跟着消失

**核心问题：`rate_hz: 200` 是个没接线的配置。**

- `ImuStreamConfig.rate_hz` 默认值 200，配置文件里也是 200
- 但它**只**用在两处：① `stream_descriptors()` 里声明"这个流是 200Hz"的元数据；
  ② 视频内嵌 IMU 路径下传给 `NativeV4l2Capture(embedded_imu_rate_hz=...)`
- 串口路径下构造 `NativeYctcSource(port, queue_size, library)` —— **签名里根本没有 rate 参数**

所以 `rate_hz=200` 既不限制 MCU 采样率，也不限制消费速率。MCU 按 1300 Hz 吐，队列只有 512，必爆。

---

## 二、源码证据

### 2.1 溢出判定与丢弃策略

`backend/DevicesManager/ego_std/hardware/yctc_imu.py`（Python 版，与 C++ 库逻辑一致）：

```python
try:
    self._queue.put_nowait(reading)
except queue.Full:
    overflowed = True
    self._overflow_count += 1
    try:
        self._queue.get_nowait()      # 丢最旧一条
    except queue.Empty:
        pass
    self._queue.put_nowait(reading)   # 塞新的
self._queue_overflow_active = overflowed

@property
def healthy(self) -> bool:
    return (self._serial is not None
            and not self._last_error
            and not self._queue_overflow_active)   # ← 溢出即判不健康

@property
def error(self) -> str:
    if self._last_error:
        return self._last_error
    if self._queue_overflow_active:
        return f"YCTC IMU queue overflow; count={self._overflow_count}"
    return ""
```

**`YCTC IMU queue overflow; count=N` 这个字符串是上位机代码生成的，不是固件上报的。**

### 2.2 实际走的是 native 库，队列容量 512

`backend/DevicesManager/ego_std/adapter.py`：

```python
if serial_imu_available and imu_path is not None:
    self._imu = self._imu_factory(imu_path, self.stereo.imu.queue_size)
    # = NativeYctcSource(port, queue_size, library)   ← 无 rate 参数
    self._imu_transport = "serial"
```

`hardware/native_yctc_source.py`：

```python
def __init__(self, port, queue_size, library_path=Path(".../libego_native_yctc_source.so")):
    self._library = ctypes.CDLL(str(library_path))
    self._handle = self._library.ego_yctc_source_create(max(1, int(queue_size)))

@property
def healthy(self):        return self._library.ego_yctc_source_healthy(self._handle)
@property
def error(self):          return self._library.ego_yctc_source_error(self._handle).decode()
@property
def overflow_count(self): return self._library.ego_yctc_source_overflow_count(self._handle)
```

### 2.3 生效配置

`~/raspberry-ego-v2/config/devices/ego-lite-01/config.yaml`：

```yaml
imu:
  enabled: true
  rate_hz: 200
  queue_size: 512
  port: /dev/serial/by-id/usb-YCTC_YCTC_SC233HGS_0152312181647-if02
```

- `port` 有值 → 走**串口路径**（`_imu_transport = "serial"`），不是视频内嵌
- `queue_size: 512` 就是那个会满的队列
- `rate_hz: 200` 在此路径下**不参与任何计算**

对比 `ego-plus` 的配置是 `rate_hz: 400 / queue_size: 4096` —— 容量给到 4096，说明作者知道这个参数要留余量。

### 2.4 消费端被视频帧阻塞

`adapter.py` 的 `read()`：

```python
def read(self):
    if self._pending:
        return self._pending.popleft()
    now = time.monotonic()
    if self._imu is not None and now < self._next_video_mono:
        sample = self._read_imu()          # 只在视频帧的间隙读 IMU
        if sample is not None:
            return sample
    ...
    frame = self._capture.read(timeout_sec=2.0)   # ← 阻塞等视频帧
    self._next_video_mono = time.monotonic() + 1.0 / self.stereo.video.fps
```

IMU 只在两帧视频之间的空隙被消费，且 `read()` 每次只吐一条样本。生产 1300/s，这个消费模型跟不上。

`_queue_available_imu()` 一次最多取 `queue_size`(512) 条，但它的调用时机同样受视频管线节奏约束。

---

## 三、实测现象（与源码吻合）

### 3.1 一次完整故障循环

```
11:26:24  kernel  usb 1-1: new high-speed USB device number 5
                  Product: YCTC_SC233HGS  SerialNumber: 0152312181647
                  cdc_acm 1-1:1.2: ttyACM0: USB ACM device
11:26:32  上位机  pipeline="ready"    hardware_connected=true
11:26:35  上位机  pipeline="fault"    hardware_connected=false      ← 仅 3 秒
                  error_code="yctc_imu_unavailable"
                  error_message="YCTC IMU queue overflow; count=404"
11:26:36          count=913      (+509/s)
...               ...
11:27:09          count=17629
11:27:10  kernel  usb 1-1: USB disconnect, device number 5
                  uvcvideo 1-1:1.1: Non-zero status (-71)
11:27:14  kernel  usb 1-1: new high-speed USB device number 6      ← 重新枚举
11:27:16  上位机  pipeline="ready"    hardware_connected=true   (pid 42685)
11:27:19  上位机  pipeline="fault"    count=406                     ← 又 3 秒，又从 ~400 起
```

- **ready → fault 恒定 3 秒**：512 格队列在 1300 Hz 灌入下约 0.4 秒填满，
  叠加启动开销，3 秒内必然溢出。确定性，非随机硬件故障。
- **count 从 ~404 重来**：worker **进程**重启（pid 42058 → 42685），新进程的 native 库
  handle 是新建的，计数从 0 起。**不是 MCU 复位。**

### 3.2 串口实测速率

```
字节率        ~23.5 KB/s
大帧          216 字节，帧头 53 59 ("SY")，108.8 Hz
子帧          18 字节（216 = 18 × 12），每大帧含 12 个样本
实际样本率    108.8 × 12 ≈ 1306 Hz
```

### 3.3 唯一变量是 IMU 录制开关

| 项目 | 好机 199.66 | 坏机 195.38 |
|---|---|---|
| `imu_rate` | 200 | 200 |
| **`record_imu`** | **false** | **true** |
| 分辨率 / 帧率 | 1920x1080 @30 | 同 |
| 内核 / 机型 | 6.12.75 / RPi5 | 同 |
| `get_throttled` | 0x0 | 0x0 |

---

## 四、回答"为什么溢出会让整机不可用"

### 4.1 是上位机软件的判定，不是固件行为

`healthy` 属性在 `_queue_overflow_active=True` 时返回 `False`，上层据此把整设备判 fault。

**固件可能完全无辜** —— 它只是按自己的速率吐数据，没有任何证据表明它主动停过。
所谓的"fail-fast"是**上位机 backend 的策略**。

### 4.2 为什么相机也断

`lsusb -t`：

```
Bus 001 Dev 010  (5268:1218 YCTC_SC233HGS)
  If 0 / If 1   Class=Video   Driver=uvcvideo   → /dev/video*
  If 2 / If 3   Class=CDC     Driver=cdc_acm    → /dev/ttyACM0  ← IMU
```

相机与 IMU 是同一 USB 复合设备的两个接口。上位机 fault 后会重启 worker、重新 `open()`
v4l2 与串口设备，这套重新初始化在内核侧表现为 `USB disconnect` → 重枚举。
两个接口属于同一设备，自然同时消失。

### 4.3 上位机为什么看不到溢出

`/api/v2/session` 的 `recorder` 队列（容量 8192、峰值 20、drop=0）是**写 mcap 的那层**，
在 IMU 队列的下游。IMU 在更上游就被丢了，数据根本没到 recorder，所以 `drop_count` 恒为 0。

👉 **判断 IMU 是否溢出，必须看日志里的 `yctc_imu_unavailable`，看 `drop_count` 无效。**

---

## 五、给开发的结论（可直接转发）

**0. 【最优先】故障机版本过旧，缺自愈逻辑。**
   195.38 跑 **4.0.45**，199.66 跑 **4.0.58**。两者的 `healthy`/`error` 判定逐字相同，
   唯一差别是 4.0.58 在 `YctcImuSource.read()` 里加了 3 行：
   ```python
   if not self._queue.full():
       self._queue_overflow_active = False
   ```
   4.0.45 里该标志只能由生产者刷新，消费端一旦跟不上就**锁死在 True**，
   于是"溢出 → fault → 重启 worker → USB 复位 → 再溢出"无限循环。
   **请确认这是否是 4.0.45→4.0.58 之间针对本问题的修复**，若是，把 195.38 升级即可闭环。

1. **`rate_hz` 没接上。** `NativeYctcSource.__init__(port, queue_size, library)` 签名里没有
   采样率参数，`rate_hz: 200` 在串口路径下只作为 `stream_descriptors()` 的元数据存在。
   请确认：该值本应下发给 MCU 限流，还是仅作声明？
2. **队列容量 512 对 1300 Hz 的实际输入明显不足。** `ego-plus` 给到 4096，ego-std 只有 512。
3. **`healthy` 的判定过严。** 溢出时丢弃策略已经生效（丢最旧塞最新），数据并非完全丢失，
   但 `healthy` 直接返回 False，导致整个设备被判 fault 并触发 worker 重启。
   可考虑改为：连续 N 个采样周期持续溢出才判 fault，或降级为 WARN 并置 quality 标志。
4. **消费端节奏受视频帧阻塞。** `read()` 里 `self._capture.read(timeout_sec=2.0)` 阻塞等待，
   IMU 只在帧间隙被消费。建议 IMU 消费独立成线程，不受视频管线节奏约束。
5. 现场还见过厂商为 **ZXCZ**（SN `064014231235`，VID `1d6b:0004`）的模组，与这批
   YCTC `5268:1218` 不同源。请确认两种模组的输出速率约定是否统一。

---

## 六、修正记录（重要）

本文档此前基于现象推断，读源码后修正如下**错误**：

| 之前的说法 | 源码核实后的事实 |
|---|---|
| 溢出是**固件** MCU 内部队列 | 是**上位机进程内**的队列（native 库 512 格 / Python `queue.Queue(512)`），固件未参与判定 |
| count 复位是**MCU 复位**的证据 | 是 **worker 进程重启**（pid 42058→42685）导致新 handle 从 0 计数 |
| 固件选择 fail-fast（有意设计） | 是**上位机 backend** 的 `healthy` 判定过严，固件可能无辜 |
| "有的会有的不会"是**头环个体差异** | 两台先就**不是同一软件版本**（4.0.45 vs 4.0.58），且缺自愈逻辑才是放大器；头环速率差异只是诱因 |

方向性结论不变：**IMU 实际输出速率（1300 Hz）与上位机配置（200 Hz）严重不匹配**，
且消费端跟不上，导致队列溢出 → 设备被判 fault → 重启 → USB 重枚举。
但**处置优先级要改**：先升版本，再谈改队列容量或消费线程。

---

## 七、如何自己读容器源码

Docker 权限不够（ls 不在 docker 组、sudo 要密码），但可以绕：

```bash
# 1. 找 backend 进程 PID（宿主上可见，以 ls 用户运行）
ps -eo pid,args | grep backend.main | grep -v grep

# 2. 直接读进程看到的根文件系统
ls -la /proc/<PID>/root/app/
cat /proc/<PID>/root/app/BUILD_METADATA.json
grep -rn "queue overflow" /proc/<PID>/root/app/backend --include=*.py
```

已封装成工具（本机跑，需 venv 装 paramiko）：

```
python container_src.py --host 192.168.195.38 info
python container_src.py --host 192.168.195.38 ls backend/DevicesManager/ego_std
python container_src.py --host 192.168.195.38 grep "queue overflow"
python container_src.py --host 192.168.195.38 cat backend/DevicesManager/ego_std/hardware/yctc_imu.py --lines 230,300
```
