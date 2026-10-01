"""读取 ego 设备容器内 /app 源码 —— 绕过 docker 权限限制。

原理：服务进程以 ls 用户运行，宿主上可见其 PID；容器根文件系统可通过
/proc/<pid>/root/ 直接读，不需要 docker 权限、不需要 sudo。

用法:
  python container_src.py --host 192.168.195.38 ls                  # 列 /app
  python container_src.py --host 192.168.195.38 grep "queue overflow"
  python container_src.py --host 192.168.195.38 cat backend/DevicesManager/ego_std/hardware/yctc_imu.py --lines 230,300
  python container_src.py --host 192.168.195.38 find "*.py" --dir backend/DevicesManager
"""

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import paramiko
except ImportError:
    print("需要 paramiko： uv pip install --python <venv> paramiko")
    sys.exit(1)

USER = "ls"
PASSWORD = "ls"
PROC_PATTERN = "backend.main"


def find_pid(c):
    """在宿主进程表里找 backend.main 的 PID。"""
    i, o, e = c.exec_command(
        "ps -eo pid,args 2>/dev/null | grep '%s' | grep -v grep | awk '{print $1}' | head -1" % PROC_PATTERN,
        timeout=30,
    )
    out = o.read().decode("utf-8", "replace").strip()
    return int(out) if out.isdigit() else None


def run(c, cmd, timeout=90):
    i, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    return out or err


def main():
    ap = argparse.ArgumentParser(description="读取 ego 容器内的 /app 源码")
    ap.add_argument("--host", default=None,
                    help="设备 IP；省略则读 device_ip.txt，没有就自动扫描本机网段")
    ap.add_argument("--user", default=USER)
    ap.add_argument("--password", default=PASSWORD)
    ap.add_argument("action", choices=["ls", "grep", "cat", "find", "info"])
    ap.add_argument("target", nargs="?", default="", help="grep 模式 / cat 的相对路径 / find 的 glob")
    ap.add_argument("--dir", default="", help="限定目录（相对 /app）")
    ap.add_argument("--lines", default="", help="cat 的行范围，如 230,300")
    ap.add_argument("--limit", type=int, default=200, help="输出最大行数")
    args = ap.parse_args()

    import devip
    args.host = devip.resolve_host(args.host)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(args.host, username=args.user, password=args.password, timeout=12)
    except Exception as exc:
        print("连接失败:", exc)
        sys.exit(1)

    pid = find_pid(c)
    if pid is None:
        print("未找到 %s 进程，服务可能没启动" % PROC_PATTERN)
        c.close()
        sys.exit(1)
    root = "/proc/%d/root/app" % pid

    if args.action == "info":
        print("backend PID :", pid)
        print("容器根      : /proc/%d/root" % pid)
        print("源码路径    :", root)
        print("BUILD_METADATA:", run(c, "cat %s/BUILD_METADATA.json" % root).strip())
        print("cwd         :", run(c, "readlink /proc/%d/cwd" % pid).strip())
        print("python      :", run(c, "readlink /proc/%d/exe" % pid).strip())
        c.close()
        return

    base = root + ("/" + args.dir.strip("/") if args.dir else "")

    if args.action == "ls":
        cmd = "ls -la %s/%s" % (base, args.target.strip("/")) if args.target else "ls -la %s" % base
    elif args.action == "grep":
        if not args.target:
            print("grep 需要给模式")
            c.close()
            sys.exit(1)
        cmd = "grep -rn '%s' %s --include=*.py --include=*.yaml --include=*.json 2>/dev/null | head -%d" % (
            args.target, base, args.limit)
    elif args.action == "cat":
        if not args.target:
            print("cat 需要给文件路径")
            c.close()
            sys.exit(1)
        path = "%s/%s" % (root, args.target.strip("/"))
        if args.lines:
            cmd = "sed -n '%sp' %s" % (args.lines.replace(",", ","), path)
        else:
            cmd = "cat %s" % path
        cmd += " | head -%d" % args.limit
    else:  # find
        pat = args.target or "*"
        cmd = "find %s -name '%s' 2>/dev/null | head -%d" % (base, pat, args.limit)

    print(run(c, cmd).strip()[:20000])
    c.close()


if __name__ == "__main__":
    main()
