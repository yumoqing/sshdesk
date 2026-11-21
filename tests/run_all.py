#!/usr/bin/env python3
"""一条命令跑完全部检查（本地与 CI 用同一个入口）。

    python tests/run_all.py              # 全量跑一遍
    python tests/run_all.py -k gui       # 只跑名字含 gui 的步骤
    python tests/run_all.py --repeat 5   # 每个步骤重复 5 次（查偶发/段错误必用）

步骤顺序：.ui 构建校验 → 窗口自检 → 原生控件冒烟 → 断点续传 → 授权 → 端到端全链路。
任一步骤非 0 退出即整体失败；失败时打印该步骤输出的最后 40 行。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def steps() -> list[tuple[str, list[str]]]:
    py = sys.executable
    out: list[tuple[str, list[str]]] = []
    for name in sorted(os.listdir(os.path.join(ROOT, "sshdesk", "ui"))):
        if name.endswith(".ui"):
            out.append(("ui 校验:%s" % name,
                        [py, "-m", "bricks_qt.cli", "check",
                         os.path.join(ROOT, "sshdesk", "ui", name), "--strict"]))
    out.append(("窗口自检:selftest", [py, "-m", "sshdesk", "--selftest"]))
    out.append(("原生控件冒烟:native", [py, os.path.join(ROOT, "tests", "native_smoke.py")]))
    out.append(("断点续传:resume", [py, os.path.join(ROOT, "tests", "test_resume.py")]))
    out.append(("授权:license", [py, os.path.join(ROOT, "tests", "test_license.py")]))
    out.append(("端到端全链路:e2e", [py, os.path.join(ROOT, "tests", "test_client.py")]))
    return out


def env_for_run() -> dict[str, str]:
    env = dict(os.environ)
    parts = [ROOT]
    pub = env.get("SSHDESK_APPPUBLIC")            # CI 把 appPublic 装在临时目录
    if pub and os.path.isdir(pub):
        parts.append(pub)
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONUNBUFFERED"] = "1"                 # 段错误会吞掉缓冲输出
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--only", default="", help="只跑名字里含该关键字的步骤")
    ap.add_argument("--repeat", type=int, default=1, help="每个步骤重复几次（查偶发用）")
    args = ap.parse_args()

    picked = [(n, c) for n, c in steps() if not args.only or args.only in n]
    if not picked:
        print("没有匹配的步骤：%r" % args.only)
        return 2

    env = env_for_run()
    print("入口: %s" % os.path.abspath(__file__))
    print("解释器: %s" % sys.executable)
    print("PYTHONPATH: %s" % env["PYTHONPATH"])
    print("每步重复: %d\n" % args.repeat)

    failed: list[str] = []
    total = 0.0
    for name, cmd in picked:
        for i in range(args.repeat):
            tag = name if args.repeat == 1 else "%s#%d" % (name, i + 1)
            t0 = time.time()
            p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
            dt = time.time() - t0
            total += dt
            if p.returncode == 0:
                print("  OK   %-26s %6.2fs" % (tag, dt))
            else:
                failed.append(tag)
                print("  FAIL %-26s %6.2fs  rc=%d" % (tag, dt, p.returncode))
                tail = (p.stdout + p.stderr).strip().splitlines()[-40:]
                for line in tail:
                    print("       | %s" % line)

    print("\n共 %d 项，失败 %d 项，用时 %.1fs" % (len(picked) * args.repeat, len(failed), total))
    if failed:
        print("失败：" + ", ".join(failed))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())