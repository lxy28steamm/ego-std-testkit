"""一键采传 —— 选任务 → 采集 → 验盘 → 上传 → 校验，一条命令跑完。

对应设备端 /remote 页面上人工点的那套动作。全链路最关键的是**顺序**：
    登录 → 选任务 → 采集 → 验盘 → 上传 → 校验
不是"先采集再绑任务"——bind 是目录重命名语义，事后补绑必然撞名（见下）。

用法：
    python collect_and_upload.py --host 192.168.50.30 --seconds 5 \
        --account 18855552004

    # 不传 --host 就复用 devip.py 的自动发现
    python collect_and_upload.py --seconds 5 --account 18855552004

    # 只读跑一遍（不采集不上传），先看设备/任务状态
    python collect_and_upload.py --account 18855552004 --dry-run

    # 只采集不上传
    python collect_and_upload.py --seconds 5 --skip-upload

实测踩过的坑（代码里都标了 ⚠️）：
  1. ⚠️⚠️ 必须"先选任务再采集"，不能"先采集再 bind-assigned"。
     bind-assigned 是**目录重命名**：把 2026-10-03-2583-C97 改名成 20261003T2102。
     assigned 任务一天只有一个目录，同名目录已存在就必然 422：
       COLLECTION_TASK_INVALID: 目标任务目录"20261003T2102"已存在，无法完成目录重命名
     正解是 PUT /collection-task/assigned/select {taskId} 之后直接 POST /session/start，
     采集目录会直接落在 storage_name（20261003T2102）下，压根不用 bind。
  2. select 之后要重新读 session 确认 task_source=assigned，才说明任务真绑上了。
  3. ⚠️ upload_category 从 /files 或 /task-folders 里抄，别自己猜
     （合法值：ego / ego_stereo / ego_plus / dex）。
  4. ⚠️ paths 只接受**目录级**，传文件级 path 会 400。
     所以默认上传完删本地（delete_after_upload=true），否则目录越堆越大，
     每次上传都把历史文件重传一遍（实测同一目录 53→86→100→130MB 递增，纯浪费带宽）。
  5. ⚠️ result=complete 不代表文件真的落盘了，必须 SSH stat 验真实大小。
     实测遇到过接口报 complete、日志写 size_bytes=3MB、实际目录 0 字节的假成功。
  6. 命令行传 --password 会留在 shell 历史里，建议省略走交互式输入。
"""


import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import devip  # noqa: E402  复用 IP 自动发现

API_PORT = devip.API_PORT
SSH_PORT = devip.SSH_PORT
SSH_USER = "ls"
SSH_PASS = "ls"  # 设备端固定口令，测试设备通用

WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_frames")


# ------------------------------------------------------------------ HTTP 小工具

def _request(method, url, body=None, timeout=20):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # 把服务端的中文报错原样抛出来，别只留个 422 让人猜
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("%s %s -> HTTP %s\n    %s" % (method, url.split(":18000")[-1],
                                                          e.code, detail)) from None
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", "replace"))


def api_get(base, path, timeout=20):
    return _request("GET", base + path, timeout=timeout)


def api_post(base, path, body, timeout=30):
    return _request("POST", base + path, body, timeout=timeout)


def api_put(base, path, body, timeout=30):
    return _request("PUT", base + path, body, timeout=timeout)




def step(title):
    print("\n" + "=" * 66)
    print("  " + title)
    print("=" * 66)


def ok(msg):
    print("  [OK] " + msg)


def warn(msg):
    print("  [!]  " + msg)


def die(msg, code=1):
    print("\n  [X] " + msg)
    sys.exit(code)


def cid(prefix):
    """command_id —— 前端是 `${前缀}-${crypto.randomUUID()}`，服务端不校验前缀。"""
    return "%s-%s" % (prefix, uuid.uuid4())


def unwrap(obj):
    """设备多数接口返回 {data: {...}}，少数直接返回体。"""
    if isinstance(obj, dict) and "data" in obj and isinstance(obj["data"], (dict, list)):
        return obj["data"]
    return obj


# ------------------------------------------------------------------ SSH 验文件

def ssh_stat(host, remote_path):
    """返回 (size_bytes, mtime, raw) —— 连不上返回 (None, None, err)。"""
    try:
        import paramiko
    except ImportError:
        return None, None, "本机没装 paramiko（pip install paramiko），跳过 SSH 校验"
    cli = None
    for pwd in (SSH_PASS,):
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(host, port=SSH_PORT, username=SSH_USER, password=pwd,
                        timeout=8, banner_timeout=8, auth_timeout=8)
            break
        except Exception as e:  # noqa: BLE001
            cli = None
            err = "%s: %s" % (type(e).__name__, e)
    if cli is None:
        return None, None, locals().get("err", "ssh connect failed")
    try:
        q = "stat -c '%s %Y' " + remote_path
        _, out, errb = cli.exec_command(q, timeout=15)
        txt = out.read().decode("utf-8", "replace").strip()
        if not txt:
            return None, None, errb.read().decode("utf-8", "replace").strip() or "stat 无输出"
        size, mtime = txt.split()[:2]
        return int(size), int(mtime), None
    except Exception as e:  # noqa: BLE001
        return None, None, "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            cli.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 各步骤

def do_login(base, account, password):
    """① 云账号登录。返回 account_id。

    一次 POST /cloud/login 就够，不需要再 POST /cloud/login/select 选身份。
    """
    r = api_post(base, "/api/v2/cloud/login",
                 {"account": account, "password": password, "device_id": ""})
    if not r.get("authenticated"):
        die("云登录失败：%s" % json.dumps(r, ensure_ascii=False)[:200])
    ok("登录成功 account_id=%s role=%s name=%s"
       % (r.get("account_id"), r.get("role"), r.get("name")))
    return r.get("account_id")


def select_task(base, task_id):
    """②⚠️ 选中分配任务 —— 这是全链路最关键的一步。

    必须在 POST /session/start **之前**调用。选中后设备会把采集目录直接
    落在 storage_name（形如 20261003T2102 = {日期}T{taskId}）下，
    session 里 task_source 变成 assigned，后面直接传就行。

    反过来"先采集再 bind-assigned"是错的：bind 是目录**重命名**，
    同一天同一任务目录名已存在就会 422（实测踩过）。
    """
    r = api_put(base, "/api/v2/collection-task/assigned/select", {"taskId": task_id})
    storage = r.get("storage_name") or r.get("name") or ""
    if r.get("source") != "assigned":
        warn("select 返回 source=%s（不是 assigned），任务可能没真正绑上：%s"
             % (r.get("source"), json.dumps(r, ensure_ascii=False)[:200]))
    ok("已选中任务 taskId=%s → 采集目录将落在 %s（collection_name=%s）"
       % (task_id, storage, (r.get("assigned") or {}).get("collection_name")))
    return storage


def verify_assigned(base, expect_task_id):
    """③ 采集后校验：task_source 必须是 assigned，否则传不上去。"""
    s = unwrap(api_get(base, "/api/v2/session", timeout=20)) or {}
    src = s.get("task_source")
    tid = str(s.get("collection_task_id") or "")
    if src != "assigned":
        die("采集后 task_source=%s（期望 assigned）—— 任务没绑上，"
            "现在上传会失败。多半是 assigned/select 没成功，先重跑整个流程。\n"
            "    当前 session: task_name=%s storage=%s"
            % (src, s.get("task_name"), s.get("task_storage_name")))
    if expect_task_id and tid != str(expect_task_id):
        warn("collection_task_id=%s 与期望的 %s 不一致" % (tid, expect_task_id))
    ok("任务绑定确认 task_source=assigned taskId=%s 目录=%s"
       % (tid or expect_task_id, s.get("task_storage_name")))
    return s



def find_task(base, ldp_device_id):
    """查分配任务，用 deviceIds 里的 ldp_device_id 匹配本机。返回 taskId 字符串。"""
    items = unwrap(api_get(base, "/api/v2/collection-task/assigned?createdSort=desc")) or {}
    if isinstance(items, dict):
        items = items.get("items") or []
    hit = None
    for t in items:
        if ldp_device_id in (t.get("deviceIds") or []):
            hit = t
            break
    if not hit:
        die("没有分配给本机（%s）的采集任务。共 %d 个任务，deviceIds 里没有本机。\n"
            "    确认设备云平台已激活、账号已加入该任务。" % (ldp_device_id, len(items)))
    if str(hit.get("taskStatus")) not in ("open", "", None):
        warn("任务 taskStatus=%s（不是 open），可能已关闭" % hit.get("taskStatus"))
    ok("匹配到任务 taskId=%s name=%s（deviceIds=%s）"
       % (hit.get("taskId"), hit.get("name"), hit.get("deviceIds")))
    return str(hit["taskId"])


def pick_source(base, prefer=None):
    """定位可上传的目录。优先级：assigned 源 > prefer 命中 > 最新最大。"""
    files = unwrap(api_get(base, "/api/v2/files")) or []
    cands = [f for f in files
             if f.get("kind") == "mcap" and (f.get("path") or "").find("/") > 0]
    if not cands:
        die("设备上没有 mcap 数据（先跑一次采集）")
    # 一个目录可能含多个 mcap，取每个目录里最大的那个做代表
    best = {}
    for f in cands:
        top = f["path"].split("/")[0]
        if top not in best or (f.get("size_bytes") or 0) > (best[top].get("size_bytes") or 0):
            best[top] = f
    folders = {x.get("path"): x for x in (unwrap(api_get(base, "/api/v2/task-folders")) or [])}
    ranked = sorted(best.values(), key=lambda f: f.get("modified_time_ns") or 0, reverse=True)
    # assigned 源优先（collector 角色只吃 assigned），其次 prefer 命中，最后按时间
    ranked.sort(key=lambda f: (
        0 if folders.get(f["path"].split("/")[0], {}).get("task_source") == "assigned" else 1,
        0 if (prefer and f["path"].startswith(prefer + "/")) else 1))
    f = ranked[0]
    path = f["path"].split("/")[0]
    src = folders.get(path, {}).get("task_source", "local")
    ok("待上传目录 path=%s（%s，%.1f MB，category=%s，源=%s）"
       % (path, f.get("name"), f.get("size_bytes", 0) / 1048576.0,
          f.get("upload_category"), src))
    if src != "assigned":
        warn("该目录 task_source=%s，assigned 角色传不上去。"
             "先 assigned/select 再重新采集（--skip-collect 单独上传很容易踩这个）。" % src)
    return path, f.get("upload_category")




def do_upload(base, path, category, account_id, keep_local=False):
    """发起上传。默认上传完删本地 mcap（delete_after_upload=true）。

    为什么默认删：assigned 任务一天只有一个目录 {日期}T{taskId}，
    而 uploads/unified 的 paths **只接受目录级**（传文件级 path 实测 400）。
    保留本地文件会让目录越堆越大 → 每次上传都把历史文件重传一遍
    （实测同一目录 total 递增 53MB → 86MB → 100MB → 130MB，纯浪费带宽）。
    删掉之后目录干净，每次只传当次那一个文件。
    """
    body = {"paths": [path], "upload_category": category, "account_id": account_id,
            "delete_after_upload": not keep_local}
    r = api_post(base, "/api/v2/cloud/uploads/unified", body, timeout=60)
    job = r.get("job_id") or r.get("jobId") or (r.get("job") or {}).get("job_id")
    if not job:
        die("上传未返回 job_id：%s" % json.dumps(r, ensure_ascii=False)[:300])
    ok("上传任务已创建 job_id=%s（上传后%s本地文件）"
       % (job, "保留" if keep_local else "删除"))
    return job



def poll_upload(base, job, timeout=1800):
    """⑦ 轮询上传状态到终态。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st = unwrap(api_get(base, "/api/v2/cloud/uploads/unified/%s" % job, timeout=30)) or {}
        state = st.get("state") or st.get("status")
        if state != last:
            extra = ""
            if st.get("total_bytes"):
                extra = " %d/%d bytes" % (st.get("uploaded_bytes") or 0, st["total_bytes"])
            print("     状态=%s%s" % (state, extra))
            last = state
        if state in ("complete", "succeeded", "success", "failed", "error", "cancelled"):
            return st
        time.sleep(4)
    die("上传超时（%ds 仍未到终态）" % timeout)


def verify_upload(st, keep_local=False):
    """校验上传结果。"""
    if (st.get("state") or "") not in ("complete", "succeeded", "success"):
        die("上传未成功：state=%s error=%s" % (st.get("state"), st.get("error") or st.get("message")))
    rids = st.get("remote_task_ids") or st.get("remote_dataset_ids") or []
    ok("上传完成 remote_task_ids=%s  %d/%d 字节"
       % (rids, st.get("uploaded_bytes") or 0, st.get("total_bytes") or 0))
    if not rids:
        warn("remote_task_ids 为空 —— 云端可能没真正落库，建议去平台核对")
    deleted = st.get("dataset_deleted")
    if deleted and not keep_local:
        ok("本地 mcap 已按预期删除（下次同目录上传不会重传旧文件）")
    elif deleted and keep_local:
        warn("dataset_deleted=true，但本次指定了 --keep-local，本地文件还是被删了")
    elif not deleted and keep_local:
        ok("本地 mcap 已保留（注意：下次同目录上传会重传它们）")
    return rids



# ------------------------------------------------------------------ 采集

def wait_recorder(base, seconds, min_growth=1):
    """等 recorder 起来并确认 message_count 在涨。"""
    t0 = time.time()
    base_cnt = None
    while time.time() - t0 < 30:
        s = unwrap(api_get(base, "/api/v2/session", timeout=12)) or {}
        rec = s.get("recorder") or {}
        cnt = rec.get("message_count")
        st = s.get("state")
        if st in ("recording", "running") or (rec.get("state") == "recording"):
            if base_cnt is None:
                base_cnt = cnt or 0
                ok("recorder 已启动，message_count=%s" % cnt)
            elif (cnt or 0) > base_cnt:
                ok("数据在涨：%s → %s 条" % (base_cnt, cnt))
                return True
        time.sleep(1.0)
    die("30s 内 recorder 没起来或 message_count 不增长 —— 检查相机是否插好、"
        "profile 是否选对（/api/v2/device-selection）")


def do_collect(base, seconds):
    """采集 N 秒。返回 (mcap_abs_path_hint, message_count)。"""
    api_post(base, "/api/v2/session/start", {"command_id": cid("ui-start")}, timeout=40)
    ok("采集已启动，计划 %d 秒" % seconds)
    try:
        wait_recorder(base, seconds)
        t0 = time.time()
        while time.time() - t0 < seconds:
            time.sleep(1.0)
            s = unwrap(api_get(base, "/api/v2/session", timeout=12)) or {}
            rec = s.get("recorder") or {}
            print("     已录 %.0fs  message_count=%s  queue=%s"
                  % (time.time() - t0, rec.get("message_count"), rec.get("queue_depth")))
    finally:
        r = api_post(base, "/api/v2/session/stop",
                     {"command_id": cid("ui-stop"), "reason": "operator_request"}, timeout=90)
        ok("采集已停止 result=%s" % (r.get("result") or unwrap(r).get("result")))
    s = unwrap(api_get(base, "/api/v2/session", timeout=20)) or {}
    rec = s.get("recorder") or {}
    return s, rec.get("message_count")


def verify_file_on_disk(host, s):
    """⚠️ 成功判据 = SSH 验真实大小 > 0。result=complete 可能是假成功。

    实测遇到过：接口报 result=complete、message_count=328、日志写 size_bytes=3116854，
    但目录里 0 字节。所以只信 stat。
    """
    rec = s.get("recorder") or {}
    cand = rec.get("final_path") or s.get("mcap_path") or ""
    if not cand:
        warn("session 没给 mcap_path/final_path，跳过 SSH 校验")
        return 0
    if not cand.startswith("/"):
        cand = "/home/ls/raspberry-ego-v2/" + cand.lstrip("./")
    size, _mtime, err = ssh_stat(host, cand)
    if err:
        warn("SSH 校验跳过：%s" % err)
        return 0
    if size <= 0:
        die("⚠️ 假成功：%s 大小 %d 字节。接口说 complete 但文件没落盘，别上传，重采一次。"
            % (cand, size))
    ok("SSH 验盘通过：%s = %d 字节 (%.1f MB)" % (cand, size, size / 1048576.0))
    return size



def save_preview(base):
    """抓一张实时预览帧存到 _frames，证明相机链路正常。"""
    try:
        raw = urllib.request.urlopen(base + "/api/v2/preview/snapshot.jpg", timeout=15).read()
        if not raw or len(raw) < 5000:
            warn("预览帧太小（%d 字节），相机可能没出图" % len(raw or b""))
            return None
        os.makedirs(WORK_DIR, exist_ok=True)
        p = os.path.join(WORK_DIR, "snap_%s.jpg" % time.strftime("%H%M%S"))
        with open(p, "wb") as f:
            f.write(raw)
        ok("实时预览帧已存：%s（%d 字节）" % (p, len(raw)))
        return p
    except Exception as e:  # noqa: BLE001
        warn("预览帧抓取失败：%s" % e)
        return None


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(
        description="Ego 设备一键采传：登录→选任务→采集→验盘→上传→校验",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="设备 IP（省略则用 devip.py 自动发现）")
    ap.add_argument("--seconds", type=int, default=5, help="采集秒数，默认 5")
    ap.add_argument("--account", help="云账号（手机号）")
    ap.add_argument("--password", help="云账号密码（建议省略，走交互式输入，不落 shell 历史）")
    ap.add_argument("--skip-collect", action="store_true", help="只上传已有数据")
    ap.add_argument("--skip-upload", action="store_true", help="只采集不上传")
    ap.add_argument("--no-ssh", action="store_true", help="跳过 SSH 验盘（不推荐）")
    ap.add_argument("--keep-local", action="store_true",
                    help="上传后保留本地 mcap（默认删）。保留会导致下次重传整个目录，不建议")
    ap.add_argument("--dry-run", action="store_true",
                    help="只读跑一遍自检+选任务+定位目录，不采集不上传")
    args = ap.parse_args()

    host = devip.resolve_host(args.host)
    base = "http://%s:%d" % (host, API_PORT)
    print("设备 %s   采集 %d 秒" % (host, args.seconds))

    # 0. 设备在线 + 本机身份
    step("0. 设备自检")
    try:
        h = unwrap(api_get(base, "/api/v2/health", timeout=8)) or {}
    except Exception as e:  # noqa: BLE001
        die("设备 %s 连不上（%s）。IP 变了？先跑 find_device.py --save" % (host, e))
    cloud = unwrap(api_get(base, "/api/v2/cloud", timeout=12)) or {}
    ldp_id = cloud.get("ldp_device_id") or ""
    ok("overall_state=%s  平台=%s  ldp_device_id=%s"
       % (h.get("overall_state"), cloud.get("state"), ldp_id or "(空)"))

    # 1. 登录（上传需要；只采集时可跳过）
    account_id = None
    task_id = None
    if not args.skip_upload:
        if not args.account:
            die("上传需要 --account（云账号手机号）。只采集的话加 --skip-upload。")
        password = args.password or getpass.getpass("云账号 %s 密码: " % args.account)
        step("1. 云账号登录")
        account_id = do_login(base, args.account, password)
        step("2. 匹配采集任务")
        task_id = find_task(base, ldp_id)
        step("3. 选中任务（必须在采集之前）")
        select_task(base, task_id)

    if not args.skip_collect:
        if args.dry_run:
            s0 = unwrap(api_get(base, "/api/v2/session", timeout=15)) or {}
            step("4-7. [dry-run] 跳过预览/采集/验盘")
            ok("当前 session: state=%s task_source=%s 目录=%s"
               % (s0.get("state"), s0.get("task_source"), s0.get("task_storage_name")))
        else:
            step("4. 相机实时预览（确认硬件链路）")
            save_preview(base)

            step("5. 采集 %d 秒" % args.seconds)
            sess, cnt = do_collect(base, args.seconds)
            ok("message_count=%s  drop_count=%s  session=%s"
               % (cnt, (sess.get("recorder") or {}).get("drop_count"), sess.get("session_id")))

            if task_id:
                step("6. 校验任务绑定")
                sess = verify_assigned(base, task_id)
            else:
                sess = unwrap(api_get(base, "/api/v2/session", timeout=20)) or {}
                warn("未登录选任务，本次数据 task_source=%s，--skip-upload 才能传"
                     % sess.get("task_source"))

            if not args.no_ssh:
                step("7. SSH 验盘（防假成功）")
                verify_file_on_disk(host, sess)
            else:
                warn("已跳过 SSH 验盘（--no-ssh）")


    if args.skip_collect:
        warn("--skip-collect：不采集，直接上传设备上已有的数据。"
             "若目录 task_source 不是 assigned，上传会失败。")

    if args.skip_upload:
        print("\n完成：只采集，未上传。")
        return

    step("8. 定位待上传目录")
    sess_now = unwrap(api_get(base, "/api/v2/session", timeout=15)) or {}
    storage = sess_now.get("task_storage_name") or ""
    path, category = pick_source(base, prefer=storage or None)

    if args.dry_run:
        print("\n[dry-run] 到此为止，不执行上传。")
        return

    step("9. 上传 + 校验")
    if args.keep_local:
        warn("用了 --keep-local：本地文件不删，下次同目录上传会把它们一起重传")
    job = do_upload(base, path, category, account_id, keep_local=args.keep_local)
    st = poll_upload(base, job)
    verify_upload(st, keep_local=args.keep_local)

    print("\n" + "=" * 66)
    print("  全链路完成：登录 → 选任务 → 采集 → 验盘 → 上传 → 校验")
    print("=" * 66)




if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        # _request 把服务端中文报错包成 RuntimeError 了，这里友好输出
        print("\n  [X] 请求失败：\n    %s" % e)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n  已中断。")
        sys.exit(130)

