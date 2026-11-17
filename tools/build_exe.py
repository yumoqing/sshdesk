"""用 PyInstaller 把 sshdesk 打成 Windows exe（onedir，双击即用）。

用法：python tools/build_exe.py [--onedir|--onefile] [--console]
产物：dist/sshdesk/sshdesk.exe（onedir）或 dist/sshdesk.exe（onefile）
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPPUBLIC = os.environ.get("SSHDESK_APPPUBLIC") or r"D:\work\repos\apppublic"


def main() -> int:
    mode = "--onefile" if "--onefile" in sys.argv else "--onedir"
    console = "--console" in sys.argv
    entry = os.path.join(ROOT, "tools", "entry.py")
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--name", "sshdesk", mode,
            "--console" if console else "--windowed",
            "--paths", ROOT,
            "--collect-submodules", "appPublic",
            "--collect-submodules", "asyncssh",
            "--collect-submodules", "pyte",
            "--hidden-import", "paramiko",
            "--hidden-import", "jinja2",
            "--hidden-import", "wcwidth",
            "--exclude-module", "tkinter",
            "--exclude-module", "numpy",
            "--exclude-module", "pandas"]
    if os.path.isdir(APPPUBLIC):
        args += ["--paths", APPPUBLIC]
    args.append(entry)
    print("运行:", " ".join(args[:8]), "...")
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode == 0:
        exe = (os.path.join(ROOT, "dist", "sshdesk", "sshdesk.exe") if mode == "--onedir"
               else os.path.join(ROOT, "dist", "sshdesk.exe"))
        print("产物:", exe, os.path.exists(exe))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
