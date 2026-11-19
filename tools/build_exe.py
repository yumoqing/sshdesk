"""用 PyInstaller 打包 sshdesk：Windows exe / macOS .app / Linux 可执行目录。

用法：
    python tools/build_exe.py [--onedir|--onefile] [--console]

产物：
    Windows  dist/sshdesk/sshdesk.exe（onedir）或 dist/sshdesk.exe（onefile）
    macOS    dist/sshdesk.app（--windowed 时 PyInstaller 自动成 bundle）
    Linux    dist/sshdesk/sshdesk

输出目录可用环境变量 SSHDESK_DISTPATH / SSHDESK_WORKPATH 改：`dist\\sshdesk` 里的 exe
正在运行时文件被锁，重打包前先打到别处（例如 dist_new），关掉旧窗口再换过来。

注意：bricks 的 .ui 文件必须打进包里（sshdesk/ui），否则界面上不来。
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPPUBLIC = os.environ.get("SSHDESK_APPPUBLIC") or r"D:\work\repos\apppublic"
BRICKSQT = (os.environ.get("SSHDESK_BRICKSQT")
            or os.path.join(os.path.dirname(ROOT), "bricks-qt"))   # 界面渲染器源码仓
SEP = ";" if sys.platform.startswith("win") else ":"          # PyInstaller 的 --add-data 分隔符


def main() -> int:
    onefile = "--onefile" in sys.argv
    console = "--console" in sys.argv
    mode = "--onefile" if onefile else "--onedir"
    entry = os.path.join(ROOT, "tools", "entry.py")
    distpath = os.environ.get("SSHDESK_DISTPATH") or os.path.join(ROOT, "dist")
    workpath = os.environ.get("SSHDESK_WORKPATH") or os.path.join(ROOT, "build")

    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--name", "sshdesk", mode,
            "--console" if console else "--windowed",
            "--distpath", distpath,
            "--workpath", workpath,
            "--paths", ROOT,
            "--hidden-import", "bricks_qt",           # 界面来自 bricks_qt（editable 装法 PyInstaller 可能跟不到，下面再补 --paths）
            "--collect-submodules", "bricks_qt",
            "--collect-submodules", "appPublic",
            "--collect-submodules", "asyncssh",
            "--collect-submodules", "pyte",
            "--hidden-import", "paramiko",
            "--hidden-import", "jinja2",
            "--hidden-import", "wcwidth",
            "--exclude-module", "tkinter",
            "--exclude-module", "numpy",
            "--exclude-module", "pandas"]
    # .ui 界面文件（bricks 描述）必须随包走
    args += ["--add-data", os.path.join(ROOT, "sshdesk", "ui") + SEP + os.path.join("sshdesk", "ui")]
    args += ["--add-data", os.path.join(ROOT, "assets") + SEP + "assets"]      # 图标（窗口图标运行时也要）

    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "cn.kymoz.sshdesk"]
        icon = os.path.join(ROOT, "assets", "sshdesk.icns")
        if os.path.isfile(icon):
            args += ["--icon", icon]
    elif sys.platform.startswith("win"):
        icon = os.path.join(ROOT, "assets", "sshdesk.ico")
        if os.path.isfile(icon):
            args += ["--icon", icon]

    if os.path.isdir(APPPUBLIC):
        args += ["--paths", APPPUBLIC]
    if os.path.isdir(BRICKSQT):
        args += ["--paths", BRICKSQT]
    args.append(entry)

    print("运行:", " ".join(args[:10]), "...")
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0:
        return r.returncode

    if sys.platform == "darwin":
        out = os.path.join(distpath, "sshdesk.app")
    elif sys.platform.startswith("win"):
        out = (os.path.join(distpath, "sshdesk", "sshdesk.exe") if not onefile
               else os.path.join(distpath, "sshdesk.exe"))
    else:
        out = (os.path.join(distpath, "sshdesk", "sshdesk") if not onefile
               else os.path.join(distpath, "sshdesk"))
    print("产物:", out, os.path.exists(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())