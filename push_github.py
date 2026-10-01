#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ego-std-test 一键推到 GitHub 私有仓库。

用法
----
    python push_github.py --check                 # 只做本地准备检查，不需要 Token
    python push_github.py                         # 交互式：粘贴 Token 后自动完成一切
    python push_github.py ghp_xxxxxxxx            # 直接带 Token
    python push_github.py --repo my-name          # 换个仓库名（默认 ego-std-testkit）
    python push_github.py --public                # 建公开仓库（默认私有，强烈建议别用这个）

它会做四件事：
    1. 用 Token 调 GitHub API 校验身份
    2. 建私有仓库（已存在则直接复用，不会报错）
    3. 推送本仓库 main 分支
    4. 把 Token 从 .git/config 的 remote 地址里抹掉（避免明文留在本地）

只依赖 Python 标准库，不需要装任何第三方包。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
BASE = Path(__file__).resolve().parent
DEFAULT_REPO = "ego-std-testkit"
DEFAULT_DESC = "Ego-Std 交付测试台：设备 API 自动巡检 + 桌面 GUI"

# 这些文件哪怕被 .gitignore 漏掉，也绝不推到远端
SENSITIVE_NAMES = {"device_ip.txt", ".env", "token.txt", "secrets.json", "credentials.json"}


def git(*args: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(
        ["git", *args], cwd=str(BASE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return p.returncode, (p.stdout + p.stderr).strip()


def api(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "ego-std-testkit-push")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # 网络层错误
        return 0, f"{type(e).__name__}: {e}"


def preflight() -> tuple[bool, list[str]]:
    """本地准备情况检查。返回 (是否可以推, 报告行列表)。"""
    ok = True
    lines: list[str] = []

    rc, out = git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        lines.append("[X] 当前目录不是 git 仓库")
        return False, lines
    lines.append("[OK] git 仓库正常")

    rc, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        lines.append("[X] 读不到当前分支")
        ok = False
    else:
        lines.append(f"[{'OK' if branch == 'main' else '!!'}] 当前分支：{branch}"
                     + ("" if branch == "main" else "（推送时会推到 main，注意）"))

    rc, dirty = git("status", "--short")
    if dirty:
        lines.append(f"[!!] 工作区有 {len(dirty.splitlines())} 处未提交改动（建议先 git add -A && git commit）")
    else:
        lines.append("[OK] 工作区干净，无未提交改动")

    rc, tracked = git("ls-files")
    files = [f for f in tracked.splitlines() if f.strip()]
    lines.append(f"[OK] 已跟踪 {len(files)} 个文件")

    leak = [f for f in files if Path(f).name in SENSITIVE_NAMES]
    if leak:
        lines.append(f"[X] 敏感文件被跟踪了：{', '.join(leak)}")
        ok = False
    else:
        lines.append("[OK] 未跟踪任何凭据/设备 IP 文件")

    for name in SENSITIVE_NAMES:
        if (BASE / name).exists():
            lines.append(f"[--] 本地存在 {name}（已被 .gitignore 排除，不会上传）")

    rc, remote = git("remote", "get-url", "origin")
    if rc == 0:
        safe = remote
        if "@" in remote and "github.com" in remote:
            safe = "https://<token>@github.com/" + remote.split("github.com/", 1)[-1]
        lines.append(f"[--] 已有 origin：{safe}")
    else:
        lines.append("[--] 尚未配置 origin（推送时自动添加）")

    return ok, lines


def scrub_token_from_remote(login: str, repo: str) -> None:
    """把 remote 地址里的 Token 抹掉，只留干净 URL。"""
    git("remote", "set-url", "origin", f"https://github.com/{login}/{repo}.git")


def main() -> int:
    ap = argparse.ArgumentParser(description="把本目录推到 GitHub")
    ap.add_argument("token", nargs="?", help="GitHub Personal Access Token（不填则交互式输入）")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"仓库名，默认 {DEFAULT_REPO}")
    ap.add_argument("--desc", default=DEFAULT_DESC, help="仓库描述")
    ap.add_argument("--public", action="store_true", help="建公开仓库（默认私有）")
    ap.add_argument("--check", action="store_true", help="只检查本地准备情况，不联网")
    args = ap.parse_args()

    print("=" * 60)
    print("  ego-std-test  →  GitHub")
    print("=" * 60)

    ok, lines = preflight()
    for ln in lines:
        print("  " + ln)
    print("=" * 60)

    if args.check:
        print("  仅检查模式，未联网。" + ("可以推送。" if ok else "请先解决上面的 [X]。"))
        return 0 if ok else 1

    token = args.token
    if not token:
        print("  粘贴 Token 后回车（在 GitHub 上生成：github.com/settings/tokens/new，只勾 repo）")
        try:
            token = input("  Token: ").strip()
        except EOFError:
            print("  没有读到输入。")
            return 1
    if not token:
        print("  未提供 Token，退出。")
        return 1

    # 1) 校验身份
    print("\n[1/4] 校验 Token …")
    code, body = api("GET", "/user", token)
    if code != 200:
        print(f"  失败（HTTP {code}）：{body}")
        print("  常见原因：Token 拼错 / 已过期 / 没勾 repo 权限。")
        return 1
    login = body.get("login") if isinstance(body, dict) else None
    if not login:
        print(f"  拿不到账号名：{body}")
        return 1
    print(f"  已认证为：{login}")

    # 2) 建仓库（幂等）
    private = not args.public
    print(f"\n[2/4] 创建{'私有' if private else '公开'}仓库 {login}/{args.repo} …")
    code, body = api("POST", "/user/repos", token, {
        "name": args.repo, "description": args.desc,
        "private": private, "auto_init": False, "has_issues": True,
    })
    if code == 201:
        print("  创建成功")
    elif code == 422:
        print("  仓库已存在，直接复用")
    elif code == 404:
        # 这是 fine-grained 长令牌最典型的坑：能认证、能读，但没有建仓权限，
        # GitHub 为了不泄露仓库是否存在，统一返回 404 而不是 403。
        print("  失败（HTTP 404）：这个 Token 没有创建仓库的权限。")
        print()
        print("  ┌─ 原因 ─────────────────────────────────────────────")
        print("  │ 你现在用的是 GitHub 新版默认的 fine-grained（细粒度）Token，")
        print("  │ 它默认不给「Administration: Read and write」权限，")
        print("  │ 所以只能读、不能建仓库。")
        print("  │")
        print("  │ 换成 classic（经典）Token，一个勾就够：")
        print("  │   1. 打开 https://github.com/settings/tokens")
        print("  │   2. 左侧菜单点 Tokens (classic)   ← 注意不是 Fine-grained")
        print("  │   3. 右上 Generate new token → Generate new token (classic)")
        print("  │   4. Note 填 ego-std-testkit，Expiration 选 7 days")
        print("  │   5. 只勾最上面那块 repo")
        print("  │   6. 拉到底点 Generate token，复制 ghp_ 开头那串")
        print("  └────────────────────────────────────────────────────")
        print("  如果你确实想继续用 fine-grained，就必须重建一个，并同时满足：")
        print("    · Repository access 选 All repositories")
        print("    · Permissions → Administration 设为 Read and write")
        print("    · Permissions → Contents 设为 Read and write")
        print()
        print(f"  原始响应：{body}")
        return 1
    else:
        print(f"  失败（HTTP {code}）：{body}")
        return 1

    html_url = f"https://github.com/{login}/{args.repo}"

    # 3) 推送
    print("\n[3/4] 推送 main 分支 …")
    remote = f"https://{login}:{token}@github.com/{login}/{args.repo}.git"
    rc, out = git("remote", "get-url", "origin")
    if rc == 0:
        git("remote", "set-url", "origin", remote)
    else:
        git("remote", "add", "origin", remote)
    rc, out = git("push", "-u", "origin", "main", timeout=300)
    print("  " + (out.replace(token, "***") if out else "(无输出)"))
    if rc != 0:
        print("  推送失败。")
        scrub_token_from_remote(login, args.repo)
        return 1

    # 4) 抹掉 Token
    print("\n[4/4] 清理本地 Token 痕迹 …")
    scrub_token_from_remote(login, args.repo)
    rc, out = git("remote", "-v")
    print("  origin = " + (out.replace(token, "***") if out else "?"))
    print("  已抹除（.git/config 里不留明文）")

    print("\n" + "=" * 60)
    print(f"  完成 →  {html_url}")
    print("=" * 60)
    print("  提醒：Token 用完了，可以到 github.com/settings/tokens 把它 Delete 掉，")
    print("        删掉不影响仓库和代码。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
