#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令台后端 —— 配合 app.py 的「⑩ 命令台」面板用。

用法（一般由 GUI 调，别手敲）：
    python _cli.py api  <路径> [json_body]
    python _cli.py ssh  <shell 命令>
    python _cli.py py   <脚本名> [参数...]

设计要点：
  * api 模式允许路径里写 {ip}，会被替换成 device_ip.txt 里的地址，
    这样从 GUI 复制过来的命令不用手改 IP。
  * 输出统一走 [OK] / [!] / [X] 前缀，GUI 靠这三个前缀上色。
  * 任何异常都打印成 [X] 而不是抛栈 —— 命令台要的是"看结果"不是"看回溯"。
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
API_PORT = 18000
SSH_USER, SSH_PASS = "ls", "ls"


def ok(msg: str) -> None:
    print("[OK] " + str(msg))


def warn(msg: str) -> None:
    print("[!] " + str(msg))


def bad(msg: str) -> None:
    print("[X] " + str(msg))


def target_host() -> str:
    """取设备 IP：device_ip.txt 优先，其次取第一个命令行参数里像 IP 的东西。"""
    f = BASE / "device_ip.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    for a in sys.argv[2:]:
        parts = a.replace(":", "/").split("/")
        for p in parts:
            if p.count(".") == 3 and all(x.isdigit() for x in p.split(".")):
                return p
    return ""


# --------------------------------------------------------------------- api


def run_api(path: str, body: str) -> int:
    host = target_host()
    if "{ip}" in path:
        # 用户自己写了占位符，没 IP 就直接说，别因为"拿不到 IP"提前退出
        path = path.replace("{ip}", host or "127.0.0.1")
        if not host:
            warn("device_ip.txt 里没有 IP，{ip} 暂时替换成 127.0.0.1")
    elif not host:
        bad("拿不到设备 IP：device_ip.txt 不存在或为空")
        return 2

    # 允许 "POST /api/v2/xxx" 这种带方法前缀的写法
    method = "GET"
    p = path.strip()
    if " " in p:
        head, p = p.split(None, 1)
        m = head.strip().upper()
        if m in ("GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"):
            method = m
        else:
            p = path.strip()
    if not p.startswith("/"):
        p = "/" + p

    url = f"http://{host}:{API_PORT}{p}"
    data = None
    if body.strip():
        try:
            data = json.dumps(json.loads(body)).encode("utf-8")
        except json.JSONDecodeError as e:
            bad(f"Body 不是合法 JSON：{e}")
            print("    合法示例：{\"command_id\":\"cli-1\",\"reason\":\"operator_request\"}")
            return 2
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    print(f"=== {method} {url}")
    if data:
        print(f"    body: {data.decode('utf-8', 'replace')}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    except Exception as e:  # noqa: BLE001
        bad(f"{type(e).__name__}: {e}")
        return 1

    text = raw.decode("utf-8", "replace")
    if code >= 400:
        bad(f"HTTP {code}")
    else:
        ok(f"HTTP {code}")
    # JSON 漂亮打印；不是 JSON 就原样输出（图片/文本）
    try:
        print(json.dumps(json.loads(text), ensure_ascii=False, indent=1))
    except Exception:  # noqa: BLE001
        if len(text) > 4000:
            print(text[:4000] + f"\n... （共 {len(text)} 字节，已截断）")
        else:
            print(text)
    return 0 if code < 400 else 1


# --------------------------------------------------------------------- ssh


def run_ssh(cmd: str) -> int:
    if not cmd.strip():
        bad("命令为空")
        return 2
    host = target_host()
    if not host:
        bad("拿不到设备 IP")
        return 2
    try:
        import paramiko
    except ImportError:
        bad("没装 paramiko，无法 SSH。装一下：pip install paramiko")
        return 2
    print(f"=== ssh {SSH_USER}@{host} : {cmd}")
    cli = None
    try:
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(host, username=SSH_USER, password=SSH_PASS, timeout=20,
                    look_for_keys=False, allow_agent=False)
        _in, out, err = cli.exec_command(cmd, timeout=180)
        so = out.read().decode("utf-8", "replace")
        se = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if so:
            print(so.rstrip())
        if se:
            print("--- stderr ---")
            print(se.rstrip())
        if rc == 0:
            ok(f"退出码 0")
        else:
            bad(f"退出码 {rc}")
        return 0 if rc == 0 else 1
    except Exception as e:  # noqa: BLE001
        bad(f"{type(e).__name__}: {e}")
        return 1
    finally:
        if cli is not None:
            try:
                cli.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------- py


def run_py(joined: str) -> int:
    parts = joined.split()
    if not parts:
        bad("脚本名为空")
        return 2
    script = parts[0]
    p = BASE / script
    if not p.exists():
        cands = [x.name for x in BASE.glob("*.py") if script in x.name]
        bad(f"找不到脚本 {script}")
        if cands:
            print("    名字接近的有：" + "、".join(cands))
        return 2
    argv = [sys.executable, str(p)] + parts[1:]
    print(f"=== {' '.join(argv)}")
    try:
        rc = subprocess.call(argv, cwd=str(BASE))
    except Exception as e:  # noqa: BLE001
        bad(f"{type(e).__name__}: {e}")
        return 1
    if rc == 0:
        ok("退出码 0")
    else:
        bad(f"退出码 {rc}")
    return rc


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    mode = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    body = sys.argv[3] if len(sys.argv) > 3 else ""
    try:
        if mode == "api":
            return run_api(arg, body)
        if mode == "ssh":
            return run_ssh(arg)
        if mode == "py":
            return run_py(arg)
        bad(f"未知模式 {mode}（可选 api / ssh / py）")
        return 2
    except Exception:  # noqa: BLE001
        bad("命令台内部异常")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
