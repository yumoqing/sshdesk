"""诊断日志：出了事要能拿到现场。

窗口版 exe 没有控制台，v0.1.0 的错误只剩对话框里截断的一行，用户报障时无从下手
（"大文件断连"到底是哪一步、哪个异常、远端怎么回的，全都没留下）。这里把连接、
SFTP 重开、传输重试、删除失败统统落到 ``~/.sshdesk/logs/sshdesk.log``：

    C:\\Users\\<你>\\.sshdesk\\logs\\sshdesk.log        # Windows
    ~/.sshdesk/logs/sshdesk.log                       # macOS / Linux

报障时把这行路径发过来就有现场了；文件按 2MB 滚动，最多 3 份，不会长胖。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading

LOG_DIR = os.path.join(os.path.expanduser("~"), ".sshdesk", "logs")
LOG_PATH = os.path.join(LOG_DIR, "sshdesk.log")
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3

_ready = False


def setup(build: str = "") -> str:
    """初始化日志（幂等）。返回日志文件路径。"""
    global _ready
    if _ready:
        return LOG_PATH
    _ready = True
    lg = logging.getLogger("sshdesk")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        lg.addHandler(fh)
    except Exception:                       # noqa: BLE001  日志起不来也不能影响启动
        pass
    try:
        if sys.stderr is not None:          # 源码运行/终端里也能看见
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            sh.setLevel(logging.INFO)
            lg.addHandler(sh)
    except Exception:                       # noqa: BLE001
        pass

    def _hook(etype, value, tb):
        lg.error("未捕获异常", exc_info=(etype, value, tb))
        try:
            sys.__excepthook__(etype, value, tb)
        except Exception:                   # noqa: BLE001
            pass

    def _thook(args):
        lg.error("线程 %s 未捕获异常", getattr(args.thread, "name", "?"),
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _hook
    threading.excepthook = _thook
    lg.info("===== sshdesk 启动 build=%s python=%s %s =====",
            build or "?", sys.version.split()[0], sys.platform)
    return LOG_PATH


def log() -> logging.Logger:
    return logging.getLogger("sshdesk")