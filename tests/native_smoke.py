"""原生控件冒烟：直接验 bricks-qt 的 Terminal / RemoteFiles 在本平台可用。

两个用途：
1. 本机手动跑：python tests/native_smoke.py
2. CI（windows-latest + ubuntu-latest）跑同一份，作为"双平台原生可用"的证据
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QSize, Qt             # noqa: E402
from PySide6.QtWidgets import QApplication       # noqa: E402

from bricks_qt import Ctx, build, load_file      # noqa: E402
from bricks_qt.remotefiles import RemoteFiles    # noqa: E402
from bricks_qt.terminal import Terminal          # noqa: E402

UI = os.path.join(ROOT, "sshdesk", "ui", "session.ui")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    node = load_file(UI)
    ctx = Ctx(base_dir=os.path.dirname(UI))
    root = build(node, ctx)
    assert root is not None, "session.ui 渲染为空"
    # 必须真正布局一次，否则 TabPanel 里的终端只有几十像素宽，什么都验不出来
    root.setAttribute(Qt.WA_DontShowOnScreen, True)
    root.resize(QSize(820, 460))
    root.show()
    for _ in range(15):
        app.processEvents()
        time.sleep(0.02)

    term = ctx.get("term")
    files = ctx.get("files")
    assert isinstance(term, Terminal), "Terminal 没建出来：%r" % type(term)
    assert isinstance(files, RemoteFiles), "RemoteFiles 没建出来：%r" % type(files)

    # 真实形态的彩色提示符（真机带 OSC 改标题 + 绿色/蓝色 SGR）
    term.feed(b"(py3) \x1b]0;ymq@home: ~\x07\x1b[01;32mymq@home\x1b[00m:"
              b"\x1b[01;34m~\x1b[00m$ echo hi\r\nhi\r\n")
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    txt = term.screen_text()
    assert "ymq@home:~$ echo hi" in txt, "终端没收到内容：%r" % txt[:60]

    # 再改一次尺寸：这条会走 pyte 的缩屏重放，颜色必须保住（早期实现会退化成黑白）
    term.resize(QSize(700, 320))
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)
    img = term.grab().toImage()          # 触发 paintEvent：颜色解析出错会在这抛
    assert not img.isNull(), "终端截图失败"

    green = blue = 0
    for y in range(0, min(img.height(), 40)):
        for x in range(0, img.width()):
            c = img.pixelColor(x, y)
            if c.green() > 140 and c.red() < 90 and c.blue() < 90:
                green += 1
            if c.blue() > 120 and c.red() < 90:
                blue += 1
    assert green and blue, "颜色没渲染出来（green=%d blue=%d）" % (green, blue)

    assert not files.btn_upload.isEnabled(), "未连接时上传按钮应禁用"

    out = os.path.join(ROOT, "out")
    os.makedirs(out, exist_ok=True)
    img.save(os.path.join(out, "native_smoke.png"))
    print("native smoke ok: Terminal + RemoteFiles 在 %s 可用" % sys.platform)
    return 0


if __name__ == "__main__":
    sys.exit(main())
