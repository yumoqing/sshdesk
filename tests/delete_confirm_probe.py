"""离线探针：删除连接必须二次确认，只有点「确认」才真删。

覆盖 4 条路径（全部 offscreen，不连 SSH）：
  1) 连接页签 × → 点「取消」  → 页签还在（没删连接）
  2) 连接页签 × → 点「确认删除」→ 页签被移除
  3) 会话管理「删除」→「取消」  → 配置里的连接还在
  4) 会话管理「删除」→「确认删除」→ 连接被删

跑法：QT_QPA_PLATFORM=offscreen ~/work/venvs/sshdesk-build/bin/python tests/delete_confirm_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QLabel, QTabWidget  # noqa: E402

import sshdesk.app as app_mod  # noqa: E402
import sshdesk.dialogs as dlg_mod  # noqa: E402
from sshdesk.config import HostProfile  # noqa: E402

ok = True


def check(cond, msg):
    global ok
    print(("  ok   " if cond else "  FAIL ") + msg)
    ok = ok and bool(cond)


class FakePage:
    """只提供 close_tab/_confirm_close 用到的字段。"""

    def __init__(self, name):
        self.profile = HostProfile(name=name, host="10.0.0.9", user="root")
        self.holder = None

    def dispose(self):
        self.disposed = True


class Stub:
    """借用真实 MainWindow.close_tab 的最小外壳。"""

    # 直接把真方法挂到外壳上，测的就是生产代码本身（不是复制一份逻辑）
    close_tab = app_mod.MainWindow.close_tab
    _confirm_close = app_mod.MainWindow._confirm_close

    def __init__(self):
        self.tabs = QTabWidget()
        self.pages_ = []
        self._trash = []
        self.status_label = QLabel()


def make_stub(name="测试机"):
    s = Stub()
    page = FakePage(name)
    holder = QLabel("x")
    page.holder = holder
    s.pages_.append(page)
    s.tabs.addTab(holder, name)
    s.tabs.setCurrentWidget(holder)
    return s, page


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])

    # ---- 1) 页签 × 取消 ----
    s, page = make_stub()
    app_mod.ask_delete = lambda *a, **k: False
    app_mod.MainWindow.close_tab(s, 0)
    app.processEvents()
    check(s.tabs.count() == 1, "页签 × 点「取消」：页签仍在（count=%d）" % s.tabs.count())
    check(s.pages_ == [page], "页签 × 点「取消」：连接对象未被 dispose")

    # ---- 2) 页签 × 确认 ----
    s, page = make_stub()
    app_mod.ask_delete = lambda *a, **k: True
    app_mod.MainWindow.close_tab(s, 0)
    app.processEvents()
    check(s.tabs.count() == 0, "页签 × 点「确认删除」：页签已移除（count=%d）" % s.tabs.count())
    check(s.pages_ == [], "页签 × 点「确认删除」：连接对象已从 pages_ 移除")
    check(getattr(page, "disposed", False), "页签 × 点「确认删除」：会话已 dispose")

    # ---- 3/4) 会话管理「删除」 ----
    class FakeCfg:
        def __init__(self):
            self.hosts = [HostProfile(name="主机A", host="10.0.0.1", user="root")]

        def save(self):
            self.saved = True

    for answer, expect in ((False, 1), (True, 0)):
        cfg = FakeCfg()
        mgr = dlg_mod.SessionManager(cfg)
        mgr.list.setCurrentRow(0)
        dlg_mod.ask_delete = lambda *a, **k: answer
        mgr._del()
        app.processEvents()
        tag = "确认删除" if answer else "取消"
        check(len(cfg.hosts) == expect,
              "会话管理删除点「%s」：剩余连接 %d（期望 %d）" % (tag, len(cfg.hosts), expect))
        mgr.close()

    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())